"""Préservation des sessions même lorsque l'horloge ne progresse pas."""
from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime as RealDateTime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from racecontrol.telemetry.csv_recorder import GT7CSVRecorder


class FrozenDateTime:
    @classmethod
    def now(cls):
        return RealDateTime(2026, 11, 9, 12, 0, 0)


def sample(speed):
    values = {field: 0 for field in GT7CSVRecorder.FIELDNAMES}
    values.update(packet_format="A", speed_kmh=speed, tyre_temperatures=(65, 66, 67, 68))
    return SimpleNamespace(**values)


class CSVRecorderTests(unittest.TestCase):
    def test_same_timestamp_creates_distinct_sessions_and_preserves_first_data(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("racecontrol.telemetry.csv_recorder.datetime", FrozenDateTime):
            directory = Path(temporary)
            with GT7CSVRecorder(directory) as first:
                first.record(sample(123))
                first_path = first.file_path
            first_bytes = first_path.read_bytes()
            with GT7CSVRecorder(directory) as second:
                second.record(sample(234))
                second_path = second.file_path
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(len(list(directory.glob("session_*.csv"))), 2)
            self.assertEqual(first_path.read_bytes(), first_bytes)
            with first_path.open(encoding="utf-8", newline="") as stream:
                first_rows = list(csv.DictReader(stream, delimiter=";"))
            with second_path.open(encoding="utf-8", newline="") as stream:
                second_rows = list(csv.DictReader(stream, delimiter=";"))
            self.assertEqual([float(row["speed_kmh"]) for row in first_rows], [123])
            self.assertEqual([float(row["speed_kmh"]) for row in second_rows], [234])
            self.assertEqual(first_rows[0]["timestamp"], second_rows[0]["timestamp"])

    def test_exclusive_creation_never_truncates_an_existing_session(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("racecontrol.telemetry.csv_recorder.datetime", FrozenDateTime), \
             patch("racecontrol.telemetry.csv_recorder.uuid4", return_value=UUID(int=1)):
            directory = Path(temporary)
            with GT7CSVRecorder(directory) as first:
                first.record(sample(123))
                path = first.file_path
            original = path.read_bytes()
            # Même une collision d'identifiant forcée doit échouer sans jamais
            # ouvrir le premier CSV en écriture destructive.
            with self.assertRaises(FileExistsError):
                GT7CSVRecorder(directory)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
