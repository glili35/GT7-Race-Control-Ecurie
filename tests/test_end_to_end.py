"""Intégration locale avec paquets GT7 synthétiques, sans véritable PS5.

Ce test exerce de vrais sockets UDP, le déchiffrement Salsa20, quatre clients
HTTPS et l'état ingénieur. Il ne valide ni une console, ni un réseau Internet.
"""
from contextlib import ExitStack
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest

from racecontrol.fleet import FleetMonitor
from racecontrol.telemetry import GT7Receiver
from racecontrol.telemetry.protocol import FORMAT_CONSTANTS
from racecontrol.transport import EngineerHub, PilotLink

try:  # Compatible avec unittest discover et l'import du module par son package.
    from test_protocol import create_synthetic_packet
except ModuleNotFoundError:
    from tests.test_protocol import create_synthetic_packet


def synthetic_car_packet(number):
    """Personnalise le paquet chiffré de référence à texte clair initial nul.

    Salsa20 est un chiffrement par flot : appliquer XOR aux octets chiffrés
    correspondants modifie uniquement ces champs. L'IV réservé à 0x40 reste
    inchangé et le paquet passe ensuite par le véritable déchiffreur.
    """
    packet = bytearray(create_synthetic_packet(296, FORMAT_CONSTANTS["A"]))

    def field(offset, format, *values):
        for index, byte in enumerate(struct.pack(format, *values)):
            packet[offset + index] ^= byte

    field(0x04, "<fff", number * 100.0, 0.0, number * -20.0)
    field(0x3C, "<f", 4000.0 + number * 100)
    field(0x44, "<fff", number * 15.0, 100.0, number * 10.0)
    field(0x60, "<ffff", 60.0 + number, 70.0 + number, 80.0 + number, 90.0 + number)
    field(0x74, "<hh", 3, 10)
    field(0x78, "<II", 90000 + number, 91000 + number)
    field(0x84, "<hh", number, 4)
    field(0x8E, "<H", 0x0009)
    field(0x90, "<BBB", 0x40 | number, number * 50, number * 10)
    field(0x124, "<I", 1000 + number)
    return bytes(packet)


def free_udp_port():
    # GT7Receiver refuse le port 0 ; réserver puis démarrer immédiatement sur
    # le port éphémère choisi par le système évite les ports PS5 réels.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("La chaîne UDP → HTTPS → ingénieur n'a pas atteint l'état attendu.")


class EndToEndTests(unittest.TestCase):
    def test_four_encrypted_udp_sources_reach_independent_tls_car_slots_and_expire(self):
        fleet = FleetMonitor(timeout_s=1.0)
        with ExitStack() as resources:
            directory = resources.enter_context(tempfile.TemporaryDirectory())
            heartbeat_sink = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            heartbeat_sink.bind(("127.0.0.1", 0))
            sender = resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            hub = EngineerHub(fleet.ingest, Path(directory), host="127.0.0.1", port=0)
            hub.start()
            resources.callback(hub.stop)
            receivers = {}
            packets = {number: synthetic_car_packet(number) for number in range(1, 5)}
            for number in range(1, 5):
                link = PilotLink(hub.invitation(number, "127.0.0.1"))
                link.start()
                resources.callback(link.stop)
                receiver = GT7Receiver(
                    ps5_ip="127.0.0.1", listen_ip="127.0.0.1",
                    listen_port=free_udp_port(), heartbeat_port=heartbeat_sink.getsockname()[1],
                    on_telemetry=link.publish,
                )
                receiver.start()
                resources.callback(receiver.stop)
                receivers[number] = receiver

            stopped = threading.Event()
            source_lock = threading.Lock()
            active_sources = {1, 2, 3, 4}
            feeder_errors = []

            def feed():
                try:
                    while not stopped.is_set():
                        with source_lock:
                            targets = tuple(active_sources)
                        for number in targets:
                            sender.sendto(packets[number], ("127.0.0.1", receivers[number].listen_port))
                        stopped.wait(0.02)
                except OSError as exc:
                    feeder_errors.append(exc)

            feeder = threading.Thread(target=feed, name="synthetic-gt7-sources", daemon=True)
            feeder.start()

            def stop_feeder():
                stopped.set()
                feeder.join(timeout=2.0)

            resources.callback(stop_feeder)
            wait_for(lambda: all(car.connected for car in fleet.snapshots()))
            for number, car in enumerate(fleet.snapshots(), start=1):
                with self.subTest(car=number):
                    self.assertEqual(car.number, number)
                    self.assertAlmostEqual(car.speed_kmh, number * 36.0)
                    self.assertAlmostEqual(car.fuel_percent, number * 15.0)
                    self.assertEqual(car.tyre_temperatures, (60.0 + number, 70.0 + number, 80.0 + number, 90.0 + number))
                    self.assertEqual(car.position_x, number * 100.0)
                    self.assertEqual(car.position_z, number * -20.0)
                    self.assertEqual(car.current_gear, number)
                    self.assertEqual(car.last_lap_ms, 91000 + number)
                    diagnostics = receivers[number].diagnostics.snapshot()
                    self.assertGreater(diagnostics.decoded_packets, 0)
                    self.assertEqual(diagnostics.decryption_errors, 0)
                    self.assertEqual(diagnostics.decode_errors, 0)

            # Supprimer une seule source UDP doit faire périmer cette voiture,
            # sans rafraîchissement artificiel par un ancien paquet retransmis.
            with source_lock:
                active_sources.remove(2)

            def isolated_signal_loss():
                cars = fleet.snapshots()
                return not cars[1].connected and all(cars[index].connected for index in (0, 2, 3))

            wait_for(isolated_signal_loss)
            cars = fleet.snapshots()
            self.assertTrue(cars[1].has_data)
            self.assertIn("SIGNAL PERDU", cars[1].alerts)
            self.assertIsNone(cars[1].speed_kmh)
            self.assertIsNone(cars[1].fuel_percent)
            self.assertIsNone(cars[1].tyre_temperatures)
            self.assertFalse(feeder_errors)


if __name__ == "__main__":
    unittest.main()
