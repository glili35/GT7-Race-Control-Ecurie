
"""
Tests synthetiques du decodeur GT7.

Le dechiffrement est simule pour isoler et verifier
la lecture des champs telemetriques.
Aucun paquet reel PS5 n'est utilise.
"""

import struct
import unittest
from unittest.mock import patch

from racecontrol.telemetry.decoder import decode_packet
from racecontrol.telemetry.protocol import (
    DecryptionResult,
    GT7PacketFormat,
)


def make_plaintext():
    """Construit un paquet dechiffre synthetique de 296 octets."""

    data = bytearray(296)

    struct.pack_into("<fff", data, 0x04, 1.0, 2.0, 3.0)
    struct.pack_into("<fff", data, 0x10, 10.0, 20.0, 30.0)

    struct.pack_into("<f", data, 0x3C, 8000.0)
    struct.pack_into("<fff", data, 0x44, 40.0, 60.0, 50.0)

    struct.pack_into(
        "<ffff",
        data,
        0x50,
        1.2,
        4.5,
        90.0,
        100.0,
    )

    struct.pack_into(
        "<ffff",
        data,
        0x60,
        80.0,
        81.0,
        82.0,
        83.0,
    )

    struct.pack_into("<hh", data, 0x74, 3, 10)
    struct.pack_into("<II", data, 0x78, 90000, 92000)
    struct.pack_into("<hh", data, 0x84, 2, 12)

    struct.pack_into("<H", data, 0x8E, 0x0009)

    data[0x90] = 0x53
    data[0x91] = 255
    data[0x92] = 128

    struct.pack_into(
        "<ffff",
        data,
        0xA4,
        10.0,
        11.0,
        12.0,
        13.0,
    )

    struct.pack_into(
        "<ffff",
        data,
        0xB4,
        0.33,
        0.34,
        0.35,
        0.36,
    )

    struct.pack_into("<I", data, 0x124, 12345)

    return bytes(data)


class TestGT7Decoder(unittest.TestCase):

    def setUp(self):
        self.plaintext = make_plaintext()

        self.decryption_result = DecryptionResult(
            plaintext=self.plaintext,
            packet_format=GT7PacketFormat.A,
            nonce=bytes(8),
            constant=0xDEADBEAF,
        )

    @patch("racecontrol.telemetry.decoder.decrypt_packet")
    def test_decode_basic_values(self, mocked_decrypt):
        mocked_decrypt.return_value = self.decryption_result

        telemetry = decode_packet(bytes(296))

        self.assertEqual(telemetry.position_x, 1.0)
        self.assertEqual(telemetry.position_y, 2.0)
        self.assertEqual(telemetry.position_z, 3.0)

        self.assertEqual(telemetry.rpm, 8000.0)
        self.assertEqual(telemetry.speed_ms, 50.0)
        self.assertEqual(telemetry.speed_kmh, 180.0)

        self.assertEqual(telemetry.fuel_level, 40.0)
        self.assertEqual(telemetry.fuel_capacity, 60.0)

        self.assertEqual(telemetry.current_lap, 3)
        self.assertEqual(telemetry.total_laps, 10)

        self.assertEqual(telemetry.race_position, 2)
        self.assertEqual(telemetry.race_position_total, 12)

        self.assertEqual(telemetry.car_id, 12345)

    @patch("racecontrol.telemetry.decoder.decrypt_packet")
    def test_decode_gear_and_pedals(self, mocked_decrypt):
        mocked_decrypt.return_value = self.decryption_result

        telemetry = decode_packet(bytes(296))

        self.assertEqual(telemetry.current_gear, 3)
        self.assertEqual(telemetry.suggested_gear, 5)

        self.assertEqual(telemetry.throttle_raw, 255)
        self.assertEqual(telemetry.brake_raw, 128)

        self.assertEqual(telemetry.throttle_percent, 100.0)

        self.assertAlmostEqual(
            telemetry.brake_percent,
            128 * 100.0 / 255.0,
        )

    @patch("racecontrol.telemetry.decoder.decrypt_packet")
    def test_decode_tyre_values(self, mocked_decrypt):
        mocked_decrypt.return_value = self.decryption_result

        telemetry = decode_packet(bytes(296))

        self.assertEqual(
            telemetry.tyre_temperatures,
            (80.0, 81.0, 82.0, 83.0),
        )

        expected_radii = (0.33, 0.34, 0.35, 0.36)

        for actual, expected in zip(
            telemetry.tyre_radii,
            expected_radii,
        ):
            self.assertAlmostEqual(actual, expected, places=5)

        self.assertEqual(
            telemetry.wheel_angular_speeds,
            (10.0, 11.0, 12.0, 13.0),
        )

    @patch("racecontrol.telemetry.decoder.decrypt_packet")
    def test_decode_flags(self, mocked_decrypt):
        mocked_decrypt.return_value = self.decryption_result

        telemetry = decode_packet(bytes(296))

        self.assertTrue(telemetry.on_track)
        self.assertFalse(telemetry.paused)
        self.assertFalse(telemetry.loading)
        self.assertTrue(telemetry.in_gear)

    @patch("racecontrol.telemetry.decoder.decrypt_packet")
    def test_reject_invalid_rpm(self, mocked_decrypt):
        data = bytearray(self.plaintext)
        struct.pack_into("<f", data, 0x3C, 50000.0)

        mocked_decrypt.return_value = DecryptionResult(
            plaintext=bytes(data),
            packet_format=GT7PacketFormat.A,
            nonce=bytes(8),
            constant=0xDEADBEAF,
        )

        with self.assertRaises(ValueError):
            decode_packet(bytes(296))


if __name__ == "__main__":
    unittest.main(verbosity=2)
