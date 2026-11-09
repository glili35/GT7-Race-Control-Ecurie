"""Trajectoires locales et correspondance exacte des variantes, sans réseau."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from racecontrol import track_paths
from racecontrol.track_paths import TrackPaths


class TrackPathsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.index = self.root / "index.json"
        self.pack = self.root / "paths.json"
        self.path = [[10, 0], [50, 0], [50, 40], [10, 40], [10, 1]]
        self.entries = [
            {"official_id": "000001", "official_name": "Court", "reverse": False},
            {"official_id": "000002", "official_name": "Court inversé", "reverse": True},
            {"official_id": "000003", "official_name": "Long", "reverse": False},
        ]
        self.rows = [self.row()]
        self.save()

    def row(self, **changes):
        return {"official_id": "000001", "official_name": "Court", "reverse": False,
                "path": self.path, "provenance": "capture", "ambiguous_with": [], **changes}

    def save(self):
        self.index.write_text(json.dumps({"configurations": self.entries}), encoding="utf-8")
        self.pack.write_text(json.dumps({"format": "gt7-race-control-track-paths", "version": 1,
                                         "paths": self.rows}), encoding="utf-8")

    def load(self):
        return TrackPaths(index_path=self.index, paths_path=self.pack)

    def assert_invalid(self):
        paths = self.load()
        self.assertIsNotNone(paths.error)
        self.assertEqual(paths.names, ())
        self.assertIsNone(paths.load("Court"))

    def test_constructor_is_offline_and_preserves_point_order_without_closing_the_loop(self):
        before = self.pack.read_bytes()
        with patch("urllib.request.urlopen") as network:
            paths = self.load()
            network.assert_not_called()
        self.assertIsNone(paths.error)
        self.assertEqual(paths.names, ("Court",))
        self.assertEqual(paths.load("Court"), tuple(tuple(point) for point in self.path))
        self.assertNotEqual(paths.load("Court")[0], paths.load("Court")[-1])
        self.assertEqual(self.pack.read_bytes(), before)
        self.assertEqual(set(self.root.iterdir()), {self.index, self.pack})

    def test_missing_or_unknown_configuration_does_not_reuse_another_layout(self):
        paths = self.load()
        for name in ("Court inversé", "Long", "court", "Absent", None, []):
            with self.subTest(name=name):
                self.assertIsNone(paths.load(name))
                self.assertEqual(paths.equivalent_names(name), ())

    def test_explicit_reverse_reuses_only_the_exact_reversed_source(self):
        self.rows.append(self.row(official_id="000002", official_name="Court inversé", reverse=True,
                                  reverse_of_id="000001", path=list(reversed(self.path))))
        self.save()
        paths = self.load()
        self.assertIsNone(paths.error)
        self.assertEqual(paths.load("Court inversé"), tuple(reversed(paths.load("Court"))))
        for name in ("Court", "Court inversé"):
            self.assertEqual(paths.equivalent_names(name), ("Court", "Court inversé"))
        self.assertIsNone(paths.load("Long"))

    def test_reverse_unknown_source_cycle_or_different_geometry_is_rejected(self):
        for changes in ({"reverse_of_id": "000099"}, {"reverse_of_id": "000002"},
                        {"reverse_of_id": "000001", "path": self.path}):
            with self.subTest(changes=changes):
                values = {"official_id": "000002", "official_name": "Court inversé",
                          "reverse": True, "path": list(reversed(self.path)), **changes}
                self.rows = [self.row(), self.row(**values)]
                self.save()
                self.assert_invalid()

    def test_reverse_reference_on_forward_configuration_is_rejected(self):
        self.rows[0]["reverse_of_id"] = "000001"
        self.save()
        self.assert_invalid()

    def test_mismatched_id_name_or_direction_never_produces_a_map(self):
        for changes in ({"official_id": "000003"}, {"official_name": "Court inversé"},
                        {"reverse": True}, {"reverse": 0}, {"provenance": "estimated"}):
            with self.subTest(changes=changes):
                self.rows = [self.row(**changes)]
                self.save()
                self.assert_invalid()

    def test_duplicate_rows_and_catalogue_identifiers_are_rejected(self):
        self.rows *= 2
        self.save()
        self.assert_invalid()
        self.rows = [self.row()]
        self.entries.append(dict(self.entries[0], official_name="Alias"))
        self.save()
        self.assert_invalid()

    def test_missing_paths_and_flagged_sources_are_not_advertised_as_maps(self):
        for changes in ({"path": None}, {"path": []}, {"flags": ["partial_capture"]}):
            with self.subTest(changes=changes):
                self.rows = [self.row(**changes)]
                self.save()
                paths = self.load()
                self.assertIsNone(paths.error)
                self.assertEqual(paths.names, ())
                self.assertIsNone(paths.load("Court"))

    def test_nonfinite_boolean_text_or_oversized_coordinates_are_rejected(self):
        for value in (float("nan"), float("inf"), True, "10", 10**400, 10_000_001):
            with self.subTest(value=value):
                altered = [list(point) for point in self.path]
                altered[1][0] = value
                self.rows = [self.row(path=altered)]
                self.save()
                self.assert_invalid()

    def test_teleport_and_degenerate_paths_are_rejected(self):
        for points in ([[0, 0]] * 5, [[0, 0], [1, 0], [2, 0]],
                       [[0, 0], [1, 0], [2, 0], [500, 0]], [[0, 0, 0]] * 5):
            with self.subTest(points=points):
                self.rows = [self.row(path=points)]
                self.save()
                self.assert_invalid()

    def test_bad_format_duplicate_json_keys_or_missing_file_remains_nonfatal(self):
        for raw in ('{}', '[]', '{"format":"gt7-race-control-track-paths","version":true,"paths":[]}',
                    '{"format":"gt7-race-control-track-paths","version":1,"paths":[],"paths":[]}'):
            with self.subTest(raw=raw):
                self.pack.write_text(raw, encoding="utf-8")
                self.assert_invalid()
        self.pack.unlink()
        self.assert_invalid()

    def test_file_and_total_point_budgets_are_enforced(self):
        with patch.object(track_paths, "MAX_PATH_BYTES", 8):
            self.assert_invalid()
        with patch.object(track_paths, "MAX_TOTAL_POINTS", 4):
            self.assert_invalid()

    def test_catalogues_without_identifiers_cannot_claim_a_shipped_layout(self):
        self.entries = [{"official_name": "Court", "bundle": None}]
        self.save()
        paths = self.load()
        self.assertEqual(paths.names, ())
        self.assertIsNone(paths.load("Court"))

    def test_real_shipped_pack_is_valid_and_contains_named_layouts(self):
        paths = TrackPaths()
        self.assertIsNone(paths.error)
        self.assertGreaterEqual(len(paths.names), 80)
        for name in ("High Speed Ring", "Circuit de Spa-Francorchamps"):
            self.assertGreater(len(paths.load(name)), 20)
        self.assertIsNone(paths.load("Nürburgring Endurance"))  # Relevé interrompu, pas de fausse liaison.


if __name__ == "__main__":
    unittest.main()
