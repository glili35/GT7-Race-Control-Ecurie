
"""
GT7 Race Control Ecurie
Décodeur des données télémétriques GT7.

Les offsets ci-dessous concernent les champs de base documentés
du paquet déchiffré.

Les extensions spécifiques aux formats ne sont pas interprétées
tant qu'elles ne sont pas suffisamment vérifiées.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from .protocol import (
    GT7PacketFormat,
    decrypt_packet,
)


def _read_float(data: bytes, offset: int) -> float:
    """Lit un float 32 bits little-endian et vérifie sa validité."""
    value = struct.unpack_from("<f", data, offset)[0]

    if not math.isfinite(value):
        raise ValueError(
            f"Valeur flottante invalide à l'offset 0x{offset:X}."
        )

    return value


def _read_uint8(data: bytes, offset: int) -> int:
    return struct.unpack_from("<B", data, offset)[0]


def _read_uint16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _read_int16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<h", data, offset)[0]


def _read_uint32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _read_float_array(
    data: bytes,
    offset: int,
    count: int = 4,
) -> tuple[float, ...]:
    return tuple(
        _read_float(data, offset + index * 4)
        for index in range(count)
    )


@dataclass(frozen=True)
class GT7Telemetry:
    """Valeurs extraites d'un paquet GT7 déchiffré."""

    packet_format: GT7PacketFormat

    position_x: float
    position_y: float
    position_z: float

    velocity_x: float
    velocity_y: float
    velocity_z: float

    rpm: float

    fuel_level: float
    fuel_capacity: float

    speed_ms: float

    boost: float
    oil_pressure: float
    water_temperature: float
    oil_temperature: float

    tyre_temperatures: tuple[float, float, float, float]

    current_lap: int
    total_laps: int

    best_lap_ms: int
    last_lap_ms: int

    race_position: int
    race_position_total: int

    flags: int

    current_gear: int
    suggested_gear: int

    throttle_raw: int
    brake_raw: int

    wheel_angular_speeds: tuple[float, float, float, float]
    tyre_radii: tuple[float, float, float, float]

    car_id: int

    @property
    def speed_kmh(self) -> float:
        """Convertit la vitesse de m/s en km/h."""
        return self.speed_ms * 3.6

    @property
    def throttle_percent(self) -> float:
        """Convertit la valeur brute de l'accélérateur en pourcentage."""
        return self.throttle_raw * 100.0 / 255.0

    @property
    def brake_percent(self) -> float:
        """Convertit la valeur brute du frein en pourcentage."""
        return self.brake_raw * 100.0 / 255.0

    @property
    def on_track(self) -> bool:
        return bool(self.flags & (1 << 0))

    @property
    def paused(self) -> bool:
        return bool(self.flags & (1 << 1))

    @property
    def loading(self) -> bool:
        return bool(self.flags & (1 << 2))

    @property
    def in_gear(self) -> bool:
        return bool(self.flags & (1 << 3))

    @property
    def turbo_active(self) -> bool:
        return bool(self.flags & (1 << 4))

    @property
    def rev_limiter_active(self) -> bool:
        return bool(self.flags & (1 << 5))

    @property
    def handbrake_active(self) -> bool:
        return bool(self.flags & (1 << 6))


def decode_packet(datagram: bytes) -> GT7Telemetry:
    """
    Déchiffre et décode un paquet GT7.

    Args:
        datagram: datagramme UDP reçu depuis GT7.

    Returns:
        GT7Telemetry contenant les champs de base décodés.

    Raises:
        PacketFormatError: format inconnu.
        DecryptionError: déchiffrement impossible.
        ValueError: données incohérentes ou paquet incomplet.
    """

    result = decrypt_packet(
        datagram,
        return_details=True,
    )

    data = result.plaintext
    packet_format = result.packet_format

    # Les champs de base utilisés ici vont jusqu'à l'offset 0x128.
    if len(data) < 0x128:
        raise ValueError(
            f"Paquet déchiffré trop court : {len(data)} octets."
        )

    # Rapport engagé :
    # 4 bits faibles = rapport actuel
    # 4 bits forts = rapport conseillé.
    gear_byte = _read_uint8(data, 0x90)

    telemetry = GT7Telemetry(
        packet_format=packet_format,

        position_x=_read_float(data, 0x04),
        position_y=_read_float(data, 0x08),
        position_z=_read_float(data, 0x0C),

        velocity_x=_read_float(data, 0x10),
        velocity_y=_read_float(data, 0x14),
        velocity_z=_read_float(data, 0x18),

        rpm=_read_float(data, 0x3C),

        fuel_level=_read_float(data, 0x44),
        fuel_capacity=_read_float(data, 0x48),

        speed_ms=_read_float(data, 0x4C),

        boost=_read_float(data, 0x50),
        oil_pressure=_read_float(data, 0x54),
        water_temperature=_read_float(data, 0x58),
        oil_temperature=_read_float(data, 0x5C),

        tyre_temperatures=_read_float_array(data, 0x60),

        current_lap=_read_int16(data, 0x74),
        total_laps=_read_int16(data, 0x76),

        best_lap_ms=_read_uint32(data, 0x78),
        last_lap_ms=_read_uint32(data, 0x7C),

        race_position=_read_int16(data, 0x84),
        race_position_total=_read_int16(data, 0x86),

        flags=_read_uint16(data, 0x8E),

        current_gear=gear_byte & 0x0F,
        suggested_gear=(gear_byte >> 4) & 0x0F,

        throttle_raw=_read_uint8(data, 0x91),
        brake_raw=_read_uint8(data, 0x92),

        # Les quatre roues commencent à 0xA4.
        wheel_angular_speeds=_read_float_array(data, 0xA4),

        # Les rayons des pneus commencent à 0xB4.
        tyre_radii=_read_float_array(data, 0xB4),

        car_id=_read_uint32(data, 0x124),
    )

    _validate_telemetry(telemetry)

    return telemetry


def _validate_telemetry(telemetry: GT7Telemetry) -> None:
    """
    Vérifie les plages générales des valeurs décodées.

    Ces contrôles détectent des erreurs évidentes de décodage.
    Ils ne constituent pas une validation de référence PS5.
    """

    if not 0 <= telemetry.rpm <= 30000:
        raise ValueError(
            f"Régime moteur incohérent : {telemetry.rpm}"
        )

    if not -5 <= telemetry.speed_ms <= 200:
        raise ValueError(
            f"Vitesse incohérente : {telemetry.speed_ms} m/s"
        )

    if telemetry.fuel_capacity < 0:
        raise ValueError("Capacité de carburant négative.")

    if telemetry.fuel_level < 0:
        raise ValueError("Niveau de carburant négatif.")

    if telemetry.current_lap < 0:
        raise ValueError("Numéro de tour négatif.")

    if telemetry.total_laps < 0:
        raise ValueError("Nombre total de tours négatif.")

    if not 0 <= telemetry.throttle_raw <= 255:
        raise ValueError("Valeur d'accélérateur invalide.")

    if not 0 <= telemetry.brake_raw <= 255:
        raise ValueError("Valeur de frein invalide.")

    for index, temperature in enumerate(
        telemetry.tyre_temperatures
    ):
        if not -100 <= temperature <= 300:
            raise ValueError(
                f"Température du pneu {index} incohérente : "
                f"{temperature}"
            )
