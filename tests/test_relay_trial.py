"""Essai relais sur un PC : vrais WebSockets, capture GT7 contrôlée.

Les mesures sont synthétiques et ne constituent ni une validation PS5 ni un
essai Internet. Les données ingénieur doivent traverser le relais et répondre
à un challenge ; seule la source UDP est remplacée dans ces tests.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from websockets.sync.client import ClientConnection

from racecontrol.fleet import FleetMonitor
from racecontrol.relay_client import RelayEngineerSession
from racecontrol.relay_server import RelayServer
from racecontrol.relay_trial import RelayLocalTrial
from racecontrol.runtime import PilotSession

try:
    from test_local_trial import ControlledReceiver, sample
except ModuleNotFoundError:
    from tests.test_local_trial import ControlledReceiver, sample


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("L'essai relais n'a pas atteint l'état attendu dans le délai prévu")


class RecordingFleet(FleetMonitor):
    def __init__(self):
        super().__init__()
        self.received = []

    def ingest(self, number, telemetry, **kwargs):
        super().ingest(number, telemetry, **kwargs)
        self.received.append((number, telemetry, threading.current_thread(), kwargs))


class RelayLocalTrialTests(unittest.TestCase):
    def setUp(self):
        ControlledReceiver.instances = []
        ControlledReceiver.fail_start = False
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_dir = Path(self.directory.name)
        self.engineer_fleet = RecordingFleet()
        self.messages = []
        self.trial = RelayLocalTrial(self.engineer_fleet, self.data_dir, car_number=3,
                                     on_status=self.messages.append)
        self.addCleanup(self.trial.stop)
        receiver = patch("racecontrol.runtime.GT7Receiver", ControlledReceiver)
        receiver.start()
        self.addCleanup(receiver.stop)

    def assert_port_closed(self, port):
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                pass

    def private_workspaces(self):
        return tuple(self.data_dir.glob("essai-relais-*"))

    def test_start_opens_a_loopback_relay_and_one_capture_without_inventing_data(self):
        settings = self.data_dir / "settings.json"
        saved = '{"invitation":"invitation-distante-conservee"}'
        settings.write_text(saved, encoding="utf-8")
        self.engineer_fleet.ingest(1, sample())
        self.trial.start(None, record=False)
        self.assertTrue(self.trial.running)
        self.assertTrue(self.trial.engineer.connected)
        self.assertIsNot(self.trial.local_fleet, self.engineer_fleet)
        self.assertEqual(len(ControlledReceiver.instances), 1)
        self.assertIsNone(ControlledReceiver.instances[-1].kwargs["ps5_ip"])
        self.assertGreater(self.trial.hub.port, 0)
        invitation = self.trial.engineer.invitation(3)
        self.assertTrue(invitation.startswith("gt7relay://"))
        self.assertEqual(len(self.private_workspaces()), 1)
        self.assertTrue(all(not car.has_data for car in self.engineer_fleet.snapshots()))
        self.assertTrue(all(not car.has_data for car in self.trial.local_fleet.snapshots()))
        self.assertFalse((self.data_dir / "sessions").exists())
        self.assertEqual(settings.read_text(encoding="utf-8"), saved)
        self.assertTrue(all("gt7relay://" not in message for message in self.messages))
        self.assertTrue(all("invitation-distante-conservee" not in message for message in self.messages))
        if __import__("os").name != "nt":
            self.assertEqual(self.private_workspaces()[0].stat().st_mode & 0o777, 0o700)

    def test_engineer_data_arrives_only_after_real_relay_sample_and_challenge(self):
        entered = threading.Event()
        release = threading.Event()
        operations = []
        original_send = ClientConnection.send

        def gated_send(connection, message, *args, **kwargs):
            operation = None
            if isinstance(message, str):
                try:
                    operation = json.loads(message).get("op")
                except (ValueError, TypeError, AttributeError):
                    pass
            if operation:
                operations.append(operation)  # Aucun contenu ni secret conservé.
            if operation == "sample":
                entered.set()
                release.wait(timeout=1.0)
            return original_send(connection, message, *args, **kwargs)

        with patch.object(ClientConnection, "send", gated_send):
            self.trial.start("192.168.0.44", record=False)
            # Comme un pilote réel, attendre la fin du handshake avant cette
            # émission synthétique unique. Les captures préconnexion sont
            # volontairement abandonnées par le client pour rester fraîches.
            wait_for(lambda: any(message.startswith("Relais connecté :") for message in self.messages))
            telemetry = sample(234.0)
            try:
                ControlledReceiver.instances[-1].emit(telemetry)
                self.assertAlmostEqual(self.trial.local_fleet.snapshots()[2].speed_kmh, 234.0)
                self.assertTrue(entered.wait(timeout=2.0), "Le pilote n'a pas émis de trame relais")
                self.assertFalse(self.engineer_fleet.snapshots()[2].has_data,
                                 "Une copie directe contournerait le WebSocket et son challenge")
                self.assertIn("challenge", operations[:operations.index("sample")])
            finally:
                release.set()
            wait_for(lambda: bool(self.engineer_fleet.received))
        snapshot = self.engineer_fleet.snapshots()[2]
        self.assertAlmostEqual(snapshot.speed_kmh, 234.0)
        self.assertEqual(snapshot.fuel_percent, 65.0)
        self.assertEqual(snapshot.tyre_temperatures, (65.0, 66.0, 67.0, 68.0))
        self.assertEqual(snapshot.lap, 2)
        number, received, source_thread, clock = self.engineer_fleet.received[-1]
        self.assertEqual(number, 3)
        self.assertIsNot(received, telemetry)
        self.assertIsNot(source_thread, threading.current_thread())
        self.assertIn("now", clock, "L'ingénieur doit dater la réception à partir du challenge")
        self.assertLessEqual(clock["now"], time.monotonic())
        self.assertTrue(all(not car.has_data for index, car in enumerate(self.engineer_fleet.snapshots()) if index != 2))

    def test_persistent_csv_keeps_all_samples_after_ephemeral_relay_is_removed(self):
        settings = self.data_dir / "settings.json"
        settings.write_text("profil conservé", encoding="utf-8")
        unrelated = self.data_dir / "circuits"
        unrelated.mkdir()
        marker = unrelated / "conserver.txt"
        marker.write_text("ne pas effacer", encoding="utf-8")
        self.trial.start("192.168.0.44", record=True)
        wait_for(lambda: any(message.startswith("Relais connecté :") for message in self.messages))
        pilot = self.trial.pilot
        receiver = ControlledReceiver.instances[-1]
        workspace = self.private_workspaces()[0]
        port = self.trial.hub.port
        for speed in range(60, 120):
            receiver.emit(sample(float(speed)))
        wait_for(lambda: math.isclose(self.engineer_fleet.snapshots()[2].speed_kmh or 0, 119.0))
        self.trial.stop()
        self.assertEqual(pilot.rows_written, 60)
        self.assertEqual(pilot.rows_dropped, 0)
        self.assertEqual(pilot.file_path.parent, self.data_dir / "sessions")
        with pilot.file_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter=";"))
        self.assertEqual(len(rows), 60)
        for expected, row in zip(range(60, 120), rows):
            self.assertAlmostEqual(float(row["speed_kmh"]), expected)
        self.assertFalse(workspace.exists())
        self.assertFalse(self.private_workspaces())
        self.assertEqual(marker.read_text(encoding="utf-8"), "ne pas effacer")
        self.assertEqual(settings.read_text(encoding="utf-8"), "profil conservé")
        self.assert_port_closed(port)
        self.assertFalse(self.trial.running)
        self.assertIsNone(self.trial.pilot)
        self.assertIsNone(self.trial.engineer)
        self.assertIsNone(self.trial.hub)
        self.assertTrue(all(not car.has_data for car in self.engineer_fleet.snapshots()))
        self.assertTrue(all(not car.has_data for car in self.trial.local_fleet.snapshots()))

    def test_more_than_sixteen_restarts_do_not_accumulate_rooms_or_old_samples(self):
        for attempt in range(17):
            with self.subTest(attempt=attempt):
                self.trial.start("192.168.0.44", record=False)
                self.assertTrue(self.trial.running)
                self.assertTrue(self.trial.engineer.connected)
                self.assertEqual(len(self.private_workspaces()), 1)
                self.assertTrue(all(not car.has_data for car in self.engineer_fleet.snapshots()))
                port = self.trial.hub.port
                self.trial.stop()
                self.trial.stop()
                self.assert_port_closed(port)
                self.assertFalse(self.private_workspaces())
        self.assertEqual(len(ControlledReceiver.instances), 17)
        self.assertTrue(all(receiver.stopped for receiver in ControlledReceiver.instances))
        self.assertFalse((self.data_dir / "sessions").exists())

    def test_active_trial_refuses_a_second_capture(self):
        self.trial.start("192.168.0.44", record=False)
        server = self.trial.hub
        with self.assertRaisesRegex(RuntimeError, "déjà en cours"):
            self.trial.start("192.168.0.45", record=False)
        self.assertIs(self.trial.hub, server)
        self.assertEqual(len(ControlledReceiver.instances), 1)
        self.assertEqual(len(self.private_workspaces()), 1)

    def test_udp_start_failure_rolls_back_csv_engineer_relay_and_workspace(self):
        servers = []
        pilots = []
        class TrackingServer(RelayServer):
            def start(server):
                super().start()
                servers.append(server.port)
        class TrackingPilot(PilotSession):
            def __init__(pilot, *args, **kwargs):
                super().__init__(*args, **kwargs)
                pilots.append(pilot)
        ControlledReceiver.fail_start = True
        with patch("racecontrol.relay_trial.RelayServer", TrackingServer), \
             patch("racecontrol.relay_trial.PilotSession", TrackingPilot):
            with self.assertRaisesRegex(OSError, "UDP indisponible"):
                self.trial.start("192.168.0.44", record=True)
        self.assertFalse(self.trial.running)
        self.assertIsNone(self.trial.hub)
        self.assertIsNone(self.trial.engineer)
        self.assertIsNone(self.trial.pilot)
        self.assertTrue(ControlledReceiver.instances[-1].stopped)
        self.assertFalse(pilots[-1].running)
        self.assertFalse(pilots[-1].recording_active)
        self.assert_port_closed(servers[-1])
        self.assertFalse(self.private_workspaces())
        pilots[-1].file_path.unlink()  # Le CSV n'est pas resté verrouillé sous Windows.
        ControlledReceiver.fail_start = False
        self.trial.start(None, record=False)
        self.assertTrue(self.trial.running)

    def test_relay_failure_after_bind_releases_socket_without_starting_capture(self):
        ports = []
        class FailingServer(RelayServer):
            def start(server):
                super().start()
                ports.append(server.port)
                raise OSError("Échec après ouverture du relais")
        with patch("racecontrol.relay_trial.RelayServer", FailingServer):
            with self.assertRaisesRegex(OSError, "ouverture du relais"):
                self.trial.start(None)
        self.assertFalse(self.trial.running)
        self.assertFalse(ControlledReceiver.instances)
        self.assert_port_closed(ports[-1])
        self.assertFalse(self.private_workspaces())

    def test_engineer_readiness_timeout_stops_started_relay_and_removes_private_state(self):
        ports = []
        original_start = RelayServer.start
        def remember_port(server):
            original_start(server)
            ports.append(server.port)
        with patch.object(RelayServer, "start", remember_port), \
             patch("racecontrol.relay_trial.RelayEngineerSession") as factory:
            factory.return_value.wait_ready.return_value = False
            with self.assertRaisesRegex(TimeoutError, "délai prévu"):
                self.trial.start(None)
            factory.return_value.stop.assert_called_once()
        self.assertFalse(ControlledReceiver.instances)
        self.assert_port_closed(ports[-1])
        self.assertFalse(self.private_workspaces())
        self.assertFalse(self.trial.running)

    def test_pilot_constructor_failure_closes_ready_engineer_and_relay(self):
        ports = []
        original_start = RelayServer.start
        def remember_port(server):
            original_start(server)
            ports.append(server.port)
        with patch.object(RelayServer, "start", remember_port), \
             patch("racecontrol.relay_trial.PilotSession", side_effect=OSError("Création pilote impossible")):
            with self.assertRaisesRegex(OSError, "Création pilote"):
                self.trial.start(None)
        self.assertFalse(ControlledReceiver.instances)
        self.assert_port_closed(ports[-1])
        self.assertFalse(self.private_workspaces())
        self.assertIsNone(self.trial.engineer)

    def test_stop_attempts_each_component_in_order_even_after_a_pilot_error(self):
        self.trial.start("192.168.0.44", record=False)
        pilot, engineer, server = self.trial.pilot, self.trial.engineer, self.trial.hub
        temporary = self.trial._tempdir
        port = server.port
        order = []
        def stop_pilot():
            order.append("pilot")
            pilot_stop()
            raise OSError("Erreur secondaire de fermeture")
        def stop_engineer():
            order.append("engineer")
            engineer_stop()
        def stop_server():
            order.append("server")
            server_stop()
        def cleanup():
            order.append("temporary")
            temporary_cleanup()
        pilot_stop, engineer_stop, server_stop = pilot.stop, engineer.stop, server.stop
        temporary_cleanup = temporary.cleanup
        with patch.object(pilot, "stop", stop_pilot), patch.object(engineer, "stop", stop_engineer), \
             patch.object(server, "stop", stop_server), patch.object(temporary, "cleanup", cleanup):
            with self.assertRaisesRegex(RuntimeError, "arrêté complètement"):
                self.trial.stop()
        self.assertEqual(order, ["pilot", "engineer", "server", "temporary"])
        self.assert_port_closed(port)
        self.assertFalse(self.private_workspaces())
        self.assertFalse(self.trial.running)
        self.assertIsNone(self.trial.hub)

    def test_invalid_slot_is_rejected_before_any_profile_is_created(self):
        for slot in (True, 0, 5, 1.0, "1"):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                RelayLocalTrial(FleetMonitor(), self.data_dir, car_number=slot)
        self.assertFalse(tuple(self.data_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
