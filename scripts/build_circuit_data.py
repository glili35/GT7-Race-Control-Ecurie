"""Reconstruit les données embarquées depuis un relevé local de la source.

Aucun accès réseau : --source-dir doit contenir l'index, signatures.json,
les bundles tracks/ et les deux notices d'origine. Le commit est conservé
dans les fichiers générés et les URL de provenance.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re


UPSTREAM = "https://raw.githubusercontent.com/jbhoorasingh/gt7-datalogger-track-data"
MAX_PATH_GAP_M = 100.0
MAX_CORNER_EDGE_DISTANCE_M = 50.0
NOTES = {
    "46b3d9": "7 repères documentés ; le catalogue en déclare 8. Aucun numéro ajouté.",
    "3c4be1": "19 repères documentés, cohérents avec le plan Spa validé ; le catalogue emploie 21. Numérotation conservée.",
    "4bf61b": "14 repères documentés ; le catalogue emploie 13. Convention communautaire conservée, sans certification officielle.",
    "6e378b": "19 repères documentés ; le catalogue emploie 20. Aucun numéro ajouté.",
    "f18da2": "12 repères documentés ; le catalogue emploie 8. Convention communautaire conservée, sans certification officielle.",
}


def read_json(path: Path):
    if path.stat().st_size > 12_000_000:
        raise ValueError(f"Fichier source trop volumineux : {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def coordinates(point):
    if (not isinstance(point, (list, tuple)) or len(point) != 2
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   or abs(value) > 10_000_000 for value in point)):
        raise ValueError("Coordonnées de référence incorrectes")
    return list(point)


def build(source: Path, output: Path, revision: str) -> dict:
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Un commit source SHA-1 complet est obligatoire")
    source_url = f"{UPSTREAM}/{revision}"
    index = read_json(source / "index.json")
    entries = index["configurations"]
    by_id = {entry["official_id"]: entry for entry in entries}
    if len(by_id) != len(entries) or any(re.fullmatch(r"[0-9a-f]{6}", key) is None for key in by_id):
        raise ValueError("Les identifiants du catalogue doivent être uniques")

    annotations = {}
    for entry in entries:
        identifier = entry["official_id"]
        bundle = entry.get("bundle")
        row = {"status": "missing", "source": source_url + "/index.json",
               "expected_turns": entry["turns"], "corners": []}
        if bundle is not None:
            filename = bundle["file"]
            if re.fullmatch(r"tracks/[a-z0-9][a-z0-9_.-]*\.json", filename) is None or ".." in filename:
                raise ValueError("Chemin de bundle incorrect")
            data = read_json(source / filename)
            if data["meta"]["official"]["official_id"] != identifier:
                raise ValueError("Le bundle appartient à une autre configuration")
            row["source"] = source_url + "/" + filename
            source_corners = data.get("corners", [])
            edge_points = ([coordinates([edge["x"], edge["z"]]) for edge in data.get("edges", [])]
                           if source_corners else [])
            if source_corners and not edge_points:
                raise ValueError(f"Les virages de {entry['official_name']} n'ont aucune bordure de référence")
            numbers = set()
            for corner in source_corners:
                number, name = corner["n"], corner.get("name", "")
                if type(number) is not int or not 1 <= number <= 1000 or number in numbers:
                    raise ValueError("Numéro de virage incorrect ou dupliqué")
                if not isinstance(name, str) or len(name) > 250:
                    raise ValueError("Nom de virage incorrect")
                x, z = coordinates([corner["apex"]["x"], corner["apex"]["z"]])
                distance = min(math.hypot(x - edge[0], z - edge[1]) for edge in edge_points)
                if distance > MAX_CORNER_EDGE_DISTANCE_M:
                    raise ValueError(
                        f"L'ancre du virage {number} de {entry['official_name']} est à {distance:.1f} m "
                        f"de ses bordures (maximum {MAX_CORNER_EDGE_DISTANCE_M:.0f} m)"
                    )
                row["corners"].append({"n": number, "name": name, "apex": {"x": x, "z": z}})
                numbers.add(number)
            row["corners"].sort(key=lambda value: value["n"])
            if row["corners"]:
                row["status"] = "documented" if len(row["corners"]) == entry["turns"] else "partial"
                if row["status"] == "partial":
                    row["note"] = NOTES.get(identifier, "Le nombre de repères diffère du catalogue ; numérotation source conservée.")
        annotations[identifier] = row

    signatures = read_json(source / "signatures.json")
    paths, excluded = [], []
    seen = set()
    for signature in signatures["signatures"]:
        identifier = signature["official_id"]
        entry = by_id[identifier]
        raw_path = signature.get("path")
        flags = signature.get("flags", [])
        if not isinstance(flags, list):
            raise ValueError("Drapeaux de qualité incorrects")
        if flags or not raw_path:
            excluded.append({"official_id": identifier, "reason": ", ".join(flags) if flags else "path_absent"})
            continue
        path = [coordinates(point) for point in raw_path]
        if not 3 <= len(path) <= 100_000:
            raise ValueError("Nombre de points incorrect")
        if any(math.dist(a, b) > MAX_PATH_GAP_M for a, b in zip(path, path[1:])):
            excluded.append({"official_id": identifier, "reason": "path_gap_over_100m"})
            continue
        if entry["official_name"] != signature["official_name"] or entry["reverse"] is not False:
            raise ValueError("La signature ne correspond pas à sa configuration directe")
        ambiguous = signature.get("ambiguous_with", [])
        if not isinstance(ambiguous, list) or any(value not in by_id for value in ambiguous):
            raise ValueError("Références ambiguës incorrectes")
        row = {"official_id": identifier, "official_name": entry["official_name"],
               "reverse": False, "path": path, "provenance": signature["provenance"],
               "ambiguous_with": ambiguous}
        if identifier in seen:
            raise ValueError("Configuration de ligne dupliquée")
        paths.append(row)
        seen.add(identifier)
        reverse = signature.get("reverse")
        if reverse is not None:
            reverse_id = reverse["official_id"]
            reverse_entry = by_id[reverse_id]
            if (reverse_entry["reverse"] is not True or reverse_entry["official_name"] != reverse["official_name"]
                    or reverse_id in seen):
                raise ValueError("Configuration inverse incorrecte ou dupliquée")
            paths.append({**row, "official_id": reverse_id, "official_name": reverse_entry["official_name"],
                          "reverse": True, "reverse_of_id": identifier, "path": list(reversed(path))})
            seen.add(reverse_id)

    output.mkdir(parents=True, exist_ok=True)
    (output / "index.json").write_bytes((source / "index.json").read_bytes())
    (output / "corners.json").write_text(json.dumps({
        "schema_version": 1, "revision": revision, "configurations": annotations,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "track_paths.json").write_text(json.dumps({
        "format": "gt7-race-control-track-paths", "version": 1, "revision": revision,
        "source": source_url + "/signatures.json", "paths": paths,
    }, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    for original, filename in (("LICENSE", "LICENSE-CC0.txt"),
                               ("vendor/gt-telemetry-LICENSE", "LICENSE-MIT-reference-laps.txt")):
        (output / filename).write_bytes((source / original).read_bytes())
    return {"configurations": len(entries), "surveyed": sum(entry.get("bundle") is not None for entry in entries),
            "annotated": sum(bool(row["corners"]) for row in annotations.values()),
            "anchors": sum(len(row["corners"]) for row in annotations.values()),
            "documented": sum(row["status"] == "documented" for row in annotations.values()),
            "partial": sum(row["status"] == "partial" for row in annotations.values()),
            "paths": len(paths), "excluded_signatures": excluded}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    arguments = parser.parse_args()
    print(json.dumps(build(arguments.source_dir, arguments.output_dir, arguments.revision), ensure_ascii=False))


if __name__ == "__main__":
    main()
