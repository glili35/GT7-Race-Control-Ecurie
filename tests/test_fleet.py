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

    def test_signal_timeout(self):
        fleet = FleetMonitor(timeout_s=2)
        fleet.ingest(2, SimpleNamespace(speed_kmh=99), now=1)
        self.assertFalse(fleet.snapshots(now=4)[1].connected)
        self.assertIsNone(fleet.snapshots(now=4)[1].speed_kmh)

    def test_reject_invalid_car(self):
        with self.assertRaises(ValueError):
            FleetMonitor().ingest(5, object())

if __name__ == '__main__':
    unittest.main()
