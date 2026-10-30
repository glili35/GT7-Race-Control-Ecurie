
"""
GT7 Race Control Ecurie
Récepteur UDP et heartbeat Gran Turismo 7.

Configuration réseau :
- PC : écoute UDP sur le port 33740.
- PS5 : reçoit le heartbeat sur le port 33739.
- Heartbeat par défaut : caractère A.
- Intervalle du heartbeat : 1,6 seconde.

Le récepteur ne considère une adresse IP comme celle de la PS5
qu'après réception et décodage réussi d'un paquet GT7.
"""

from __future__ import annotations

import socket
import ipaddress
import struct
import threading
import time

from typing import Callable, Optional

from .decoder import GT7Telemetry, decode_packet
from .diagnostics import TelemetryDiagnostics
from .protocol import (
    DecryptionError,
    PacketFormatError,
)


TelemetryCallback = Callable[[GT7Telemetry], None]
StatusCallback = Callable[[str], None]


class GT7Receiver:
    """Récepteur réseau GT7 pour une PS5 et un PC."""

    def __init__(
        self,
        *,
        ps5_ip: Optional[str] = None,
        listen_ip: str = "0.0.0.0",
        listen_port: int = 33740,
        heartbeat_port: int = 33739,
        heartbeat_interval: float = 1.6,
        heartbeat: bytes = b"A",
        on_telemetry: Optional[TelemetryCallback] = None,
        on_status: Optional[StatusCallback] = None,
        diagnostics: Optional[TelemetryDiagnostics] = None,
    ) -> None:

        if not 1 <= listen_port <= 65535:
            raise ValueError("Port d'écoute invalide.")

        if not 1 <= heartbeat_port <= 65535:
            raise ValueError("Port heartbeat invalide.")

        if heartbeat_interval <= 0:
            raise ValueError(
                "L'intervalle heartbeat doit être positif."
            )

        if not isinstance(heartbeat, bytes) or len(heartbeat) != 1:
            raise ValueError(
                "Le heartbeat doit être un seul octet, par exemple b'A'."
            )

        if ps5_ip is not None:
            try:
                ps5_ip = str(ipaddress.IPv4Address(ps5_ip))
            except ipaddress.AddressValueError as exc:
                raise ValueError("Adresse IPv4 PS5 invalide") from exc
        self.ps5_ip = ps5_ip
        self.listen_ip = listen_ip
        self.listen_port = listen_port
        self.heartbeat_port = heartbeat_port
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat = heartbeat

        self.on_telemetry = on_telemetry
        self.on_status = on_status

        self.diagnostics = diagnostics or TelemetryDiagnostics()

        self._socket: Optional[socket.socket] = None

        self._receiver_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()

        self._running = False
        self._discovered_ps5_ip: Optional[str] = None

        self._last_rejection_log = 0.0
        self._last_stale_notification = 0.0
        self._last_telemetry_time: Optional[float] = None

    @property
    def running(self) -> bool:
        """Indique si le récepteur est démarré."""

        with self._state_lock:
            return self._running

    @property
    def discovered_ps5_ip(self) -> Optional[str]:
        """Adresse IP découverte après décodage d'un paquet valide."""

        with self._state_lock:
            return self._discovered_ps5_ip

    @property
    def last_telemetry_age(self) -> Optional[float]:
        """Âge de la dernière télémétrie valide, en secondes."""

        with self._state_lock:
            if self._last_telemetry_time is None:
                return None

            return max(
                0.0,
                time.monotonic() - self._last_telemetry_time,
            )

    def _emit_status(self, message: str) -> None:
        """Transmet un message d'état sans interrompre le récepteur."""

        if self.on_status is None:
            return

        try:
            self.on_status(message)
        except Exception as exc:
            self.diagnostics.record_error(
                f"Erreur dans le callback de statut : {exc}"
            )

    def start(self) -> None:
        """Démarre le socket UDP et les deux threads."""

        with self._state_lock:
            if self._running:
                return

            self._stop_event.clear()
            self._discovered_ps5_ip = None
            self._last_telemetry_time = None
            self._last_stale_notification = 0.0

            udp_socket = socket.socket(
                socket.AF_INET,
                socket.SOCK_DGRAM,
            )

            try:
                udp_socket.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_REUSEADDR,
                    1,
                )

                udp_socket.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_BROADCAST,
                    1,
                )

                udp_socket.bind(
                    (self.listen_ip, self.listen_port)
                )

                udp_socket.settimeout(0.25)

            except OSError:
                udp_socket.close()
                raise

            self._socket = udp_socket
            self._running = True

            self._receiver_thread = threading.Thread(
                target=self._receive_loop,
                name="GT7-UDP-Receiver",
                daemon=True,
            )

            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="GT7-Heartbeat",
                daemon=True,
            )

            self._receiver_thread.start()
            self._heartbeat_thread.start()

        self._emit_status(
            f"Récepteur GT7 démarré sur "
            f"{self.listen_ip}:{self.listen_port}."
        )

    def stop(self) -> None:
        """Arrête les threads et ferme le socket UDP."""

        with self._state_lock:
            if not self._running:
                return

            self._running = False
            self._stop_event.set()

            receiver_thread = self._receiver_thread
            heartbeat_thread = self._heartbeat_thread
            udp_socket = self._socket

        if udp_socket is not None:
            try:
                udp_socket.close()
            except OSError as exc:
                self.diagnostics.record_error(
                    f"Erreur fermeture socket : {exc}"
                )

        current_thread = threading.current_thread()

        for thread in (receiver_thread, heartbeat_thread):
            if (
                thread is not None
                and thread is not current_thread
            ):
                thread.join(timeout=2.0)

        with self._state_lock:
            self._socket = None
            self._receiver_thread = None
            self._heartbeat_thread = None

        self._emit_status("Récepteur GT7 arrêté.")

    def _heartbeat_destination(self) -> str:
        """Détermine l'adresse de destination du heartbeat."""

        with self._state_lock:
            if self.ps5_ip:
                return self.ps5_ip

            if self._discovered_ps5_ip:
                return self._discovered_ps5_ip

        return "255.255.255.255"

    def _heartbeat_loop(self) -> None:
        """Envoie le heartbeat périodiquement."""

        while not self._stop_event.is_set():

            udp_socket = self._socket

            if udp_socket is None:
                break

            destination = self._heartbeat_destination()

            try:
                udp_socket.sendto(
                    self.heartbeat,
                    (destination, self.heartbeat_port),
                )

            except OSError as exc:
                if not self._stop_event.is_set():
                    self.diagnostics.record_error(
                        f"Erreur heartbeat UDP : {exc}"
                    )

            self._stop_event.wait(
                self.heartbeat_interval
            )

    def _receive_loop(self) -> None:
        """Reçoit et traite les datagrammes UDP."""

        while not self._stop_event.is_set():

            udp_socket = self._socket

            if udp_socket is None:
                break

            try:
                datagram, source = udp_socket.recvfrom(4096)

            except socket.timeout:
                self._check_stale_telemetry()
                continue

            except OSError as exc:
                if not self._stop_event.is_set():
                    self.diagnostics.record_error(
                        f"Erreur réception UDP : {exc}"
                    )
                break

            source_ip = source[0]

            # Si une IP PS5 est configurée, ignorer
            # immédiatement les paquets provenant des autres machines.
            if (
                self.ps5_ip is not None
                and source_ip != self.ps5_ip
            ):
                self.diagnostics.record_rejected_source()
                now = time.monotonic()
                if now - self._last_rejection_log >= 30:
                    self._last_rejection_log = now
                    self._emit_status("Paquets UDP ignorés : source différente de la PS5 configurée.")
                continue

            self.diagnostics.record_datagram(source_ip)

            try:
                telemetry = decode_packet(datagram)

            except PacketFormatError as exc:
                self.diagnostics.record_packet_format_error(
                    str(exc)
                )
                continue

            except DecryptionError as exc:
                self.diagnostics.record_decryption_error(
                    str(exc)
                )
                continue

            except (ValueError, struct.error) as exc:
                self.diagnostics.record_decode_error(
                    str(exc)
                )
                continue

            except Exception as exc:
                self.diagnostics.record_error(
                    f"Erreur inattendue de décodage : {exc}"
                )
                continue

            self.diagnostics.record_decoded()

            with self._state_lock:
                first_discovery = (
                    self._discovered_ps5_ip is None
                    and self.ps5_ip is None
                )

                self._discovered_ps5_ip = source_ip
                self._last_telemetry_time = time.monotonic()

                self._last_stale_notification = 0.0

            if first_discovery:
                self._emit_status(
                    f"Console GT7 détectée : {source_ip}"
                )

            if self.on_telemetry is not None:
                try:
                    self.on_telemetry(telemetry)

                except Exception as exc:
                    self.diagnostics.record_error(
                        f"Erreur callback télémétrie : {exc}"
                    )

    def _check_stale_telemetry(self) -> None:
        """Signale une perte de télémétrie après trois secondes."""

        with self._state_lock:
            if self._last_telemetry_time is None:
                return

            age = time.monotonic() - self._last_telemetry_time

            if age < 3.0:
                return

            if self._last_stale_notification != 0.0:
                return

            self._last_stale_notification = time.monotonic()

        self._emit_status(
            "Aucune télémétrie GT7 valide depuis plus de 3 secondes."
        )

    def __enter__(self) -> "GT7Receiver":
        self.start()
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ) -> None:
        self.stop()
