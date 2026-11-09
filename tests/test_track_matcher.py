"""Reconnaissance sur géométries synthétiques locales, sans PS5 ni Internet."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from racecontrol import circuits
from racecontrol.track_matcher import TrackMatcher


def lane(z=0.0, *, start=0, end=900):
    return [(float(x), z + offset, side)
            for x in range(start, end + 1, 5)
            for offset, side in ((-8.0, "L"), (8.0, "R"))]


class TrackMatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.index = self.root / "index.json"
        self.cache = self.root / "cache"

    def tearDown(self):
        self.temp.cleanup()

    def library(self, entries, geometry):
        self.index.write_text(json.dumps({"configurations": [
            {"official_name": name, "bundle": None if filename is None else {"file": filename}}
            for name, filename in entries
        ]}), encoding="utf-8")
        for filename, points in geometry.items():
            path = self.cache / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"edges": [
                {"x": x, "z": z, "side": side} for x, z, side in points
            ]}), encoding="utf-8")
        return circuits.CircuitLibrary(self.cache, self.index)

    def test_one_position_never_identifies_a_circuit(self):
        library = self.library([("A", "tracks/a.json")], {"tracks/a.json": lane()})
        matcher = TrackMatcher(library)
        first = matcher.observe(50.0, 0.0, now=1.0)
        self.assertEqual(first.status, "waiting")
        self.assertIsNone(first.candidate)
        for index in range(20):
            result = matcher.observe(50.0, 0.0, now=2.0 + index)
        self.assertEqual(result.sample_count, 1)
        self.assertIsNone(result.candidate)

    def test_several_positions_distinguish_separate_installed_geometries(self):
        library = self.library([("A", "tracks/a.json"), ("B", "tracks/b.json")],
                               {"tracks/a.json": lane(), "tracks/b.json": lane(1000.0)})
        matcher = TrackMatcher(library)
        for index in range(7):
            result = matcher.observe(index * 30.0, 0.0, now=index + 1.0)
        self.assertEqual(result.status, "candidate")
        self.assertEqual(result.candidate, "A")
        self.assertEqual(result.candidates, ("A",))
        self.assertGreaterEqual(result.confidence, 0.75)
        self.assertEqual(result.stable_candidate, "A")
        self.assertAlmostEqual(result.mean_distance_m, 8.0)

    def test_shared_bundle_variants_always_require_manual_configuration_choice(self):
        library = self.library([("Monza", "tracks/shared.json"), ("Monza variante", "tracks/shared.json")],
                               {"tracks/shared.json": lane()})
        matcher = TrackMatcher(library)
        self.assertEqual(matcher.loaded_groups, 1)
        self.assertEqual(matcher.available_configurations, 2)
        for index in range(8):
            result = matcher.observe(index * 30.0, 0.0, now=index + 1.0)
        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.candidate)
        self.assertEqual(result.candidates, ("Monza", "Monza variante"))
        self.assertIsNone(result.stable_candidate)

    def test_identical_geometry_in_different_files_is_also_ambiguous(self):
        points = lane()
        reordered = [(x, z, "R" if side == "L" else "L") for x, z, side in reversed(points)]
        library = self.library([("A", "tracks/a.json"), ("A bis", "tracks/alias.json")],
                               {"tracks/a.json": points, "tracks/alias.json": reordered})
        matcher = TrackMatcher(library)
        self.assertEqual(matcher.loaded_groups, 1)
        for index in range(8):
            result = matcher.observe(index * 30.0, 0.0, now=index + 1.0)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.candidates, ("A", "A bis"))
        self.assertIsNone(result.candidate)

    def test_overlapping_borders_remain_ambiguous_even_with_different_coordinates(self):
        library = self.library([("A", "tracks/a.json"), ("B", "tracks/b.json")],
                               {"tracks/a.json": lane(), "tracks/b.json": lane(2.0)})
        matcher = TrackMatcher(library)
        for index in range(8):
            result = matcher.observe(index * 30.0, 0.0, now=index + 1.0)
        self.assertEqual(matcher.loaded_groups, 2)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.candidates, ("A", "B"))
        self.assertIsNone(result.candidate)

    def test_one_outlier_does_not_override_a_stable_match(self):
        library = self.library([("A", "tracks/a.json"), ("B", "tracks/b.json")],
                               {"tracks/a.json": lane(), "tracks/b.json": lane(1000.0)})
        matcher = TrackMatcher(library)
        for index in range(8):
            result = matcher.observe(index * 30.0, 0.0, now=index + 1.0)
        self.assertEqual(result.candidate, "A")
        result = matcher.observe(50000.0, 90000.0, now=9.0)
        self.assertEqual(result.stable_candidate, "A")
        self.assertNotEqual(result.candidate, "B")
        result = matcher.observe(270.0, 0.0, now=10.0)
        self.assertEqual(result.candidate, "A")

    def test_extended_unknown_positions_keep_previous_suggestion_without_forcing_it(self):
        library = self.library([("A", "tracks/a.json")], {"tracks/a.json": lane()})
        matcher = TrackMatcher(library)
        for index in range(8):
            result = matcher.observe(index * 30.0, 0.0, now=index + 1.0)
        for index in range(8, 16):
            result = matcher.observe(index * 30.0, 5000.0, now=index + 1.0)
        self.assertEqual(result.status, "no_match")
        self.assertIsNone(result.candidate)
        self.assertEqual(result.stable_candidate, "A")

    def test_spin_in_a_small_area_is_insufficient_spatial_evidence(self):
        library = self.library([("A", "tracks/a.json")], {"tracks/a.json": lane()})
        matcher = TrackMatcher(library)
        around = [(50.0, 0.0), (65.0, 0.0), (65.0, 15.0), (50.0, 15.0), (35.0, 15.0), (35.0, 0.0)]
        for index, (x, z) in enumerate(around * 3):
            result = matcher.observe(x, z, now=index + 1.0)
        self.assertIsNone(result.candidate)
        self.assertIsNone(result.stable_candidate)

    def test_missing_geometry_is_never_downloaded_or_claimed_as_recognized(self):
        library = self.library([("Disponible mais non installé", "tracks/a.json"), ("Sans géométrie", None)], {})
        with patch.object(circuits, "urlopen") as download:
            matcher = TrackMatcher(library)
            result = matcher.observe(50, 20)
            matcher.reload()
            download.assert_not_called()
        self.assertEqual(matcher.loaded_groups, 0)
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.candidate)

    def test_reload_picks_up_a_voluntary_install_and_resets_old_evidence(self):
        library = self.library([("A", "tracks/a.json")], {})
        matcher = TrackMatcher(library)
        self.assertEqual(matcher.loaded_groups, 0)
        self.library([("A", "tracks/a.json")], {"tracks/a.json": lane()})
        matcher.reload()
        self.assertEqual(matcher.loaded_groups, 1)
        self.assertEqual(matcher.observe(0, 0, now=1).sample_count, 1)

    def test_invalid_samples_and_frequency_limit_do_not_grow_the_buffer(self):
        library = self.library([("A", "tracks/a.json")], {"tracks/a.json": lane()})
        matcher = TrackMatcher(library)
        matcher.observe(0, 0, now=1)
        result = matcher.observe(30, 0, now=1.01)
        self.assertEqual(result.sample_count, 1)
        for bad in (float("nan"), float("inf"), True, "20", 10**400):
            result = matcher.observe(bad, 0, now=2)
            self.assertEqual(result.sample_count, 1)
            self.assertIsNone(result.candidate)
        result = matcher.observe(30, 0, now=2)
        self.assertEqual(result.sample_count, 2)
        for index in range(3, 70):
            matcher.observe(index * 10, 0, now=index)
        self.assertLessEqual(result.sample_count, 24)
        self.assertEqual(matcher.observe(700, 0, now=70).sample_count, 24)

    def test_new_distinct_circuit_can_eventually_be_proposed_without_resetting_manually(self):
        library = self.library([("A", "tracks/a.json"), ("B", "tracks/b.json")],
                               {"tracks/a.json": lane(), "tracks/b.json": lane(1000.0)})
        matcher = TrackMatcher(library)
        for index in range(8):
            matcher.observe(index * 30.0, 0.0, now=index + 1.0)
        for index in range(24):
            result = matcher.observe(index * 30.0, 1000.0, now=index + 9.0)
        self.assertEqual(result.candidate, "B")
        self.assertEqual(result.stable_candidate, "B")
        matcher.reset(keep_stable=False)
        self.assertIsNone(matcher.observe(0, 1000, now=40).stable_candidate)


if __name__ == "__main__":
    unittest.main()
