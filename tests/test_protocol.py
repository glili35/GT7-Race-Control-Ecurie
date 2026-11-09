"""
Tests automatisés du protocole GT7.

Ces tests utilisent des paquets synthétiques créés localement.
Ils ne constituent pas des tests réalisés avec une véritable PS5.
"""

import struct
import unittest

from Crypto.Cipher import Salsa20

from racecontrol.telemetry.protocol import (
    GT7_KEY,
    GT7_MAGIC_BYTES,
    GT7PacketFormat,
    PacketFormatError,
    DecryptionError,
    detect_packet_format,
    build_nonce,
    decrypt_packet,
    FORMAT_CONSTANTS,
)


def create_synthetic_packet(
    packet_size: int,
    constant: int,
    *,
    magic: bytes = GT7_MAGIC_BYTES,
) -> bytes:
    """
    Crée un paquet synthétique pour tester le déchiffrement GT7.

    L'IV est rétabli à son emplacement dans le datagramme
    après le chiffrement, afin que decrypt_packet puisse
    reconstruire le nonce attendu.
    """
    plaintext = bytearray(packet_size)
    plaintext[:4] = magic

    iv1 = 0x12345678
    iv2 = iv1 ^ constant

    nonce = struct.pack("<II", iv2, iv1)

    cipher = Salsa20.new(
        key=GT7_KEY,
        nonce=nonce,
    )

    encrypted = bytearray(
        cipher.encrypt(bytes(plaintext))
    )

    # Rétablit l'IV dans le datagramme reçu.
    struct.pack_into(
        "<I",
        encrypted,
        0x40,
        iv1,
    )

    return bytes(encrypted)

class TestGT7Protocol(unittest.TestCase):

    def test_format_a(self):
        self.assertEqual(
            detect_packet_format(bytes(296)),
            GT7PacketFormat.A,
        )

    def test_format_b(self):
        self.assertEqual(
            detect_packet_format(bytes(316)),
            GT7PacketFormat.B,
        )

    def test_format_tilde(self):
        self.assertEqual(
            detect_packet_format(bytes(344)),
            GT7PacketFormat.TILDE,
        )

    def test_format_c(self):
        self.assertEqual(
            detect_packet_format(bytes(368)),
            GT7PacketFormat.C,
        )

    def test_unknown_size(self):
        with self.assertRaises(PacketFormatError):
            detect_packet_format(bytes(100))

    def test_nonce_construction(self):
        packet = bytearray(296)
        iv1 = 0x12345678

        struct.pack_into(
            "<I",
            packet,
            0x40,
            iv1,
        )

        nonce = build_nonce(
            bytes(packet),
            FORMAT_CONSTANTS["A"],
        )

        iv2 = iv1 ^ FORMAT_CONSTANTS["A"]

        expected = struct.pack(
            "<II",
            iv2,
            iv1,
        )

        self.assertEqual(nonce, expected)
        self.assertEqual(len(nonce), 8)

    def test_nonce_packet_too_short(self):
        with self.assertRaises(DecryptionError):
            build_nonce(
                bytes(20),
                FORMAT_CONSTANTS["A"],
            )

    def test_decryption_format_a_success(self):
        packet = create_synthetic_packet(
            296,
            FORMAT_CONSTANTS["A"],
        )

        result = decrypt_packet(
            packet,
            return_details=True,
        )

        self.assertEqual(
            result.packet_format,
            GT7PacketFormat.A,
        )

        self.assertEqual(
            result.constant,
            FORMAT_CONSTANTS["A"],
        )

        self.assertEqual(
            result.plaintext[:4],
            GT7_MAGIC_BYTES,
        )

        self.assertEqual(
            result.nonce,
            build_nonce(
                packet,
                FORMAT_CONSTANTS["A"],
            ),
        )

    def test_decryption_returns_plaintext_by_default(self):
        packet = create_synthetic_packet(
            296,
            FORMAT_CONSTANTS["A"],
        )

        plaintext = decrypt_packet(packet)

        self.assertIsInstance(plaintext, bytes)

        self.assertEqual(
            plaintext[:4],
            GT7_MAGIC_BYTES,
        )

    def test_wrong_constant_is_rejected_without_fallback(self):
        packet = create_synthetic_packet(
            296,
            FORMAT_CONSTANTS["B"],
        )

        with self.assertRaises(DecryptionError) as context:
            decrypt_packet(
                packet,
                return_details=True,
            )

        message = str(context.exception)

        self.assertIn(
            "constante utilisée=0xDEADBEAF",
            message,
        )

        self.assertIn(
            "Aucune constante de secours",
            message,
        )

    def test_wrong_magic_is_rejected(self):
        packet = create_synthetic_packet(
            296,
            FORMAT_CONSTANTS["A"],
            magic=b"FAIL",
        )

        with self.assertRaises(DecryptionError) as context:
            decrypt_packet(packet)

        self.assertIn(
            "Échec de vérification du marqueur G7S0",
            str(context.exception),
        )

    def test_format_b_decryption(self):
        packet = create_synthetic_packet(
            316,
            FORMAT_CONSTANTS["B"],
        )

        result = decrypt_packet(
            packet,
            return_details=True,
        )

        self.assertEqual(
            result.packet_format,
            GT7PacketFormat.B,
        )

        self.assertEqual(
            result.constant,
            FORMAT_CONSTANTS["B"],
        )

    def test_format_tilde_decryption(self):
        packet = create_synthetic_packet(
            344,
            FORMAT_CONSTANTS["~"],
        )

        result = decrypt_packet(
            packet,
            return_details=True,
        )

        self.assertEqual(
            result.packet_format,
            GT7PacketFormat.TILDE,
        )

        self.assertEqual(
            result.constant,
            FORMAT_CONSTANTS["~"],
        )

    def test_format_c_decryption(self):
        packet = create_synthetic_packet(
            368,
            FORMAT_CONSTANTS["C"],
        )

        result = decrypt_packet(
            packet,
            return_details=True,
        )

        self.assertEqual(
            result.packet_format,
            GT7PacketFormat.C,
        )

        self.assertEqual(
            result.constant,
            FORMAT_CONSTANTS["C"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)