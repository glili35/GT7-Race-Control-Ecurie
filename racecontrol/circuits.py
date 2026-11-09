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
class CornerSet:
    """Repères d'une configuration exacte, avec leur provenance.

    ``documented`` désigne un jeu documenté par sa source, sans certification
    officielle. ``partial`` signale une couverture partielle ou non confirmée ;
    ``missing`` et ``invalid`` ne fournissent aucun repère à dessiner.
    """

    points: tuple[CornerPoint, ...]
    status: str
    source: str
    revision: str
    note: str = ""


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
    return _corner_points(data.get("corners"))


def _corner_points(corners: object) -> tuple[CornerPoint, ...]:
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
    def __init__(self, cache_dir: Path | str | None = None, index_path: Path | str | None = None,
                 overlay_path: Path | str | None = None):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else CACHE
        self.index_path = Path(index_path) if index_path is not None else INDEX
        self.overlay_path = Path(overlay_path) if overlay_path is not None else self.index_path.with_name("corners.json")
        self.entrees: dict[str, dict] = {}
        self.noms: list[str] = []
        self.erreur_catalogue: str | None = None
        self._erreurs_cache: dict[str, str] = {}
        self._erreurs_virages: dict[str, str] = {}
        self._corner_overlay: dict[str, object] = {}
        self._corner_revision = ""
        self._corner_overlay_error: str | None = None
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
        self._read_corner_overlay()

    def _read_corner_overlay(self) -> None:
        """Lit une ressource facultative ; ne crée ni cache ni connexion."""
        try:
            data = json.loads(_read_bounded(self.overlay_path, MAX_INDEX_BYTES))
            if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                    or data["schema_version"] != 1):
                raise CircuitLibraryError("Format des annotations embarquées incorrect.")
            revision, entries = data.get("revision"), data.get("configurations")
            if not isinstance(revision, str) or not revision.strip() or len(revision) > 128:
                raise CircuitLibraryError("Révision des annotations embarquées incorrecte.")
            if not isinstance(entries, dict) or len(entries) > 2048:
                raise CircuitLibraryError("Liste des annotations embarquées incorrecte.")
            if any(not isinstance(key, str) or not key or len(key) > 128 for key in entries):
                raise CircuitLibraryError("Identifiant de configuration annotée incorrect.")
            self._corner_revision = revision
            self._corner_overlay = entries
        except FileNotFoundError:
            pass  # Un catalogue historique sans overlay reste utilisable.
        except (OSError, ValueError, UnicodeError, TypeError) as exc:
            self._corner_overlay_error = f"Annotations des virages indisponibles : {exc}"

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
            return self._erreurs_cache.get(nom) or self._erreurs_virages.get(nom)

    def _remember_corner_error(self, nom: str, message: str | None) -> None:
        with self._lock:
            if message is None:
                self._erreurs_virages.pop(nom, None)
            else:
                self._erreurs_virages[nom] = message

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
        """Compatibilité : repères exacts, sans téléchargement ni modification."""
        return self.annotations_virages(nom).points

    def annotations_virages(self, nom: str) -> CornerSet:
        """Préfère les annotations embarquées propres à l'identifiant GT7.

        Un overlay explicite remplace les annotations du cache historique,
        même s'il indique leur absence. Aucun repère ni sens inverse n'est
        déduit de la géométrie. Une erreur n'invalide jamais les bordures.
        """
        if self.erreur_catalogue:
            return CornerSet((), "missing", "", "")
        entry = self._entry(nom)
        if self._corner_overlay_error:
            self._remember_corner_error(nom, self._corner_overlay_error)
            return CornerSet((), "invalid", "", self._corner_revision)
        identifier = entry.get("official_id")
        if isinstance(identifier, str) and identifier in self._corner_overlay:
            try:
                result = self._overlay_corners(self._corner_overlay[identifier])
            except (ValueError, TypeError) as exc:
                self._remember_corner_error(nom, f"Annotations des virages indisponibles : {exc}")
                return CornerSet((), "invalid", "", self._corner_revision)
            self._remember_corner_error(nom, None)
            return result
        return self._cached_corners(nom, entry)

    def _overlay_corners(self, item: object) -> CornerSet:
        if not isinstance(item, dict):
            raise CircuitLibraryError("Jeu de virages incorrect.")
        status, source, raw_points = item.get("status"), item.get("source"), item.get("corners")
        if status not in ("documented", "partial", "missing"):
            raise CircuitLibraryError("Statut des virages incorrect.")
        if (not isinstance(source, str) or len(source) > 2048
                or (status != "missing" and not source.strip())):
            raise CircuitLibraryError("Source des virages incorrecte.")
        if not isinstance(raw_points, list):
            raise CircuitLibraryError("Liste des virages incorrecte.")
        points = _corner_points(raw_points)
        if (status == "missing") != (not points):
            raise CircuitLibraryError("Le statut et les repères de virage sont incohérents.")
        expected = item.get("expected_turns")
        if expected is not None and (type(expected) is not int or not 1 <= expected <= 1000):
            raise CircuitLibraryError("Nombre de virages annoncé incorrect.")
        note = item.get("note", "")
        if not isinstance(note, str) or len(note) > 2000:
            raise CircuitLibraryError("Note sur les virages incorrecte.")
        return CornerSet(points, status, source.strip(), self._corner_revision, note.strip())

    def _cached_corners(self, nom: str, entry: dict) -> CornerSet:
        """Repli historique ; un retour n'hérite jamais des repères de l'aller."""
        path = self.chemin(nom)
        if path is None:
            self._remember_corner_error(nom, None)
            return CornerSet((), "missing", "", "")
        try:
            content = _read_bounded(path, MAX_DOWNLOAD_BYTES)
            _parse_points(content)
            corners = _parse_corners(content)
            data = json.loads(content)
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            official = meta.get("official") if isinstance(meta.get("official"), dict) else {}
            cached_id, wanted_id = official.get("official_id"), entry.get("official_id")
            if cached_id and wanted_id and cached_id != wanted_id:
                raise CircuitLibraryError("Ces repères appartiennent à une autre configuration.")
            if corners and entry.get("reverse") is True:
                if not wanted_id or cached_id != wanted_id or official.get("reverse") is not True:
                    self._remember_corner_error(nom, None)
                    return CornerSet((), "missing", "", "")
            elif corners and official.get("reverse") is True:
                raise CircuitLibraryError("Ces repères appartiennent à un autre sens du circuit.")
        except FileNotFoundError:
            self._remember_corner_error(nom, None)
            return CornerSet((), "missing", "", "")
        except (OSError, ValueError, TypeError) as exc:
            self._remember_corner_error(nom, f"Annotations des virages indisponibles : {exc}")
            return CornerSet((), "invalid", "", "")
        self._remember_corner_error(nom, None)
        if not corners:
            return CornerSet((), "missing", "", "")
        revision = meta.get("updated_at", "")
        revision = revision if isinstance(revision, str) and len(revision) <= 128 else ""
        return CornerSet(corners, "partial", "Cache local historique", revision)

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
