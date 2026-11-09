from types import SimpleNamespace
import unittest

from racecontrol.fleet import FleetMonitor


class FleetTests(unittest.TestCase):
    def test_four_independent_cars(self):
        fleet = FleetMonitor()
        fleet.ingest(1, SimpleNamespace(speed_kmh=120, fuel=4, fuel_capacity=50), now=10)
        fleet.ingest(4, SimpleNamespace(speed_kmh=210, fuel=40, fuel_capacity=50), now=11)
        cars = fleet.snapshots(now=12)
        self.assertEqual(len(cars), 4)
        self.assertTrue(cars[0].connected)
        self.assertEqual(cars[3].speed_kmh, 210)
        self.assertFalse(cars[1].connected)
        self.assertIn('CARBURANT CRITIQUE', cars[0].alerts)

    def test_real_decoder_field_fuel_level_and_all_car_metrics(self):
        fleet = FleetMonitor()
        fleet.ingest(2, SimpleNamespace(
            fuel_level=9, fuel=99, fuel_capacity=100, speed_kmh=145,
            rpm=6500, current_gear=4, current_lap=3, total_laps=10,
            throttle_percent=70, brake_percent=0,
            tyre_temperatures=(65, 66, 70, 71), last_lap_ms=90000,
            best_lap_ms=0xFFFFFFFF, race_position=2, position_x=12, position_z=34,
        ), now=1)
        car = fleet.snapshots(now=2)[1]
        self.assertEqual(car.fuel_percent, 9)
        self.assertEqual(car.fuel_l, 9)
        self.assertEqual(car.current_gear, 4)
        self.assertEqual(car.current_lap, 3)
        self.assertEqual(car.throttle_percent, 70)
        self.assertEqual(car.brake_percent, 0)
        self.assertEqual(car.tyre_temperatures, (65, 66, 70, 71))
        self.assertEqual(car.last_lap_ms, 90000)
        self.assertIsNone(car.best_lap_ms)
        self.assertIn('CARBURANT CRITIQUE', car.alerts)

    def test_signal_timeout_masks_previous_values_and_estimates(self):
        fleet = FleetMonitor(timeout_s=2)
        for now, lap, fuel in [(0, 1, 100), (1, 2, 95), (2, 3, 90)]:
            fleet.ingest(2, SimpleNamespace(speed_kmh=99, current_lap=lap, fuel_level=fuel, fuel_capacity=100), now=now)
        self.assertEqual(fleet.snapshots(now=2)[1].consumption_percent_per_lap, 5)
        car = fleet.snapshots(now=5)[1]
        self.assertFalse(car.connected)
        self.assertTrue(car.has_data)
        self.assertIsNone(car.speed_kmh)
        self.assertIsNone(car.fuel_percent)
        self.assertIsNone(car.consumption_percent_per_lap)
        self.assertIn('SIGNAL PERDU', car.alerts)

    def test_never_connected_is_waiting_and_missing_is_not_zero(self):
        fleet = FleetMonitor()
        car = fleet.snapshots(now=1)[0]
        self.assertFalse(car.has_data)
        self.assertFalse(car.connected)
        self.assertEqual(car.alerts, ())
        self.assertIsNone(car.fuel_percent)
        fleet.ingest(1, SimpleNamespace(speed_kmh=0), now=2)
        car = fleet.snapshots(now=2)[0]
        self.assertEqual(car.speed_kmh, 0)
        self.assertIsNone(car.throttle_percent)
        self.assertIsNone(car.tyre_temperatures)

    def test_per_car_consumption_is_isolated_and_reset_on_reconnect(self):
        fleet = FleetMonitor()
        for now, lap, fuel1, fuel4 in [(0, 1, 100, 100), (1, 2, 95, 90), (2, 3, 90, 80)]:
            for number, fuel in [(1, fuel1), (4, fuel4)]:
                fleet.ingest(number, SimpleNamespace(current_lap=lap, fuel_level=fuel, fuel_capacity=100), now=now)
        cars = fleet.snapshots(now=2)
        self.assertEqual(cars[0].consumption_percent_per_lap, 5)
        self.assertEqual(cars[3].consumption_percent_per_lap, 10)
        self.assertIsNone(cars[1].consumption_percent_per_lap)
        fleet.ingest(1, SimpleNamespace(current_lap=4, fuel_level=85, fuel_capacity=100), now=10)
        self.assertIsNone(fleet.snapshots(now=10)[0].consumption_percent_per_lap)

    def test_reject_invalid_car_and_nonfinite_timeout(self):
        for number in (5, 0, True, 1.0, '1'):
            with self.subTest(number=number), self.assertRaises(ValueError):
                FleetMonitor().ingest(number, object())
        for timeout in (0, -1, float('nan'), float('inf'), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                FleetMonitor(timeout_s=timeout)

    def test_invalid_numeric_values_and_sentinels_do_not_reach_ui(self):
        fleet = FleetMonitor()
        fleet.ingest(1, SimpleNamespace(
            speed_kmh=float('nan'), fuel_level=float('inf'), fuel_capacity=100,
            rpm=True, current_lap=-1, last_lap_ms=0, best_lap_ms=0xFFFFFFFF,
            tyre_temperatures=(20, float('nan'), 40, 50),
        ), now=1)
        car = fleet.snapshots(now=1)[0]
        self.assertIsNone(car.speed_kmh)
        self.assertIsNone(car.fuel_percent)
        self.assertIsNone(car.rpm)
        self.assertIsNone(car.lap)
        self.assertIsNone(car.last_lap_ms)
        self.assertIsNone(car.best_lap_ms)
        self.assertEqual(car.tyre_temperatures, (20, None, 40, 50))

    def test_older_callback_cannot_replace_newer_car_data(self):
        fleet = FleetMonitor()
        fleet.ingest(1, SimpleNamespace(speed_kmh=100), now=10)
        fleet.ingest(1, SimpleNamespace(speed_kmh=10), now=9)
        self.assertEqual(fleet.snapshots(now=10)[0].speed_kmh, 100)

    def test_session_reset_keeps_other_cars(self):
        fleet = FleetMonitor()
        for number in (1, 2):
            fleet.ingest(number, SimpleNamespace(speed_kmh=number * 100), now=1)
        fleet.reset(1)
        cars = fleet.snapshots(now=1)
        self.assertFalse(cars[0].has_data)
        self.assertEqual(cars[1].speed_kmh, 200)
        fleet.reset()
        self.assertTrue(all(not car.has_data for car in fleet.snapshots(now=1)))

    def test_fresh_loading_or_inactive_flux_is_connected_but_hides_car_values(self):
        for flags, activity in ((4, "loading"), (0, "inactive")):
            with self.subTest(activity=activity):
                fleet = FleetMonitor()
                fleet.ingest(1, SimpleNamespace(
                    flags=flags, speed_kmh=216, rpm=8000, current_gear=5,
                    fuel_level=5, fuel_capacity=100, current_lap=3, total_laps=10,
                    position_x=10, position_z=20, throttle_percent=90,
                    brake_percent=0, tyre_temperatures=(70, 71, 72, 73),
                    last_lap_ms=90000, best_lap_ms=89000,
                ), now=100)
                car = fleet.snapshots(now=100)[0]
                self.assertTrue(car.connected)
                self.assertTrue(car.has_data)
                self.assertEqual(car.activity_state, activity)
                for field in ("speed_kmh", "rpm", "current_gear", "fuel_percent", "lap",
                              "position_x", "position_z", "throttle_percent", "brake_percent",
                              "tyre_temperatures", "last_lap_ms", "best_lap_ms", "remaining_laps"):
                    self.assertIsNone(getattr(car, field), field)
                self.assertEqual(car.alerts, ())

    def test_pause_keeps_metrics_and_history_but_not_a_new_lap_estimate(self):
        fleet = FleetMonitor()
        for now, lap, fuel in [(0, 1, 100), (1, 2, 95), (2, 3, 90)]:
            fleet.ingest(1, SimpleNamespace(flags=1, current_lap=lap, fuel_level=fuel,
                                          fuel_capacity=100, speed_kmh=100), now=now)
        previous = fleet.snapshots(now=2)[0]
        fleet.ingest(1, SimpleNamespace(flags=3, current_lap=4, fuel_level=60,
                                      fuel_capacity=100, speed_kmh=100), now=2.1)
        paused = fleet.snapshots(now=2.1)[0]
        self.assertTrue(paused.connected)
        self.assertEqual(paused.activity_state, "paused")
        self.assertEqual(paused.speed_kmh, 100)
        self.assertEqual(paused.measured_laps, previous.measured_laps)
        self.assertEqual(paused.consumption_percent_per_lap, previous.consumption_percent_per_lap)

    def test_activity_compatibility_signal_loss_and_car_isolation(self):
        fleet = FleetMonitor()
        fleet.ingest(1, SimpleNamespace(flags=1, speed_kmh=100), now=1)
        fleet.ingest(2, SimpleNamespace(speed_kmh=120), now=1)
        fleet.ingest(3, SimpleNamespace(flags=0, speed_kmh=140), now=1)
        fleet.ingest(4, SimpleNamespace(flags=4, speed_kmh=160), now=1)
        cars = fleet.snapshots(now=1)
        self.assertEqual([car.activity_state for car in cars], ["driving", "unknown", "inactive", "loading"])
        self.assertEqual([car.speed_kmh for car in cars], [100, 120, None, None])
        self.assertTrue(all(car.connected for car in cars))
        lost = fleet.snapshots(now=5)
        self.assertTrue(all(not car.connected for car in lost))
        self.assertTrue(all(car.activity_state == "unknown" for car in lost))
        self.assertTrue(all(car.alerts == ("SIGNAL PERDU",) for car in lost))

    def test_gradual_refuelling_resets_only_affected_car(self):
        fleet = FleetMonitor()
        for now, lap, fuel in [(0, 1, 100), (1, 2, 95), (2, 3, 90)]:
            for car in (1, 2):
                fleet.ingest(car, SimpleNamespace(flags=1, current_lap=lap,
                                                 fuel_level=fuel, fuel_capacity=100), now=now)
        for step in range(1, 101):
            fleet.ingest(1, SimpleNamespace(flags=1, current_lap=3, fuel_level=90+step*.05,
                                           fuel_capacity=100), now=2+step*.001)
        cars = fleet.snapshots(now=2.1)
        self.assertEqual(cars[0].measured_laps, 0)
        self.assertEqual(cars[1].measured_laps, 1)
        self.assertAlmostEqual(cars[1].consumption_percent_per_lap, 5)


if __name__ == '__main__':
    unittest.main()
