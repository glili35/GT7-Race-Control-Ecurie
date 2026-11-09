"""Données fictives pour découvrir l'interface, sans ouvrir de socket."""
from __future__ import annotations

import math
from types import SimpleNamespace


def circuit_points():
    return [(600 * math.cos(i * math.tau / 240),
             350 * math.sin(i * math.tau / 240), "L") for i in range(240)]


def sample(number: int, elapsed: float) -> SimpleNamespace:
    """Tour accéléré de 18 s, explicitement réservé à la démonstration."""
    lap_duration = 18 + number
    progress = elapsed / lap_duration + (number - 1) * .12
    angle = progress * math.tau
    return SimpleNamespace(
        speed_kmh=145 + 65 * math.sin(angle * 2), rpm=6800 + 1800 * math.sin(angle * 2),
        current_gear=3 + int(2 * (1 + math.sin(angle * 2)) / 2),
        throttle_percent=max(0, 90 * math.sin(angle * 2 + 1)),
        brake_percent=max(0, -65 * math.sin(angle * 2 + 1)),
        fuel_level=max(3, 88 - progress * (3.7 + number * .2)), fuel_capacity=100,
        tyre_temperatures=tuple(65 + 12 * math.sin(angle + n * .3) for n in range(4)),
        current_lap=int(progress) + 1, total_laps=20,
        last_lap_ms=int(lap_duration * 1000), best_lap_ms=int(lap_duration * 990),
        race_position=number, position_x=600 * math.cos(angle),
        position_z=350 * math.sin(angle), flags=1, on_track=True, paused=False,
        loading=False, car_id=1000 + number,
    )
