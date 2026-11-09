"""Trajectoires GT7 embarquées, lues hors ligne par identifiant de configuration.

Ces lignes sont des relevés de roulage ordonnés, pas des bordures ni des ancres
de virage. Leurs extrémités ne sont jamais reliées automatiquement. Une variante
inversée n'hérite d'un relevé que si le pack la relie explicitement à sa source.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import sys

from .circuits import INDEX

PATHS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "circuits_gt7" / "track_paths.json"
MAX_PATH_BYTES = 5_000_000
MAX_INDEX_BYTES = 2_000_000
MAX_CONFIGURATIONS = 2048
MAX_PATH_POINTS = 24_000
MAX_TOTAL_POINTS = 400_000
MAX_STEP_M = 100.0
_ID = re.compile(r"[0-9a-f]{6}\Z", re.ASCII)


class TrackPathsError(ValueError):
    """Le pack embarqué est absent, incohérent ou inutilisable."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TrackPathsError("Une clé du pack est dupliquée.")
        result[key] = value
    return result


def _read_json(path: Path, maximum: int):
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise TrackPathsError("Fichier de trajectoires trop volumineux.")
    return json.loads(content, object_pairs_hook=_unique_object)


def _id(value):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise TrackPathsError("Identifiant de configuration incorrect.")
    return value


def _points(raw):
    if not isinstance(raw, list) or not 4 <= len(raw) <= MAX_PATH_POINTS:
        raise TrackPathsError("Trajectoire absente ou de longueur incorrecte.")
    points = []
    for point in raw:
        if not isinstance(point, list) or len(point) != 2:
            raise TrackPathsError("Point de trajectoire incorrect.")
        coordinates = []
        for value in point:
            if type(value) not in (int, float):
                raise TrackPathsError("Coordonnée de trajectoire incorrecte.")
            try:
                coordinate = float(value)
            except (OverflowError, ValueError):
                raise TrackPathsError("Coordonnée de trajectoire hors limites.") from None
            if not math.isfinite(coordinate) or abs(coordinate) > 10_000_000:
                raise TrackPathsError("Coordonnée de trajectoire hors limites.")
            coordinates.append(coordinate)
        current = tuple(coordinates)
        if points and math.dist(points[-1], current) > MAX_STEP_M:
            raise TrackPathsError("Trajectoire interrompue par un saut de position.")
        points.append(current)
    if len(set(points)) < 4:
        raise TrackPathsError("Trajectoire sans positions distinctes suffisantes.")
    return tuple(points)


class TrackPaths:
    """Lecture seule et bornée ; aucune requête réseau ni écriture de profil.

    Les noms viennent exclusivement du catalogue fourni. Une donnée qui ne
    correspond pas au nom, à l'identifiant et au sens de ce catalogue n'est pas
    appliquée à une autre variante. Un pack invalide laisse les bordures de la
    bibliothèque utilisables et expose son erreur via ``error``.
    """

    def __init__(self, index_path=None, paths_path=None):
        self.index_path = Path(index_path) if index_path is not None else INDEX
        self.paths_path = Path(paths_path) if paths_path is not None else PATHS
        self.error = None
        self.names: tuple[str, ...] = ()
        self._paths = {}
        self._equivalents = {}
        try:
            self._load()
        except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
            self.error = "Trajectoires embarquées indisponibles : pack absent ou incohérent."

    def _load(self):
        index = _read_json(self.index_path, MAX_INDEX_BYTES)
        configurations = index.get("configurations") if isinstance(index, dict) else None
        if not isinstance(configurations, list) or not 1 <= len(configurations) <= MAX_CONFIGURATIONS:
            raise TrackPathsError("Catalogue des configurations incorrect.")
        by_id, by_name = {}, set()
        for entry in configurations:
            if not isinstance(entry, dict):
                raise TrackPathsError("Configuration de circuit incorrecte.")
            identifier = entry.get("official_id")
            if identifier is None:
                continue  # Compatibilité avec les anciens catalogues sans ID.
            identifier = _id(identifier)
            name, reverse = entry.get("official_name"), entry.get("reverse", False)
            if (not isinstance(name, str) or not name.strip() or len(name) > 250
                    or name in by_name or identifier in by_id or type(reverse) is not bool):
                raise TrackPathsError("Configuration de circuit ambiguë ou incorrecte.")
            by_id[identifier] = (name, reverse)
            by_name.add(name)
        data = _read_json(self.paths_path, MAX_PATH_BYTES)
        if (not isinstance(data, dict) or data.get("format") != "gt7-race-control-track-paths"
                or type(data.get("version")) is not int or data["version"] != 1):
            raise TrackPathsError("Format de trajectoires incorrect.")
        rows = data.get("paths")
        if not isinstance(rows, list) or len(rows) > MAX_CONFIGURATIONS:
            raise TrackPathsError("Liste de trajectoires incorrecte.")
        records, seen, total = {}, set(), 0
        for row in rows:
            if not isinstance(row, dict):
                raise TrackPathsError("Description de trajectoire incorrecte.")
            identifier = _id(row.get("official_id"))
            if identifier in seen:
                raise TrackPathsError("Identifiant de trajectoire dupliqué.")
            seen.add(identifier)
            if identifier not in by_id:
                raise TrackPathsError("Trajectoire sans configuration correspondante.")
            name, reverse = by_id[identifier]
            if (row.get("official_name") != name or type(row.get("reverse")) is not bool
                    or row["reverse"] != reverse or row.get("provenance") not in ("survey", "capture")):
                raise TrackPathsError("Trajectoire rattachée à une autre configuration.")
            if row.get("flags") or not row.get("path"):
                continue  # Une boîte englobante ou un relevé signalé n'est pas une carte.
            points = _points(row["path"])
            total += len(points)
            if total > MAX_TOTAL_POINTS:
                raise TrackPathsError("Le pack contient trop de points.")
            source = _id(row["reverse_of_id"]) if "reverse_of_id" in row else None
            records[identifier] = (name, reverse, points, source)
        groups = {identifier: {record[0]} for identifier, record in records.items()}
        for identifier, (name, reverse, points, source) in records.items():
            if source is None:
                continue
            original = records.get(source)
            if (not reverse or original is None or original[1] or original[3] is not None
                    or points != tuple(reversed(original[2]))):
                raise TrackPathsError("Référence de variante inversée incorrecte.")
            groups[source].add(name)
        paths, equivalents = {}, {}
        for identifier, (name, _, points, source) in records.items():
            group = tuple(sorted(groups[source or identifier]))
            paths[name] = points
            equivalents[name] = group
        self._paths, self._equivalents = paths, equivalents
        self.names = tuple(sorted(paths))

    def load(self, name: str):
        """Trajectoire ordonnée de cette configuration exacte, ou ``None``."""
        return self._paths.get(name) if isinstance(name, str) else None

    def equivalent_names(self, name: str) -> tuple[str, ...]:
        """Variantes liées explicitement ; aucune direction n'est reconnue ici."""
        return self._equivalents.get(name, ()) if isinstance(name, str) else ()
