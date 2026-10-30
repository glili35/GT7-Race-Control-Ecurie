"""État de quatre voitures pour un poste d'ingénieur (sans dépendance Tkinter).

Les échantillons arrivent d'agents/récepteurs distincts; ce module ne suppose pas
qu'une seule PS5 émet la télémétrie des quatre voitures.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import Any

@dataclass(frozen=True)
class CarSnapshot:
    number: int
    connected: bool
    age_s: float | None
    speed_kmh: float | None
    fuel_l: float | None
    lap: int | None
    position_x: float | None
    position_z: float | None
    alerts: tuple[str, ...]

@dataclass
class _CarState:
    last_seen: float | None = None
    telemetry: Any = None
    alerts: list[str] = field(default_factory=list)

class FleetMonitor:
    """Thread-safe state manager for exactly four independently sourced cars."""
    def __init__(self, timeout_s: float = 3.0):
        if timeout_s <= 0:
            raise ValueError('timeout_s doit être positif')
        self.timeout_s = timeout_s
        self._cars = {n: _CarState() for n in range(1, 5)}
        self._lock = RLock()

    def ingest(self, number: int, telemetry: Any, *, now: float | None = None) -> None:
        if number not in self._cars:
            raise ValueError('Numéro de voiture attendu : 1 à 4')
        timestamp = monotonic() if now is None else now
        with self._lock:
            state = self._cars[number]
            state.telemetry = telemetry
            state.last_seen = timestamp
            state.alerts = self._make_alerts(telemetry)

    @staticmethod
    def _make_alerts(t: Any) -> list[str]:
        alerts = []
        fuel = getattr(t, 'fuel', None)
        capacity = getattr(t, 'fuel_capacity', None)
        if isinstance(fuel, (int, float)) and isinstance(capacity, (int, float)) and capacity > 0:
            if fuel / capacity < 0.10:
                alerts.append('CARBURANT CRITIQUE')
            elif fuel / capacity < 0.20:
                alerts.append('CARBURANT BAS')
        return alerts

    def snapshots(self, *, now: float | None = None) -> tuple[CarSnapshot, ...]:
        timestamp = monotonic() if now is None else now
        with self._lock:
            result = []
            for n, state in self._cars.items():
                age = None if state.last_seen is None else max(0., timestamp - state.last_seen)
                online = age is not None and age <= self.timeout_s
                t = state.telemetry if online else None
                result.append(CarSnapshot(
                    number=n, connected=online, age_s=age,
                    speed_kmh=getattr(t, 'speed_kmh', None),
                    fuel_l=getattr(t, 'fuel', None),
                    lap=getattr(t, 'current_lap', None),
                    position_x=getattr(t, 'position_x', None),
                    position_z=getattr(t, 'position_z', None),
                    alerts=tuple(state.alerts) if online else ('SIGNAL PERDU',),
                ))
            return tuple(result)
