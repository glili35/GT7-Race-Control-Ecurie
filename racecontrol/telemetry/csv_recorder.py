
"""Enregistrement CSV de la télémétrie GT7.

Un appel à record() écrit une ligne pour chaque paquet décodé.
Aucune limitation artificielle de fréquence n'est appliquée.
"""

import csv
from datetime import datetime
from pathlib import Path
from uuid import uuid4


class GT7CSVRecorder:
    """Enregistre une session GT7 dans un fichier CSV."""

    FIELDNAMES = [
        "timestamp",
        "packet_format",
        "position_x",
        "position_y",
        "position_z",
        "velocity_x",
        "velocity_y",
        "velocity_z",
        "speed_kmh",
        "rpm",
        "fuel_level",
        "fuel_capacity",
        "oil_pressure",
        "water_temperature",
        "oil_temperature",
        "tyre_front_left",
        "tyre_front_right",
        "tyre_rear_left",
        "tyre_rear_right",
        "current_lap",
        "total_laps",
        "best_lap_ms",
        "last_lap_ms",
        "race_position",
        "race_position_total",
        "current_gear",
        "suggested_gear",
        "throttle_percent",
        "brake_percent",
        "car_id",
    ]

    def __init__(self, output_directory="sessions"):
        self.output_directory = Path(output_directory)
        self.output_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        timestamp = datetime.now().strftime(
            "%Y-%m-%d_%H-%M-%S_%f"
        )

        self.file_path = (
            self.output_directory
            / f"session_{timestamp}_{uuid4().hex}.csv"
        )

        # L'horloge Windows peut retourner deux fois le même instant lors
        # d'un redémarrage rapide. L'UUID différencie les sessions ; le mode
        # exclusif protège aussi une session existante en cas de collision.
        self._file = open(
            self.file_path,
            mode="x",
            newline="",
            encoding="utf-8",
        )

        self._writer = csv.DictWriter(
            self._file,
            fieldnames=self.FIELDNAMES,
            delimiter=";",
        )

        self._writer.writeheader()
        self.rows_written = 0

    def record(self, telemetry, *, timestamp=None):
        """Écrit un paquet, avec sa date de réception si elle est fournie.

        Les appelants historiques peuvent continuer à ne fournir que le paquet.
        Une date ISO ou un datetime permet à un writer asynchrone de conserver
        l'instant de réception au lieu de dater le vidage de sa file.
        """

        if timestamp is None:
            timestamp = datetime.now()
        elif isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        timestamp_text = timestamp.isoformat(timespec="milliseconds")

        tyres = telemetry.tyre_temperatures

        row = {
            "timestamp": timestamp_text,
            "packet_format": telemetry.packet_format,
            "position_x": telemetry.position_x,
            "position_y": telemetry.position_y,
            "position_z": telemetry.position_z,
            "velocity_x": telemetry.velocity_x,
            "velocity_y": telemetry.velocity_y,
            "velocity_z": telemetry.velocity_z,
            "speed_kmh": telemetry.speed_kmh,
            "rpm": telemetry.rpm,
            "fuel_level": telemetry.fuel_level,
            "fuel_capacity": telemetry.fuel_capacity,
            "oil_pressure": telemetry.oil_pressure,
            "water_temperature": telemetry.water_temperature,
            "oil_temperature": telemetry.oil_temperature,
            "tyre_front_left": tyres[0],
            "tyre_front_right": tyres[1],
            "tyre_rear_left": tyres[2],
            "tyre_rear_right": tyres[3],
            "current_lap": telemetry.current_lap,
            "total_laps": telemetry.total_laps,
            "best_lap_ms": telemetry.best_lap_ms,
            "last_lap_ms": telemetry.last_lap_ms,
            "race_position": telemetry.race_position,
            "race_position_total": (
                telemetry.race_position_total
            ),
            "current_gear": telemetry.current_gear,
            "suggested_gear": telemetry.suggested_gear,
            "throttle_percent": telemetry.throttle_percent,
            "brake_percent": telemetry.brake_percent,
            "car_id": telemetry.car_id,
        }

        self._writer.writerow(row)
        self.rows_written += 1

    def close(self):
        """Ferme proprement le fichier CSV."""
        if not self._file.closed:
            self._file.flush()
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
