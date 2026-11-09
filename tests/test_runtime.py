"""Tests du cycle de vie pilote, avec un CSV réel et un récepteur contrôlé."""
from __future__ import annotations

import csv
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from racecontrol.fleet import FleetMonitor
from racecontrol.runtime import PilotSession


def sample(speed=120):
    return SimpleNamespace(
        packet_format="A", position_x=1, position_y=0, position_z=2,
        velocity_x=0, velocity_y=0, velocity_z=0, speed_kmh=speed,
        rpm=5000, fuel_level=80, fuel_capacity=100, oil_pressure=3,
        water_temperature=85, oil_temperature=110,
        tyre_temperatures=(65, 66, 67, 68), current_lap=1, total_laps=10,
        best_lap_ms=90000, last_lap_ms=91000, race_position=1,
        race_position_total=4, current_gear=3, suggested_gear=3,
        throttle_percent=60, brake_percent=0, car_id=1,
    )


class FakeReceiver:
    instances = []
    fail_start = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        if self.fail_start:
            raise OSError("Le port UDP est déjà utilisé")
        self.started = True

    def stop(self):
        self.stopped = True
        self.started = False

    def emit(self, telemetry):
        self.kwargs["on_telemetry"](telemetry)


class FakeLink:
    instances = []

    def __init__(self, invitation, on_status=None):
        self.started = False
        self.stopped = False
        self.items = []
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def publish(self, telemetry):
        self.items.append(telemetry)

    def stop(self):
        self.stopped = True


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        FakeReceiver.instances = []
        FakeReceiver.fail_start = False
        FakeLink.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.messages = []
        self.session = PilotSession(FleetMonitor(), self.root, car_number=2,
                                    on_status=self.messages.append)
        self.addCleanup(self.session.stop)
        self.receiver_patch = patch("racecontrol.runtime.GT7Receiver", FakeReceiver)
        self.receiver_patch.start()
        self.addCleanup(self.receiver_patch.stop)
        self.transport_patch = patch.dict(sys.modules, {
            "racecontrol.transport": SimpleNamespace(PilotLink=FakeLink),
        })
        self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)

    def test_full_rate_csv_and_local_fleet(self):
        self.session.start("192.168.0.44")
        receiver = FakeReceiver.instances[-1]
        for i in range(60):
            receiver.emit(sample(i))
        self.session.stop()
        self.assertEqual(self.session.rows_written, 60)
        self.assertEqual(self.session.rows_dropped, 0)
        self.assertEqual(self.session.file_path.parent, self.root / "sessions")
        with self.session.file_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        self.assertEqual(len(rows), 60)
        self.assertEqual([float(row["speed_kmh"]) for row in rows], list(range(60)))
        self.assertEqual(self.session.fleet.snapshots()[1].speed_kmh, 59)
        self.assertTrue(receiver.stopped)
        self.assertFalse(self.session.running)

    def test_optional_transmission_does_not_require_recording(self):
        self.session.start("192.168.0.44", invitation="secret-invitation", record=False)
        FakeReceiver.instances[-1].emit(sample())
        self.session.stop()
        link = FakeLink.instances[-1]
        self.assertEqual(len(link.items), 1)
        self.assertTrue(link.started)
        self.assertTrue(link.stopped)
        self.assertIsNone(self.session.file_path)
        self.assertFalse((self.root / "sessions").exists())
        self.assertNotIn("secret-invitation", " ".join(self.messages))

    def test_udp_start_failure_closes_started_link_and_csv(self):
        FakeReceiver.fail_start = True
        with self.assertRaises(OSError):
            self.session.start("192.168.0.44", invitation="secret")
        self.assertFalse(self.session.running)
        self.assertTrue(FakeReceiver.instances[-1].stopped)
        self.assertTrue(FakeLink.instances[-1].stopped)
        self.assertFalse(self.session._writer.is_alive())
        self.assertEqual(self.session.rows_written, 0)
        # La fermeture doit libérer le fichier également sous Windows.
        self.session.file_path.unlink()

    def test_restart_uses_new_file_and_rejects_old_receiver_callback(self):
        self.session.start("192.168.0.44")
        first_receiver = FakeReceiver.instances[-1]
        first_receiver.emit(sample(10))
        self.session.stop()
        first_path = self.session.file_path
        self.session.start("192.168.0.44")
        second_path = self.session.file_path
        self.assertIsNone(self.session.fleet.snapshots()[1].speed_kmh)
        first_receiver.emit(sample(999))
        FakeReceiver.instances[-1].emit(sample(20))
        self.session.stop()
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(self.session.rows_written, 1)
        with second_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["speed_kmh"]), 20)

    def test_disk_write_error_is_visible_and_does_not_stop_capture(self):
        failed = threading.Event()

        def fail_write(*args):
            failed.set()
            raise OSError("Disque plein")

        with patch("racecontrol.runtime.GT7CSVRecorder.record", side_effect=fail_write):
            self.session.start("192.168.0.44")
            FakeReceiver.instances[-1].emit(sample())
            self.assertTrue(failed.wait(1))
            self.session.stop()
        self.assertIsNotNone(self.session.recording_error)
        self.assertTrue(any("CSV impossible" in text for text in self.messages))
        self.assertTrue(FakeReceiver.instances[-1].stopped)

    def test_writer_start_failure_releases_file_and_link(self):
        with patch("racecontrol.runtime.threading.Thread.start", side_effect=RuntimeError("Thread indisponible")):
            with self.assertRaisesRegex(RuntimeError, "Thread indisponible"):
                self.session.start("192.168.0.44", invitation="secret")
        self.assertFalse(self.session.running)
        self.assertTrue(FakeLink.instances[-1].stopped)
        self.session.file_path.unlink()

    def test_bad_invitation_error_never_contains_invitation(self):
        with patch.object(FakeLink, "__init__", side_effect=ValueError("secret-invitation")):
            with self.assertRaises(ValueError) as failure:
                self.session.start("192.168.0.44", invitation="secret-invitation")
        self.assertNotIn("secret-invitation", str(failure.exception))
        self.assertNotIn("secret-invitation", " ".join(self.messages))
        self.assertFalse(self.session.running)

    def test_record_queue_overflow_is_explicit_and_receiver_keeps_running(self):
        entered = threading.Event()
        release = threading.Event()
        real_record = __import__("racecontrol.runtime", fromlist=["GT7CSVRecorder"]).GT7CSVRecorder.record

        def slow_record(recorder, telemetry):
            entered.set()
            release.wait(2)
            real_record(recorder, telemetry)

        self.session.RECORD_QUEUE_SIZE = 1
        with patch("racecontrol.runtime.GT7CSVRecorder.record", new=slow_record):
            try:
                self.session.start("192.168.0.44")
                receiver = FakeReceiver.instances[-1]
                receiver.emit(sample(1))
                self.assertTrue(entered.wait(1))
                receiver.emit(sample(2))
                receiver.emit(sample(3))
                self.assertEqual(self.session.rows_dropped, 1)
                self.assertTrue(self.session.running)
                self.assertTrue(any("mesures CSV ont été perdues" in text for text in self.messages))
            finally:
                release.set()
                self.session.stop()
        self.assertEqual(self.session.rows_written, 2)

    def test_discovery_is_explicit_none_and_status_failure_is_contained(self):
        self.session.on_status = lambda text: (_ for _ in ()).throw(RuntimeError("Tk fermé"))
        self.session.start(None, record=False)
        self.assertIsNone(FakeReceiver.instances[-1].kwargs["ps5_ip"])
        self.session._emit("statut")
        self.session.stop()

    def test_invalid_car_number_rejected(self):
        for number in (0, 5, True):
            with self.subTest(number=number), self.assertRaises(ValueError):
                PilotSession(FleetMonitor(), self.root, car_number=number)


if __name__ == "__main__":
    unittest.main()
