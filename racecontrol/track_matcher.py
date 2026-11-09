"""Suggestions de circuits par correspondance géométrique, entièrement hors ligne.

Les bordures installées et les trajectoires embarquées sont examinées. Les
points de bordure ne sont pas reliés ni interprétés comme une trajectoire. Une correspondance repose sur
plusieurs positions et une étendue minimale, jamais sur un point isolé. Le
résultat reste une proposition à confirmer : ce module ne change aucun circuit.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import math
from statistics import median
import struct
from threading import RLock
from time import monotonic

from .circuits import CircuitLibrary
from .track_paths import TrackPaths

MAX_GEOMETRIES = 128
MAX_POINTS_PER_GEOMETRY = 24_000
MAX_TOTAL_POINTS = 400_000
MAX_POINTS_PER_CELL = 256


@dataclass(frozen=True)
class MatchResult:
    status: str
    candidate: str | None = None
    candidates: tuple[str, ...] = ()
    confidence: float = 0.0  # Cohérence géométrique, pas une probabilité calibrée.
    sample_count: int = 0
    mean_distance_m: float | None = None
    stable_candidate: str | None = None
    message: str = ""

    @property
    def ambiguous(self) -> bool:
        return self.status == "ambiguous"


class _Grid:
    """Index de points avec coût borné pour chaque voisinage consulté."""
    def __init__(self, coordinates: list[tuple[float, float]], radius_m: float):
        self.cell_size = radius_m
        self.cells: dict[tuple[int, int], list[tuple[float, float]]] = {}
        self.count = 0
        self.bounds = (min(x for x, _ in coordinates), min(z for _, z in coordinates),
                       max(x for x, _ in coordinates), max(z for _, z in coordinates))
        stride = max(1, math.ceil(len(coordinates) / MAX_POINTS_PER_GEOMETRY))
        occupied = set()
        for x, z in coordinates[::stride]:
            # Les bordures relevées très densément n'ont pas besoin de centaines
            # de points identiques à quelques centimètres pour cette suggestion.
            small_cell = (math.floor(x / 2.0), math.floor(z / 2.0))
            if small_cell in occupied:
                continue
            occupied.add(small_cell)
            key = (math.floor(x / self.cell_size), math.floor(z / self.cell_size))
            bucket = self.cells.setdefault(key, [])
            if len(bucket) < MAX_POINTS_PER_CELL:
                bucket.append((x, z))
                self.count += 1

    def distance(self, x: float, z: float, radius_m: float) -> float | None:
        xmin, zmin, xmax, zmax = self.bounds
        if x < xmin - radius_m or x > xmax + radius_m or z < zmin - radius_m or z > zmax + radius_m:
            return None
        cx, cz = math.floor(x / self.cell_size), math.floor(z / self.cell_size)
        best = radius_m * radius_m
        found = False
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for px, pz in self.cells.get((cx + dx, cz + dz), ()):
                    squared = (px - x) ** 2 + (pz - z) ** 2
                    if squared <= best:
                        best = squared
                        found = True
        return math.sqrt(best) if found else None


@dataclass
class _Geometry:
    names: tuple[str, ...]
    grid: _Grid


@dataclass(frozen=True)
class _Observation:
    x: float
    z: float
    distances: tuple[float | None, ...]


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _span(points: list[_Observation]) -> float:
    if not points:
        return 0.0
    return math.hypot(max(point.x for point in points) - min(point.x for point in points),
                      max(point.z for point in points) - min(point.z for point in points))


class TrackMatcher:
    """Reconnaissance prudente parmi les géométries disponibles hors ligne.

    ``observe`` doit recevoir les positions d'une seule voiture. Il conserve au
    plus 24 observations et ignore les répétitions trop proches. ``reload`` est
    nécessaire après une installation volontaire de nouveaux circuits. Ces deux
    opérations n'effectuent aucun accès réseau. Le chargement initial est un
    travail disque : pour préserver l'interface, le construire dans un worker.
    """
    def __init__(self, library: CircuitLibrary, *, min_samples: int = 6,
                 window_size: int = 24, min_span_m: float = 120.0,
                 min_step_m: float = 8.0, sample_interval_s: float = 0.25,
                 radius_m: float = 40.0, paths: TrackPaths | None = None):
        if type(min_samples) is not int or not 3 <= min_samples <= 24:
            raise ValueError("Il faut entre 3 et 24 observations minimales")
        if type(window_size) is not int or not min_samples <= window_size <= 48:
            raise ValueError("Fenêtre de reconnaissance incorrecte")
        for value in (min_span_m, min_step_m, radius_m):
            if _finite(value) is None or value <= 0:
                raise ValueError("Paramètre géométrique incorrect")
        if _finite(sample_interval_s) is None or not 0 <= sample_interval_s <= 60:
            raise ValueError("Cadence de reconnaissance incorrecte")
        self.library = library
        self.paths = paths if paths is not None else TrackPaths(index_path=getattr(library, "index_path", None))
        self.min_samples, self.window_size = min_samples, window_size
        self.min_span_m, self.min_step_m = float(min_span_m), float(min_step_m)
        self.sample_interval_s, self.radius_m = float(sample_interval_s), float(radius_m)
        self._lock = RLock()
        self._geometries: list[_Geometry] = []
        self._observations: deque[_Observation] = deque(maxlen=window_size)
        self._last_time: float | None = None
        self._stable_candidate: str | None = None
        self._last_result = MatchResult("waiting")
        self.skipped: tuple[str, ...] = ()
        self.reload()

    @property
    def loaded_groups(self) -> int:
        return len(self._geometries)

    @property
    def available_configurations(self) -> int:
        return len({name for geometry in self._geometries for name in geometry.names})

    def reload(self) -> None:
        """Recharge seulement les géométries locales ; ne télécharge rien."""
        with self._lock:
            by_file: dict[str, list[str]] = {}
            skipped = []
            for name in self.library.noms:
                bundle = self.library.entrees[name].get("bundle")
                if bundle is not None:
                    by_file.setdefault(bundle["file"], []).append(name)
            # L'empreinte canonique regroupe aussi deux fichiers différents qui
            # décrivent exactement les mêmes coordonnées, quel que soit l'ordre.
            geometries: list[_Geometry] = []
            by_digest: dict[bytes, _Geometry] = {}
            total_points = 0
            represented = set()

            def add(names, coordinates):
                nonlocal total_points
                expanded = set(names)
                for name in names:
                    expanded.update(alias for alias in self.paths.equivalent_names(name)
                                    if alias in self.library.entrees)
                coordinates = sorted(set(coordinates))
                digest = hashlib.sha256()
                for x, z in coordinates:
                    digest.update(struct.pack("<dd", 0.0 if x == 0 else x, 0.0 if z == 0 else z))
                signature = digest.digest()
                identical = by_digest.get(signature)
                if identical is not None:
                    identical.names = tuple(sorted(set(identical.names) | expanded))
                    represented.update(expanded)
                    return
                if len(geometries) >= MAX_GEOMETRIES:
                    skipped.extend(expanded)
                    return
                grid = _Grid(coordinates, self.radius_m)
                if total_points + grid.count > MAX_TOTAL_POINTS:
                    skipped.extend(expanded)
                    return
                geometry = _Geometry(tuple(sorted(expanded)), grid)
                geometries.append(geometry)
                by_digest[signature] = geometry
                total_points += grid.count
                represented.update(expanded)

            for names in by_file.values():
                try:
                    points = self.library.charger_points(names[0])
                except (OSError, ValueError, TypeError):
                    skipped.extend(names)
                    continue
                if points:
                    add(names, [(x, z) for x, z, _ in points])
            # Les lignes ont déjà leur ordre pour le dessin, mais cet index ne
            # déduit aucun sens de roulage. Les twins explicitement liés restent
            # toujours plusieurs choix, même avec des bordures plus détaillées.
            for name in self.library.noms:
                if name not in represented and (points := self.paths.load(name)):
                    add([name], points)
            self._geometries = geometries
            self.skipped = tuple(sorted(set(skipped) - represented))
            self.reset(keep_stable=True)

    def reset(self, *, keep_stable: bool = True) -> None:
        """Commence une nouvelle observation, sans changer la sélection du GUI."""
        with self._lock:
            self._observations.clear()
            self._last_time = None
            if not keep_stable:
                self._stable_candidate = None
            status = "waiting" if self._geometries else "unavailable"
            message = ("Roule sur une portion de circuit pour obtenir une suggestion."
                       if self._geometries else "Aucun tracé local disponible pour la reconnaissance.")
            self._last_result = MatchResult(status, stable_candidate=self._stable_candidate, message=message)

    def observe(self, x: float, z: float, *, now: float | None = None) -> MatchResult:
        """Propose une configuration uniquement lorsque les indices concordent.

        ``confidence`` décrit la cohérence avec les bordures locales et n'est
        pas une probabilité de reconnaissance. Une variante de même géométrie
        reste ambiguë : ``candidate`` vaut None et ``candidates`` donne les noms
        parmi lesquels l'utilisateur doit choisir.
        """
        px, pz = _finite(x), _finite(z)
        timestamp = monotonic() if now is None else _finite(now)
        with self._lock:
            if not self._geometries:
                return self._last_result
            if px is None or pz is None or timestamp is None or abs(px) > 10_000_000 or abs(pz) > 10_000_000:
                return MatchResult("waiting", sample_count=len(self._observations),
                                   stable_candidate=self._stable_candidate,
                                   message="Position absente ou incorrecte : aucune suggestion nouvelle.")
            if self._last_time is not None:
                elapsed = timestamp - self._last_time
                if elapsed < self.sample_interval_s:
                    return self._last_result
                if elapsed > 10:
                    self.reset(keep_stable=True)
            if self._observations:
                last = self._observations[-1]
                if math.hypot(px - last.x, pz - last.z) < self.min_step_m:
                    return self._last_result
            self._last_time = timestamp
            distances = tuple(geometry.grid.distance(px, pz, self.radius_m) for geometry in self._geometries)
            self._observations.append(_Observation(px, pz, distances))
            self._last_result = self._evaluate()
            return self._last_result

    def _evaluate(self) -> MatchResult:
        count = len(self._observations)
        base = dict(sample_count=count, stable_candidate=self._stable_candidate)
        if count < self.min_samples:
            return MatchResult("waiting", **base,
                               message=f"Observation locale : {count}/{self.min_samples} positions distinctes au minimum.")
        ranked = []
        for index, geometry in enumerate(self._geometries):
            nearby = [observation for observation in self._observations if observation.distances[index] is not None]
            coverage = len(nearby) / count
            # Quelques excursions ou un point aberrant sont tolérés ; une sortie
            # prolongée ou des données de circuits mélangées ne sont pas reconnues.
            if len(nearby) < self.min_samples or coverage < 0.80:
                continue
            distinct = {(math.floor(point.x / self.min_step_m), math.floor(point.z / self.min_step_m)) for point in nearby}
            if len(distinct) < self.min_samples or _span(nearby) < self.min_span_m:
                continue
            distances = [point.distances[index] for point in nearby]
            typical = median(distances)
            if typical > self.radius_m * 0.60:
                continue
            mean = sum(distances) / len(distances)
            confidence = max(0.0, min(1.0, coverage * (1 - typical / (self.radius_m * 1.5))))
            ranked.append((typical, mean, confidence, geometry))
        if not ranked:
            return MatchResult("no_match", **base,
                               message="Pas encore de correspondance fiable sur une portion assez étendue des tracés locaux.")
        ranked.sort(key=lambda item: (item[0], item[1]))
        best_distance, mean, confidence, best = ranked[0]
        competitors = [item[3] for item in ranked if item[0] - best_distance < 12.0]
        names = tuple(sorted({name for geometry in competitors for name in geometry.names}))
        if len(names) > 1:
            return MatchResult("ambiguous", candidates=names, confidence=confidence,
                               mean_distance_m=mean, **base,
                               message="Géométrie compatible avec plusieurs configurations : choisis la configuration manuellement.")
        if confidence < 0.75:
            return MatchResult("waiting", candidates=names, confidence=confidence,
                               mean_distance_m=mean, **base,
                               message="Correspondance encore fragile : poursuis le roulage et confirme le circuit manuellement.")
        self._stable_candidate = names[0]
        return MatchResult("candidate", candidate=names[0], candidates=names,
                           confidence=confidence, sample_count=count, mean_distance_m=mean,
                           stable_candidate=self._stable_candidate,
                           message=f"Tracé probable : {names[0]}. Confirme la configuration avant de l'appliquer.")
