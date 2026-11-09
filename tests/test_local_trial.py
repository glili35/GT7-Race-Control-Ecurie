"""Essai local : vrai HTTPS, source GT7 remplacée uniquement dans ces tests.

Les échantillons sont des GT7Telemetry synthétiques et ne constituent pas une
validation sur PS5. Aucun générateur n'est présent dans le contrôleur livré.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from racecontrol.fleet import FleetMonitor
from racecontrol.local_trial import LocalTrial
from racecontrol.runtime import PilotSession
from racecontrol.telemetry.decoder import GT7Telemetry
from racecontrol.telemetry.protocol import GT7PacketFormat
from racecontrol.transport import EngineerHub, PilotLink, invitation_details


def sample(speed=120.0):
    return GT7Telemetry(
        packet_format=GT7PacketFormat.A,
        position_x=123.0, position_y=0.0, position_z=456.0,
        velocity_x=0.0, velocity_y=0.0, velocity_z=speed / 3.6,
        rpm=6000.0, fuel_level=65.0, fuel_capacity=100.0,
        speed_ms=speed / 3.6, boost=0.0, oil_pressure=3.0,
        water_temperature=85.0, oil_temperature=110.0,
        tyre_temperatures=(65.0, 66.0, 67.0, 68.0),
        current_lap=2, total_laps=10, best_lap_ms=90000,
        last_lap_ms=91000, race_position=3, race_position_total=4,
        flags=1, current_gear=3, suggested_gear=3,
        throttle_raw=153, brake_raw=0,
        wheel_angular_speeds=(10.0,) * 4, tyre_radii=(0.3,) * 4,
        car_id=321,
    )


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("L'essai n'a pas reçu le résultat réseau dans le délai prévu")


class ControlledReceiver:
    instances = []
    fail_start = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        if self.fail_start:
            raise OSError("Port UDP indisponible")
        self.started = True

    def stop(self):
        self.stopped = True
        self.started = False

    def emit(self, telemetry):
        self.kwargs["diagnostics"].record_datagram("192.168.0.44")
        self.kwargs["diagnostics"].record_decoded()
        self.kwargs["on_telemetry"](telemetry)


class RecordingFleet(FleetMonitor):
    """Trace la provenance des valeurs réellement reçues par l'ingénieur."""
    def __init__(self):
        super().__init__()
        self.received = []

    def ingest(self, number, telemetry, **kwargs):
        self.received.append((number, telemetry, threading.current_thread()))
        super().ingest(number, telemetry, **kwargs)


class LocalTrialTests(unittest.TestCase):
    def setUp(self):
        ControlledReceiver.instances = []
        ControlledReceiver.fail_start = False
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_dir = Path(self.directory.name)
        self.engineer_fleet = RecordingFleet()
        self.messages = []
        self.trial = LocalTrial(self.engineer_fleet, self.data_dir, car_number=3,
                                on_status=self.messages.append)
        self.addCleanup(self.trial.stop)
        receiver_patch = patch("racecontrol.runtime.GT7Receiver", ControlledReceiver)
        receiver_patch.start()
        self.addCleanup(receiver_patch.stop)

    def assert_port_closed(self, port):
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                pass

    def test_start_creates_one_capture_and_no_engineer_values_without_ps5(self):
        settings = self.data_dir / "settings.json"
        settings.write_text('{"invitation":"ma-vraie-invitation"}', encoding="utf-8")
        self.engineer_fleet.ingest(1, sample())
        self.trial.start(None, record=False)
        self.assertTrue(self.trial.running)
        self.assertIsNot(self.trial.local_fleet, self.engineer_fleet)
        self.assertEqual(len(ControlledReceiver.instances), 1)
        self.assertIsNone(ControlledReceiver.instances[0].kwargs['ps5_ip'])
        self.assertEqual(self.trial.hub._server.server_address[0], '127.0.0.1')
        self.assertGreater(self.trial.hub.port, 0)
        invitation = self.trial.hub.invitation(3, '127.0.0.1')
        self.assertEqual(invitation_details(invitation), ('127.0.0.1', self.trial.hub.port, 3))
        self.assertTrue(all(not car.has_data for car in self.engineer_fleet.snapshots()))
        self.assertFalse((self.data_dir / 'sessions').exists())
        self.assertEqual(settings.read_text(encoding="utf-8"), '{"invitation":"ma-vraie-invitation"}')
        self.assertTrue(all('gt7rc://' not in message for message in self.messages))

    def test_engineer_fleet_receives_only_after_real_https_round_trip(self):
        entered = threading.Event()
        release = threading.Event()
        original_send = PilotLink._send

        def delayed_send(link, message):
            entered.set()
            release.wait(1.0)
            return original_send(link, message)

        with patch.object(PilotLink, '_send', delayed_send):
            self.trial.start('192.168.0.44', record=False)
            telemetry = sample(234.0)
            try:
                ControlledReceiver.instances[-1].emit(telemetry)
                self.assertEqual(self.trial.local_fleet.snapshots()[2].speed_kmh, 234.0)
                self.assertTrue(entered.wait(0.5))
                self.assertFalse(self.engineer_fleet.snapshots()[2].has_data,
                                 'Une copie directe contournerait la liaison HTTPS')
            finally:
                release.set()
            wait_for(lambda: self.engineer_fleet.snapshots()[2].connected)
        snapshot = self.engineer_fleet.snapshots()[2]
        self.assertAlmostEqual(snapshot.speed_kmh, 234.0)
        self.assertEqual(snapshot.fuel_percent, 65.0)
        self.assertEqual(snapshot.tyre_temperatures, (65.0, 66.0, 67.0, 68.0))
        self.assertEqual(snapshot.lap, 2)
        number, received, thread = self.engineer_fleet.received[-1]
        self.assertEqual(number, 3)
        self.assertIsNot(received, telemetry)  # JSON décodé par le vrai hub.
        self.assertIsNot(thread, threading.current_thread())
        wait_for(lambda: any('transmise' in message for message in self.messages))

    def test_csv_keeps_every_captured_sample_while_transport_uses_latest(self):
        self.trial.start('192.168.0.44', record=True)
        pilot = self.trial.pilot
        receiver = ControlledReceiver.instances[-1]
        for speed in range(60, 120):
            receiver.emit(sample(float(speed)))
        wait_for(lambda: math.isclose(self.engineer_fleet.snapshots()[2].speed_kmh or 0, 119.0))
        self.trial.stop()
        self.assertEqual(pilot.rows_written, 60)
        self.assertEqual(pilot.rows_dropped, 0)
        with pilot.file_path.open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream, delimiter=';'))
        self.assertEqual(len(rows), 60)
        for expected, row in zip(range(60, 120), rows):
            self.assertAlmostEqual(float(row['speed_kmh']), expected)
        self.assertTrue(receiver.stopped)
        self.assertFalse(self.trial.running)
        self.assertIsNone(self.trial.hub)
        self.assertIsNone(self.trial.pilot)
        self.assertTrue(all(not car.has_data for car in self.engineer_fleet.snapshots()))
        self.assertTrue(all(not car.has_data for car in self.trial.local_fleet.snapshots()))

    def test_stop_restart_has_no_stale_samples_or_second_engineer_capture(self):
        self.trial.start('192.168.0.44', record=False)
        first = ControlledReceiver.instances[-1]
        first.emit(sample(100.0))
        wait_for(lambda: self.engineer_fleet.snapshots()[2].connected)
        port = self.trial.hub.port
        self.trial.stop()
        self.assert_port_closed(port)
        self.trial.stop()  # Arrêt répété sans erreur.
        self.trial.start('192.168.0.44', record=False)
        self.assertEqual(sum(receiver.started for receiver in ControlledReceiver.instances), 1)
        first.emit(sample(300.0))
        self.assertFalse(self.trial.local_fleet.snapshots()[2].has_data)
        self.assertFalse(self.engineer_fleet.snapshots()[2].has_data)
        ControlledReceiver.instances[-1].emit(sample(80.0))
        wait_for(lambda: self.engineer_fleet.snapshots()[2].speed_kmh == 80.0)
        self.assertEqual(len(ControlledReceiver.instances), 2)

    def test_repeated_start_does_not_open_another_socket_or_capture(self):
        self.trial.start('192.168.0.44', record=False)
        hub = self.trial.hub
        with self.assertRaisesRegex(RuntimeError, 'déjà en cours'):
            self.trial.start('192.168.0.45', record=False)
        self.assertIs(self.trial.hub, hub)
        self.assertEqual(len(ControlledReceiver.instances), 1)

    def test_udp_start_failure_rolls_back_tls_link_csv_and_server(self):
        hubs = []
        pilots = []

        class TrackingHub(EngineerHub):
            def start(hub):
                super().start()
                hubs.append((hub, hub.port))

        class TrackingPilot(PilotSession):
            def __init__(pilot, *args, **kwargs):
                super().__init__(*args, **kwargs)
                pilots.append(pilot)

        ControlledReceiver.fail_start = True
        with patch('racecontrol.local_trial.EngineerHub', TrackingHub), patch('racecontrol.local_trial.PilotSession', TrackingPilot):
            with self.assertRaisesRegex(OSError, 'UDP indisponible'):
                self.trial.start('192.168.0.44', record=True)
        self.assertFalse(self.trial.running)
        self.assertIsNone(self.trial.hub)
        self.assertIsNone(self.trial.pilot)
        self.assertTrue(ControlledReceiver.instances[-1].stopped)
        self.assertFalse(pilots[-1].running)
        self.assertFalse(pilots[-1]._writer.is_alive())
        self.assert_port_closed(hubs[-1][1])
        pilots[-1].file_path.unlink()  # Le CSV est bien fermé, y compris sous Windows.
        ControlledReceiver.fail_start = False
        self.trial.start('192.168.0.44', record=False)
        self.assertTrue(self.trial.running)

    def test_hub_failure_after_bind_releases_socket_and_never_starts_capture(self):
        ports = []

        class FailingHub(EngineerHub):
            def start(hub):
                super().start()
                ports.append(hub.port)
                raise OSError('Échec après ouverture HTTPS')

        with patch('racecontrol.local_trial.EngineerHub', FailingHub):
            with self.assertRaisesRegex(OSError, 'ouverture HTTPS'):
                self.trial.start('192.168.0.44')
        self.assertFalse(ControlledReceiver.instances)
        self.assertFalse(self.trial.running)
        self.assertIsNone(self.trial.hub)
        self.assertIsNone(self.trial.pilot)
        self.assert_port_closed(ports[-1])

    def test_pilot_constructor_failure_still_closes_started_hub(self):
        ports = []
        original_start = EngineerHub.start

        def remember_port(hub):
            original_start(hub)
            ports.append(hub.port)

        with patch.object(EngineerHub, 'start', remember_port), patch('racecontrol.local_trial.PilotSession', side_effect=OSError('Création pilote impossible')):
            with self.assertRaisesRegex(OSError, 'Création pilote'):
                self.trial.start(None)
        self.assertFalse(ControlledReceiver.instances)
        self.assertFalse(self.trial.running)
        self.assert_port_closed(ports[-1])

    def test_stop_attempts_hub_cleanup_even_if_pilot_stop_raises(self):
        self.trial.start('192.168.0.44', record=False)
        pilot = self.trial.pilot
        port = self.trial.hub.port
        original_stop = pilot.stop

        def stop_then_fail():
            original_stop()
            raise OSError('Erreur secondaire d’arrêt')

        with patch.object(pilot, 'stop', stop_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'arrêté complètement'):
                self.trial.stop()
        self.assert_port_closed(port)
        self.assertFalse(self.trial.running)
        self.assertIsNone(self.trial.hub)
        self.assertTrue(all(not car.has_data for car in self.trial.local_fleet.snapshots()))

    def test_invalid_slot_is_rejected_before_creating_files(self):
        for slot in (True, 0, 5, 1.0, '1'):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                LocalTrial(FleetMonitor(), self.data_dir, car_number=slot)
        self.assertFalse(list(self.data_dir.iterdir()))


if __name__ == '__main__':
    unittest.main()
