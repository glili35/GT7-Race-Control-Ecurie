"""Catalogue GT7 et téléchargement volontaire de points de bordure.

Les relevés de bordure ne sont pas des polylignes ordonnées. Un affichage doit
les dessiner comme des points, sauf s'il dispose d'un ordre géométrique vérifié.
Aucun accès réseau ni création de dossier n'a lieu à la construction.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from threading import Event, RLock
from urllib.parse import urlsplit
from urllib.request import urlopen

SOURCE = "https://raw.githubusercontent.com/jbhoorasingh/gt7-datalogger-track-data/main/"
CACHE = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache")) / "GT7-Race-Control-Ecurie" / "circuits"
INDEX = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "circuits_gt7" / "index.json"
MAX_DOWNLOAD_BYTES = 12_000_000
MAX_INDEX_BYTES = 2_000_000
MAX_EDGE_POINTS = 100_000
_BUNDLE_PATH = re.compile(r"tracks/[a-z0-9][a-z0-9_.-]*\.json\Z", re.ASCII)


@dataclass(frozen=True)
class CornerPoint:
    """Ancre numérotée fournie par le bundle, en coordonnées GT7 X/Z.

    Il s'agit d'une annotation de la bibliothèque communautaire ; la présence
    de cette ancre ne certifie pas une numérotation officielle du circuit.
    """

    n: int
    name: str
    x: float
    z: float


@dataclass(frozen=True)
class BulkInstallResult:
    """Bilan par nom de configuration, pas une promesse de couverture complète."""

    installed: tuple[str, ...]
    already_present: tuple[str, ...]
    failed: dict[str, str]
    cancelled: bool


class CircuitLibraryError(ValueError):
    """Erreur exploitable par l'interface, sans supposer le catalogue présent."""


def _bundle_file(bundle: object) -> str:
    if not isinstance(bundle, dict):
        raise CircuitLibraryError("Description de tracé incorrecte dans le catalogue.")
    filename = bundle.get("file")
    if not isinstance(filename, str) or not _BUNDLE_PATH.fullmatch(filename) or ".." in filename:
        raise CircuitLibraryError("Chemin de circuit non valide dans le catalogue.")
    return filename


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise CircuitLibraryError("Fichier de circuit trop volumineux.")
    return data


def _parse_points(content: bytes) -> list[tuple[float, float, str]]:
    try:
        data = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise CircuitLibraryError("Le fichier de circuit ne contient pas un JSON valide.") from exc
    if not isinstance(data, dict):
        raise CircuitLibraryError("Le fichier de circuit doit contenir un objet JSON.")
    edges = data.get("edges")
    if not isinstance(edges, list) or not edges or len(edges) > MAX_EDGE_POINTS:
        raise CircuitLibraryError("Les points de bordure du circuit sont absents ou trop nombreux.")
    points = []
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("side") not in ("L", "R"):
            raise CircuitLibraryError("Un point de bordure possède un côté incorrect.")
        coords = []
        for axis in ("x", "z"):
            value = edge.get(axis)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CircuitLibraryError("Un point de bordure possède une coordonnée incorrecte.")
            try:
                number = float(value)
            except (ValueError, OverflowError) as exc:
                raise CircuitLibraryError("Coordonnée de circuit hors limites.") from exc
            if not math.isfinite(number) or abs(number) > 10_000_000:
                raise CircuitLibraryError("Coordonnée de circuit non finie ou hors limites.")
            coords.append(number)
        points.append((coords[0], coords[1], edge["side"]))
    return points


def _parse_corners(content: bytes) -> tuple[CornerPoint, ...]:
    """Valide les annotations facultatives indépendamment des bordures."""
    try:
        data = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise CircuitLibraryError("Le fichier de circuit ne contient pas un JSON valide.") from exc
    if not isinstance(data, dict):
        raise CircuitLibraryError("Le fichier de circuit doit contenir un objet JSON.")
    corners = data.get("corners")
    if corners is None:
        return ()
    if not isinstance(corners, list) or len(corners) > 1000:
        raise CircuitLibraryError("La liste des virages est incorrecte ou trop longue.")
    result = []
    numbers = set()
    for corner in corners:
        if not isinstance(corner, dict):
            raise CircuitLibraryError("Une annotation de virage est incorrecte.")
        number = corner.get("n")
        if type(number) is not int or not 1 <= number <= 1000 or number in numbers:
            raise CircuitLibraryError("Les numéros de virage doivent être positifs et uniques.")
        name = corner.get("name", "")
        if not isinstance(name, str) or len(name) > 250:
            raise CircuitLibraryError("Un nom de virage est incorrect.")
        apex = corner.get("apex")
        if not isinstance(apex, dict):
            raise CircuitLibraryError("Une ancre de virage est absente ou incorrecte.")
        coordinates = []
        for axis in ("x", "z"):
            value = apex.get(axis)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CircuitLibraryError("Une ancre de virage possède une coordonnée incorrecte.")
            try:
                coordinate = float(value)
            except (ValueError, OverflowError) as exc:
                raise CircuitLibraryError("Coordonnée de virage hors limites.") from exc
            if not math.isfinite(coordinate) or abs(coordinate) > 10_000_000:
                raise CircuitLibraryError("Coordonnée de virage non finie ou hors limites.")
            coordinates.append(coordinate)
        numbers.add(number)
        result.append(CornerPoint(number, name.strip(), coordinates[0], coordinates[1]))
    return tuple(sorted(result, key=lambda corner: corner.n))


class CircuitLibrary:
    def __init__(self, cache_dir: Path | str | None = None, index_path: Path | str | None = None):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else CACHE
        self.index_path = Path(index_path) if index_path is not None else INDEX
        self.entrees: dict[str, dict] = {}
        self.noms: list[str] = []
        self.erreur_catalogue: str | None = None
        self._erreurs_cache: dict[str, str] = {}
        self._lock = RLock()
        self._installation_lock = RLock()
        try:
            data = json.loads(_read_bounded(self.index_path, MAX_INDEX_BYTES))
            configurations = data.get("configurations") if isinstance(data, dict) else None
            if not isinstance(configurations, list) or not configurations or len(configurations) > 2048:
                raise CircuitLibraryError("Liste des configurations absente ou incorrecte.")
            entries = {}
            for item in configurations:
                if not isinstance(item, dict):
                    raise CircuitLibraryError("Une configuration du catalogue est incorrecte.")
                name = item.get("official_name")
                if not isinstance(name, str) or not name.strip() or len(name) > 250 or name in entries:
                    raise CircuitLibraryError("Nom de circuit absent, incorrect ou dupliqué.")
                bundle = item.get("bundle")
                if bundle is not None:
                    _bundle_file(bundle)
                entries[name] = item
            self.entrees = entries
            self.noms = sorted(entries)
        except (OSError, ValueError, UnicodeError, TypeError) as exc:
            self.erreur_catalogue = f"Catalogue des circuits indisponible : {exc}"

    def _entry(self, nom: str) -> dict:
        if self.erreur_catalogue:
            raise CircuitLibraryError(self.erreur_catalogue)
        if not isinstance(nom, str) or nom not in self.entrees:
            raise CircuitLibraryError("Circuit inconnu : sélectionne un circuit du catalogue.")
        return self.entrees[nom]

    def est_disponible(self, nom: str) -> bool:
        if self.erreur_catalogue:
            return False
        return self._entry(nom).get("bundle") is not None

    def chemin(self, nom: str) -> Path | None:
        if self.erreur_catalogue:
            return None
        entry = self._entry(nom)
        if entry.get("bundle") is None:
            return None
        relative = _bundle_file(entry["bundle"])
        path = self.cache_dir / relative
        # Un cache redirigé par un lien symbolique ne doit pas atteindre un autre
        # dossier que celui choisi par l'appelant.
        if not path.resolve().is_relative_to(self.cache_dir.resolve()):
            raise CircuitLibraryError("Le chemin du cache sort du dossier des circuits.")
        return path

    def erreur_cache(self, nom: str) -> str | None:
        """Dernière erreur de lecture locale, à afficher près du bouton installer."""
        with self._lock:
            return self._erreurs_cache.get(nom)

    def _remember_error(self, nom: str, message: str | None) -> None:
        with self._lock:
            if message is None:
                self._erreurs_cache.pop(nom, None)
            else:
                self._erreurs_cache[nom] = message

    def charger_points(self, nom: str) -> list[tuple[float, float, str]]:
        """Points (x, z, côté L/R) non ordonnés ; aucune ligne n'est déduite.

        Un cache absent renvoie []. Un cache incorrect renvoie également [] et
        renseigne erreur_cache(nom), pour permettre une nouvelle installation.
        Un nom inconnu dans un catalogue valide déclenche une erreur lisible.
        """
        path = self.chemin(nom)
        if path is None:
            return []
        try:
            points = _parse_points(_read_bounded(path, MAX_DOWNLOAD_BYTES))
        except FileNotFoundError:
            self._remember_error(nom, None)
            return []
        except (OSError, ValueError, TypeError) as exc:
            self._remember_error(nom, f"Cache du circuit incorrect : {exc}")
            return []
        self._remember_error(nom, None)
        return points

    def charger_virages(self, nom: str) -> tuple[CornerPoint, ...]:
        """Lit seulement les ancres numérotées présentes dans le cache local.

        Aucun numéro n'est déduit de la courbure ni du compteur GT7. Les caches
        anciens sans annotations restent compatibles. Des annotations invalides
        renvoient () et renseignent erreur_cache(nom), sans rendre les bordures
        inutilisables. Cette méthode ne télécharge et ne modifie aucun fichier.
        """
        path = self.chemin(nom)
        if path is None:
            return ()
        try:
            content = _read_bounded(path, MAX_DOWNLOAD_BYTES)
            _parse_points(content)
            corners = _parse_corners(content)
        except FileNotFoundError:
            self._remember_error(nom, None)
            return ()
        except (OSError, ValueError, TypeError) as exc:
            self._remember_error(nom, f"Annotations des virages indisponibles : {exc}")
            return ()
        self._remember_error(nom, None)
        return corners

    def est_installe(self, nom: str) -> bool:
        """Vrai seulement si le cache contient réellement des points exploitables."""
        return bool(self.charger_points(nom))

    def installer(self, nom: str) -> None:
        """Télécharge explicitement un tracé, le valide puis remplace le cache.

        Le fichier final n'est jamais remplacé par une réponse partielle ou
        invalide. Les appels simultanés sur la même bibliothèque sont sérialisés.
        """
        with self._installation_lock:
            if self.erreur_catalogue:
                raise CircuitLibraryError(self.erreur_catalogue)
            path = self.chemin(nom)
            if path is None:
                raise CircuitLibraryError("Tracé géométrique non disponible pour ce circuit.")
            if self.est_installe(nom):
                return
            relative = _bundle_file(self._entry(nom)["bundle"])
            with urlopen(SOURCE + relative, timeout=30) as response:
                destination = urlsplit(response.geturl())
                if destination.scheme != "https" or destination.hostname != "raw.githubusercontent.com":
                    raise CircuitLibraryError("Le téléchargement du circuit a été redirigé vers une source inattendue.")
                size = response.headers.get("Content-Length")
                if size is not None:
                    try:
                        length = int(size)
                    except (ValueError, TypeError) as exc:
                        raise CircuitLibraryError("Taille de téléchargement incorrecte.") from exc
                    if length < 0 or length > MAX_DOWNLOAD_BYTES:
                        raise CircuitLibraryError("Circuit trop volumineux.")
                content = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(content) > MAX_DOWNLOAD_BYTES:
                raise CircuitLibraryError("Circuit trop volumineux.")
            _parse_points(content)
            temporary: Path | None = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=path.stem + "-", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self._remember_error(nom, None)

    def installer_tous(
        self,
        progress: Callable[[int, int, str, str], None] | None = None,
        cancel: Event | None = None,
    ) -> BulkInstallResult:
        """Installe volontairement les tracés disponibles, sans toucher aux autres.

        À appeler dans un worker, jamais dans le thread graphique. ``progress``
        est appelé au plus une fois par configuration terminée avec le statut
        « prêt », « installé » ou « échec ». Ses erreurs n'annulent pas le lot.
        L'annulation est prise en compte entre les fichiers ; un téléchargement
        déjà commencé peut prendre jusqu'au délai réseau de ``installer``.
        Plusieurs configurations partageant un bundle ne le téléchargent qu'une
        fois. Les configurations sans géométrie ne sont jamais téléchargées.
        """
        if self.erreur_catalogue:
            raise CircuitLibraryError(self.erreur_catalogue)
        grouped: dict[str, list[str]] = {}
        for name in self.noms:
            bundle = self._entry(name).get("bundle")
            if bundle is not None:
                grouped.setdefault(_bundle_file(bundle), []).append(name)
        total = sum(len(names) for names in grouped.values())
        installed: list[str] = []
        already_present: list[str] = []
        failed: dict[str, str] = {}
        completed = 0
        cancelled = False
        for names in grouped.values():
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            try:
                ready = self.est_installe(names[0])
                if ready:
                    already_present.extend(names)
                    status = "prêt"
                else:
                    self.installer(names[0])
                    installed.extend(names)
                    status = "installé"
            except Exception as exc:
                # Une source ou un cache défectueux ne doit pas empêcher les
                # autres circuits de s'installer. Pas d'interception de Ctrl+C.
                reason = str(exc) or type(exc).__name__
                failed.update((name, reason) for name in names)
                status = "échec"
            for name in names:
                completed += 1
                if progress is not None:
                    try:
                        progress(completed, total, name, status)
                    except Exception:
                        pass
        return BulkInstallResult(tuple(installed), tuple(already_present), failed, cancelled)

    def charger(self, nom: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """Compatibilité historique : listes gauche/droite, sans ordre garanti."""
        points = self.charger_points(nom)
        return ([(x, z) for x, z, side in points if side == "L"],
                [(x, z) for x, z, side in points if side == "R"])
