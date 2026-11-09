"""Régressions du récepteur sur UDP loopback, sans véritable console GT7."""
from contextlib import ExitStack
import errno
import queue
import socket
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from racecontrol.telemetry.receiver import GT7Receiver

try:
    from test_end_to_end import free_udp_port, synthetic_car_packet
except ModuleNotFoundError:
    from tests.test_end_to_end import free_udp_port, synthetic_car_packet


class ReceiverNetworkTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.clock = SimpleNamespace(value=100.0)
        self.clock_patch = patch(
            "racecontrol.telemetry.receiver.time",
            SimpleNamespace(monotonic=lambda: self.clock.value),
        )
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.sender_one = self.resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self.sender_one.bind(("127.0.0.1", 0))
        self.sender_two = self.resources.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self.sender_two.bind(("127.0.0.2", 0))
        self.received = queue.Queue()

    def receiver(self, ps5_ip=None, port=None):
        receiver = GT7Receiver(
            ps5_ip=ps5_ip, listen_ip="127.0.0.1", listen_port=port or free_udp_port(),
            on_telemetry=self.received.put,
        )
        # Exercer les vraies réceptions et le vrai déchiffrement, sans envoyer
        # de heartbeat broadcast hors du loopback pendant ces tests.
        receiver._heartbeat_loop = lambda: None
        self.resources.callback(receiver.stop)
        return receiver

    def send(self, sender, receiver, number=1, packet=None):
        sender.sendto(packet if packet is not None else synthetic_car_packet(number),
                      ("127.0.0.1", receiver.listen_port))

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Le récepteur n'a pas traité le datagramme UDP local.")

    def test_second_listener_is_refused_and_port_can_be_reused_after_stop(self):
        first = self.receiver(ps5_ip="127.0.0.1")
        first.start()
        second = self.receiver(ps5_ip="127.0.0.1", port=first.listen_port)
        with self.assertRaises(OSError):
            second.start()
        self.assertFalse(second.running)
        self.send(self.sender_one, first)
        self.assertEqual(self.received.get(timeout=2).car_id, 1001)
        first.stop()
        second.start()
        self.send(self.sender_one, second, number=2)
        self.assertEqual(self.received.get(timeout=2).car_id, 1002)

    def test_auto_discovery_keeps_one_source_until_valid_telemetry_expires(self):
        receiver = self.receiver()
        receiver.start()
        self.send(self.sender_one, receiver, number=1)
        self.assertEqual(self.received.get(timeout=2).car_id, 1001)
        self.assertEqual(receiver.discovered_ps5_ip, "127.0.0.1")
        self.assertEqual(receiver._heartbeat_destination(), "127.0.0.1")

        self.send(self.sender_two, receiver, number=2)
        self.wait_for(lambda: receiver.diagnostics.snapshot().rejected_source_packets == 1)
        self.assertTrue(self.received.empty())
        self.assertEqual(receiver.diagnostics.snapshot().decoded_packets, 1)
        self.assertEqual(receiver.discovered_ps5_ip, "127.0.0.1")

        # L'horloge avance sans attendre trois secondes en temps réel.
        self.clock.value = 103.1
        self.assertEqual(receiver._heartbeat_destination(), "255.255.255.255")
        self.send(self.sender_two, receiver, packet=b"pas un paquet GT7")
        self.wait_for(lambda: receiver.diagnostics.snapshot().packet_format_errors == 1)
        self.assertEqual(receiver.discovered_ps5_ip, "127.0.0.1")
        self.assertEqual(receiver._heartbeat_destination(), "255.255.255.255")

        # Une autre IP ne devient sélectionnée qu'après un décodage réussi.
        self.send(self.sender_two, receiver, number=2)
        self.assertEqual(self.received.get(timeout=2).car_id, 1002)
        self.assertEqual(receiver.discovered_ps5_ip, "127.0.0.2")
        self.assertEqual(receiver._heartbeat_destination(), "127.0.0.2")
        self.send(self.sender_one, receiver, number=1)
        self.wait_for(lambda: receiver.diagnostics.snapshot().rejected_source_packets == 2)
        self.assertTrue(self.received.empty())

    def test_manual_ip_is_preserved_after_loss_and_other_sources_stay_rejected(self):
        receiver = self.receiver(ps5_ip="127.0.0.1")
        receiver.start()
        self.send(self.sender_one, receiver)
        self.received.get(timeout=2)
        self.clock.value = 120.0
        self.assertEqual(receiver._heartbeat_destination(), "127.0.0.1")
        self.send(self.sender_two, receiver, number=2)
        self.wait_for(lambda: receiver.diagnostics.snapshot().rejected_source_packets == 1)
        self.assertTrue(self.received.empty())
        self.assertEqual(receiver.discovered_ps5_ip, "127.0.0.1")

    def test_restart_clears_discovery_and_can_learn_a_different_console(self):
        receiver = self.receiver()
        receiver.start()
        self.send(self.sender_one, receiver)
        self.received.get(timeout=2)
        receiver.stop()
        receiver.start()
        self.assertIsNone(receiver.discovered_ps5_ip)
        self.assertIsNone(receiver.last_telemetry_age)
        self.assertEqual(receiver._heartbeat_destination(), "255.255.255.255")
        self.send(self.sender_two, receiver, number=2)
        self.assertEqual(self.received.get(timeout=2).car_id, 1002)
        self.assertEqual(receiver.discovered_ps5_ip, "127.0.0.2")

    def test_address_in_use_explains_how_to_release_the_port(self):
        udp = MagicMock()
        udp.bind.side_effect = OSError(errno.EADDRINUSE, "Adresse déjà utilisée")
        receiver = self.receiver(ps5_ip="127.0.0.1")
        with patch("racecontrol.telemetry.receiver.socket.socket", return_value=udp):
            with self.assertRaises(OSError) as failure:
                receiver.start()
        self.assertEqual(failure.exception.errno, errno.EADDRINUSE)
        self.assertIn(str(receiver.listen_port), str(failure.exception))
        self.assertIn("Ferme l'autre connexion", str(failure.exception))
        udp.close.assert_called_once()

    def test_other_bind_errors_are_preserved_without_relabelling_them(self):
        original = OSError(errno.EACCES, "Accès refusé par le système")
        udp = MagicMock()
        udp.bind.side_effect = original
        receiver = self.receiver(ps5_ip="127.0.0.1")
        with patch("racecontrol.telemetry.receiver.socket.socket", return_value=udp):
            with self.assertRaises(OSError) as failure:
                receiver.start()
        self.assertIs(failure.exception, original)
        udp.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
