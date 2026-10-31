"""Tests synthétiques : aucune PS5 n'est utilisée."""
import unittest
from racecontrol.telemetry.receiver import GT7Receiver
from racecontrol.telemetry.diagnostics import TelemetryDiagnostics

class TestReceiverSecurity(unittest.TestCase):
    def test_reject_invalid_ip(self):
        for ip in ("999.1.2.3", "not-an-ip", "::1"):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                GT7Receiver(ps5_ip=ip)

    def test_configured_ip(self):
        receiver = GT7Receiver(ps5_ip="192.168.1.10")
        self.assertEqual(receiver._heartbeat_destination(), "192.168.1.10")

    def test_rejected_source_counter(self):
        diag = TelemetryDiagnostics()
        diag.record_rejected_source()
        diag.record_rejected_source()
        self.assertEqual(diag.snapshot().rejected_source_packets, 2)
        diag.reset()
        self.assertEqual(diag.snapshot().rejected_source_packets, 0)

if __name__ == "__main__":
    unittest.main()
