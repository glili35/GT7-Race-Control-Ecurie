
"""
GT7 Race Control Ecurie
Diagnostic et statistiques du moteur de télémétrie.

Les compteurs sont protégés par un verrou afin de pouvoir être
utilisés sans risque depuis plusieurs threads.
"""

from __future__ import annotations

import threading
import time

from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DiagnosticsSnapshot:
    """Photographie immuable de l'état des diagnostics."""

    received_datagrams: int
    decoded_packets: int
    rejected_source_packets: int

    packet_format_errors: int
    decryption_errors: int
    decode_errors: int

    last_source_ip: Optional[str]
    last_packet_age_seconds: Optional[float]

    packets_per_second: float

    last_error: Optional[str]
    last_error_age_seconds: Optional[float]


class TelemetryDiagnostics:
    """
    Collecte les statistiques de réception et de décodage.

    Les méthodes record_* sont prévues pour être appelées par
    le récepteur UDP et ses threads.
    """

    def __init__(self, rate_window_seconds: float = 5.0) -> None:
        if rate_window_seconds <= 0:
            raise ValueError(
                "La fenêtre de calcul doit être supérieure à zéro."
            )

        self._lock = threading.Lock()
        self._rate_window_seconds = float(rate_window_seconds)

        self._received_datagrams = 0
        self._decoded_packets = 0
        self._rejected_source_packets = 0

        self._packet_format_errors = 0
        self._decryption_errors = 0
        self._decode_errors = 0

        self._last_source_ip: Optional[str] = None
        self._last_packet_time: Optional[float] = None

        self._last_error: Optional[str] = None
        self._last_error_time: Optional[float] = None

        self._recent_packet_times: deque[float] = deque()

    def record_datagram(self, source_ip: Optional[str] = None) -> None:
        """Enregistre la réception d'un datagramme UDP."""

        now = time.monotonic()

        with self._lock:
            self._received_datagrams += 1
            self._last_packet_time = now

            if source_ip:
                self._last_source_ip = source_ip

            self._recent_packet_times.append(now)
            self._remove_old_packet_times(now)

    def record_rejected_source(self) -> None:
        """Compte les datagrammes écartés avant déchiffrement."""
        with self._lock:
            self._rejected_source_packets += 1

    def record_decoded(self) -> None:
        """Enregistre un paquet GT7 correctement décodé."""

        with self._lock:
            self._decoded_packets += 1

    def record_packet_format_error(self, message: str) -> None:
        """Enregistre une taille de paquet non reconnue."""

        with self._lock:
            self._packet_format_errors += 1
            self._set_last_error(message)

    def record_decryption_error(self, message: str) -> None:
        """Enregistre un échec de déchiffrement."""

        with self._lock:
            self._decryption_errors += 1
            self._set_last_error(message)

    def record_decode_error(self, message: str) -> None:
        """Enregistre une erreur pendant le décodage des champs."""

        with self._lock:
            self._decode_errors += 1
            self._set_last_error(message)

    def record_error(self, message: str) -> None:
        """Enregistre une erreur générale du récepteur."""

        with self._lock:
            self._set_last_error(message)

    def _set_last_error(self, message: str) -> None:
        """À appeler uniquement lorsque le verrou est déjà acquis."""

        self._last_error = str(message)
        self._last_error_time = time.monotonic()

    def _remove_old_packet_times(self, now: float) -> None:
        """Supprime les dates situées hors de la fenêtre de calcul."""

        cutoff = now - self._rate_window_seconds

        while (
            self._recent_packet_times
            and self._recent_packet_times[0] < cutoff
        ):
            self._recent_packet_times.popleft()

    def snapshot(self) -> DiagnosticsSnapshot:
        """Retourne une photographie cohérente des diagnostics."""

        now = time.monotonic()

        with self._lock:
            self._remove_old_packet_times(now)

            if self._last_packet_time is None:
                packet_age = None
            else:
                packet_age = max(
                    0.0,
                    now - self._last_packet_time,
                )

            if self._last_error_time is None:
                error_age = None
            else:
                error_age = max(
                    0.0,
                    now - self._last_error_time,
                )

            rate = (
                len(self._recent_packet_times)
                / self._rate_window_seconds
            )

            return DiagnosticsSnapshot(
                received_datagrams=self._received_datagrams,
                decoded_packets=self._decoded_packets,
                rejected_source_packets=self._rejected_source_packets,

                packet_format_errors=self._packet_format_errors,
                decryption_errors=self._decryption_errors,
                decode_errors=self._decode_errors,

                last_source_ip=self._last_source_ip,
                last_packet_age_seconds=packet_age,

                packets_per_second=rate,

                last_error=self._last_error,
                last_error_age_seconds=error_age,
            )

    def reset(self) -> None:
        """Remet tous les compteurs et informations à zéro."""

        with self._lock:
            self._received_datagrams = 0
            self._decoded_packets = 0
            self._rejected_source_packets = 0

            self._packet_format_errors = 0
            self._decryption_errors = 0
            self._decode_errors = 0

            self._last_source_ip = None
            self._last_packet_time = None

            self._last_error = None
            self._last_error_time = None

            self._recent_packet_times.clear()
