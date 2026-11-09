"""Fuel estimates use complete laps and never mix separate race sessions."""
from types import SimpleNamespace
import unittest

from racecontrol.strategy import FuelStrategy


def sample(lap=1, fuel=100, capacity=100, total=10, car_id=7, **fields):
    return SimpleNamespace(
        current_lap=lap, fuel_level=fuel, fuel_capacity=capacity,
        total_laps=total, car_id=car_id, **fields,
    )


class FuelStrategyTests(unittest.TestCase):
    def test_skip_first_partial_then_measure_complete_lap(self):
        strategy = FuelStrategy()
        self.assertIsNone(strategy.update(sample(1, 83)).consumption_percent_per_lap)
        self.assertIsNone(strategy.update(sample(2, 80)).consumption_percent_per_lap)
        estimate = strategy.update(sample(3, 75))
        self.assertEqual(estimate.measured_laps, 1)
        self.assertAlmostEqual(estimate.consumption_percent_per_lap, 5)
        self.assertAlmostEqual(estimate.remaining_laps, 15)
        self.assertAlmostEqual(estimate.fuel_needed_percent, 40)
        self.assertEqual(estimate.status, "ESTIMATION PROVISOIRE")

    def test_out_lap_zero_and_first_lap_are_excluded(self):
        strategy = FuelStrategy()
        for lap, fuel in [(0, 100), (1, 98), (2, 80)]:
            self.assertEqual(strategy.update(sample(lap, fuel)).measured_laps, 0)
        estimate = strategy.update(sample(3, 75))
        self.assertEqual(estimate.consumption_percent_per_lap, 5)
        self.assertEqual(estimate.measured_laps, 1)

    def test_normalize_raw_fuel_to_capacity(self):
        strategy = FuelStrategy()
        for lap, fuel in [(1, 40), (2, 35), (3, 30)]:
            estimate = strategy.update(sample(lap, fuel, capacity=50))
        self.assertEqual(estimate.consumption_percent_per_lap, 10)
        self.assertEqual(estimate.remaining_laps, 6)

    def test_refuelling_inside_lap_clears_history_and_excludes_lap(self):
        strategy = FuelStrategy()
        for lap, fuel in [(1, 100), (2, 95), (3, 90)]:
            strategy.update(sample(lap, fuel))
        self.assertEqual(strategy.update(sample(3, 99)).measured_laps, 0)
        self.assertEqual(strategy.update(sample(4, 95)).measured_laps, 0)
        estimate = strategy.update(sample(5, 90))
        self.assertEqual(estimate.measured_laps, 1)
        self.assertEqual(estimate.consumption_percent_per_lap, 5)

    def test_refuelling_at_lap_boundary_excludes_both_affected_laps(self):
        strategy = FuelStrategy()
        for lap, fuel in [(1, 40), (2, 35), (3, 90), (4, 85)]:
            self.assertEqual(strategy.update(sample(lap, fuel)).measured_laps, 0)
        self.assertEqual(strategy.update(sample(5, 80)).consumption_percent_per_lap, 5)

    def test_backward_lap_capacity_and_car_changes_reset_history(self):
        for changed in [sample(1, 80), sample(4, 75, capacity=90), sample(4, 75, car_id=8), sample(4, 75, total=20)]:
            with self.subTest(changed=changed):
                strategy = FuelStrategy()
                for lap, fuel in [(1, 100), (2, 95), (3, 90)]:
                    strategy.update(sample(lap, fuel))
                self.assertEqual(strategy.update(changed).measured_laps, 0)

    def test_skipped_crossings_and_loading_do_not_make_lap_estimates(self):
        strategy = FuelStrategy()
        strategy.update(sample(1, 100))
        strategy.update(sample(2, 95))
        self.assertEqual(strategy.update(sample(4, 80)).measured_laps, 0)
        self.assertEqual(strategy.update(sample(5, 75)).measured_laps, 0)
        self.assertEqual(strategy.update(sample(6, 70)).measured_laps, 1)
        self.assertEqual(strategy.update(sample(6, 70, flags=4)).measured_laps, 0)

    def test_invalid_values_never_yield_nan_or_guessed_zero(self):
        strategy = FuelStrategy()
        for telemetry in [sample(fuel=float('nan')), sample(capacity=0), sample(fuel=-1), sample(fuel=101), sample(lap=None), sample(fuel=True), sample(fuel=10**1000)]:
            self.assertIsNone(strategy.update(telemetry).remaining_laps)

    def test_unlimited_laps_no_finish_fuel_prediction(self):
        strategy = FuelStrategy()
        for lap, fuel in [(1, 100), (2, 95), (3, 90)]:
            estimate = strategy.update(sample(lap, fuel, total=0))
        self.assertEqual(estimate.consumption_percent_per_lap, 5)
        self.assertIsNone(estimate.fuel_needed_percent)

    def test_history_window_is_bounded_and_estimate_becomes_usable(self):
        strategy = FuelStrategy(window=3)
        for lap, fuel in [(1, 100), (2, 99), (3, 97), (4, 94), (5, 90), (6, 85)]:
            estimate = strategy.update(sample(lap, fuel))
        self.assertEqual(estimate.measured_laps, 4)
        self.assertEqual(estimate.consumption_percent_per_lap, 4)
        self.assertEqual(estimate.status, "ESTIMATION SUR TOURS COMPLETS")

    def test_insufficient_fuel_alert_requires_two_complete_laps(self):
        strategy = FuelStrategy()
        strategy.update(sample(1, 30, total=20))
        strategy.update(sample(2, 25, total=20))
        self.assertFalse(strategy.update(sample(3, 20, total=20)).insufficient)
        self.assertTrue(strategy.update(sample(4, 15, total=20)).insufficient)

    def test_gradual_refuelling_is_detected_and_affected_lap_is_excluded(self):
        strategy = FuelStrategy()
        for lap, fuel in [(1, 100), (2, 95), (3, 90), (3, 80)]:
            estimate = strategy.update(sample(lap, fuel, flags=1))
        self.assertEqual(estimate.measured_laps, 1)
        # No individual delta exceeds the old 0.1% tank threshold.
        for step in range(1, 101):
            estimate = strategy.update(sample(3, 80 + step * 0.05, flags=1))
        self.assertEqual(estimate.measured_laps, 0)
        self.assertIsNone(estimate.consumption_percent_per_lap)
        self.assertEqual(strategy.update(sample(4, 80, flags=1)).measured_laps, 0)
        recovered = strategy.update(sample(5, 75, flags=1))
        self.assertEqual(recovered.measured_laps, 1)
        self.assertAlmostEqual(recovered.consumption_percent_per_lap, 5)

    def test_small_fuel_oscillations_are_not_summed_as_refuelling(self):
        strategy = FuelStrategy()
        for lap, fuel in [(1, 100), (2, 95), (3, 90), (3, 80)]:
            strategy.update(sample(lap, fuel, flags=1))
        for _ in range(50):
            strategy.update(sample(3, 80.05, flags=1))
            estimate = strategy.update(sample(3, 80, flags=1))
        self.assertEqual(estimate.measured_laps, 1)
        self.assertAlmostEqual(estimate.consumption_percent_per_lap, 5)

    def test_gradual_refuelling_across_lap_boundary_excludes_touched_laps(self):
        strategy = FuelStrategy()
        for lap, fuel in [(1, 100), (2, 95), (3, 90), (3, 80)]:
            strategy.update(sample(lap, fuel, flags=1))
        for step in range(1, 101):
            strategy.update(sample(3 if step < 50 else 4, 80 + step * 0.05, flags=1))
        self.assertEqual(strategy.update(sample(5, 80, flags=1)).measured_laps, 0)
        recovered = strategy.update(sample(6, 75, flags=1))
        self.assertEqual(recovered.measured_laps, 1)
        self.assertAlmostEqual(recovered.consumption_percent_per_lap, 5)

    def test_pause_preserves_estimate_without_processing_new_lap_or_fuel(self):
        strategy = FuelStrategy()
        for lap, fuel in [(1, 100), (2, 95), (3, 90)]:
            before_pause = strategy.update(sample(lap, fuel, flags=1))
        paused = strategy.update(sample(4, 60, flags=3))
        self.assertEqual(paused, before_pause)
        resumed = strategy.update(sample(3, 85, flags=1))
        self.assertEqual(resumed.measured_laps, 1)
        self.assertAlmostEqual(resumed.consumption_percent_per_lap, 5)

    def test_inactive_and_loading_reset_history_and_skip_interrupted_lap(self):
        for flags in (0, 4):
            with self.subTest(flags=flags):
                strategy = FuelStrategy()
                for lap, fuel in [(1, 100), (2, 95), (3, 90)]:
                    strategy.update(sample(lap, fuel, flags=1))
                stopped = strategy.update(sample(3, 90, flags=flags))
                self.assertEqual(stopped.measured_laps, 0)
                self.assertEqual(strategy.update(sample(3, 88, flags=1)).measured_laps, 0)
                self.assertEqual(strategy.update(sample(4, 85, flags=1)).measured_laps, 0)
                recovered = strategy.update(sample(5, 80, flags=1))
                self.assertEqual(recovered.measured_laps, 1)
                self.assertAlmostEqual(recovered.consumption_percent_per_lap, 5)


if __name__ == '__main__':
    unittest.main()
