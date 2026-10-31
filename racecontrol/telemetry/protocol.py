"""
GT7 Race Control Ecurie
Protocole UDP et déchiffrement Salsa20 de Gran Turismo 7.

Formats reconnus par taille :
A : 296 octets
B : 316 octets
~ : 344 octets
C : 368 octets

Le déchiffrement utilise exclusivement la constante associée
au format détecté. Aucun essai de constante de secours n'est effectué.

Le marqueur G7S0 est vérifié après déchiffrement.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum

from Crypto.Cipher import Salsa20


# Clé GT7 : premiers 32 octets de la chaîne documentée.
GT7_KEY = b"Simulator Interface Packet GT7 ver 0.0"[:32]

# Marqueur attendu au début du paquet déchiffré.
GT7_MAGIC = 0x47375330
GT7_MAGIC_BYTES = struct.pack("<I", GT7_MAGIC)

# Position de l'IV dans le datagramme.
IV_OFFSET = 0x40

# Tailles des formats reconnus.
FORMAT_SIZES = {
    296: "A",
    316: "B",
    344: "~",
    368: "C",
}

# Constantes XOR associées aux formats.
FORMAT_CONSTANTS = {
    "A": 0xDEADBEAF,
    "B": 0xDEADBEEF,
    "~": 0x55FABB4F,
    "C": 0xDEADBEEF,
}


class GT7PacketFormat(str, Enum):
    A = "A"
    B = "B"
    TILDE = "~"
    C = "C"


class PacketFormatError(ValueError):
    """Taille de paquet GT7 inconnue."""


class DecryptionError(ValueError):
    """Échec du déchiffrement ou du contrôle du marqueur GT7."""


@dataclass(frozen=True)
class DecryptionResult:
    """Résultat du déchiffrement et détails de traçabilité."""

    plaintext: bytes
    packet_format: GT7PacketFormat
    nonce: bytes
    constant: int


def detect_packet_format(datagram: bytes) -> GT7PacketFormat:
    """
    Détecte le format à partir de la taille du datagramme UDP.
    """
    try:
        name = FORMAT_SIZES[len(datagram)]
    except KeyError:
        raise PacketFormatError(
            f"Taille de paquet inconnue : {len(datagram)} octets."
        ) from None

    return (
        GT7PacketFormat.TILDE
        if name == "~"
        else GT7PacketFormat(name)
    )


def build_nonce(datagram: bytes, xor_constant: int) -> bytes:
    """
    Construit le nonce Salsa20 GT7.

    IV1 est lu en little-endian à l'offset 0x40.
    IV2 est calculé par XOR avec la constante du format.
    Le nonce est IV2 suivi de IV1, tous deux en little-endian.
    """
    if len(datagram) < IV_OFFSET + 4:
        raise DecryptionError(
            "Datagramme trop court pour lire l'IV."
        )

    iv1 = struct.unpack_from(
        "<I",
        datagram,
        IV_OFFSET,
    )[0]

    iv2 = iv1 ^ xor_constant

    return struct.pack("<II", iv2, iv1)


def decrypt_packet(
    datagram: bytes,
    *,
    return_details: bool = False,
) -> bytes | DecryptionResult:
    """
    Déchiffre un datagramme GT7 avec Salsa20.

    Une seule constante est utilisée :
    celle associée au format détecté.

    Aucun essai de constante alternative n'est effectué.

    Args:
        datagram: paquet UDP GT7 reçu.
        return_details: si True, retourne aussi le format,
                        le nonce et la constante utilisés.

    Returns:
        Les données déchiffrées ou un DecryptionResult.

    Raises:
        PacketFormatError: taille de paquet inconnue.
        DecryptionError: erreur Salsa20 ou marqueur G7S0 absent.
    """
    packet_format = detect_packet_format(datagram)

    if len(datagram) < IV_OFFSET + 4:
        raise DecryptionError(
            "Datagramme trop court pour contenir l'IV."
        )

    # Sélection unique de la constante correspondant au format.
    constant = FORMAT_CONSTANTS[packet_format.value]

    # Construction du nonce avec cette seule constante.
    nonce = build_nonce(datagram, constant)

    try:
        cipher = Salsa20.new(
            key=GT7_KEY,
            nonce=nonce,
        )

        plaintext = cipher.decrypt(datagram)

    except (ValueError, TypeError) as exc:
        raise DecryptionError(
            "Erreur Salsa20. "
            f"Format={packet_format.value}, "
            f"constante=0x{constant:08X}, "
            f"nonce={nonce.hex().upper()}. "
            f"Détail : {exc}"
        ) from exc

    # Vérification du marqueur GT7 après déchiffrement.
    if plaintext[:4] != GT7_MAGIC_BYTES:
        raise DecryptionError(
            "Échec de vérification du marqueur G7S0. "
            f"Format={packet_format.value}, "
            f"constante utilisée=0x{constant:08X}, "
            f"nonce={nonce.hex().upper()}. "
            "Aucune constante de secours n'a été essayée."
        )

    result = DecryptionResult(
        plaintext=plaintext,
        packet_format=packet_format,
        nonce=nonce,
        constant=constant,
    )

    if return_details:
        return result

    return plaintext