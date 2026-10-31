
"""
GT7 Race Control Ecurie
Package du moteur de télémétrie Gran Turismo 7.

Ce package regroupe :
- le protocole réseau GT7 ;
- le déchiffrement Salsa20 ;
- le décodage des paquets ;
- les diagnostics de réception ;
- le récepteur UDP et le heartbeat.
"""

from .protocol import (
    GT7PacketFormat,
    PacketFormatError,
    DecryptionError,
    DecryptionResult,
    detect_packet_format,
    decrypt_packet,
)

from .decoder import (
    GT7Telemetry,
    decode_packet,
)

from .diagnostics import (
    DiagnosticsSnapshot,
    TelemetryDiagnostics,
)

from .receiver import (
    GT7Receiver,
)

__all__ = [
    "GT7PacketFormat",
    "PacketFormatError",
    "DecryptionError",
    "DecryptionResult",
    "detect_packet_format",
    "decrypt_packet",
    "GT7Telemetry",
    "decode_packet",
    "DiagnosticsSnapshot",
    "TelemetryDiagnostics",
    "GT7Receiver",
]

__version__ = "0.1.0"
