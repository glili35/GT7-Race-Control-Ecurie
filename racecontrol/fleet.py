"""Thread-safe state and independent fuel strategy for four GT7 sources."""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from time import monotonic
from typing import Any

from .strategy import FuelEstimate, FuelStrategy, activity_state, finite_number, fuel_value, integer


@dataclass(frozen=True)
class CarSnapshot:
    number: int
    connected: bool
    age_s: float | None
    speed_kmh: float | None
    fuel_l: float | None  # Legacy raw value: the name does not certify litre units.
    lap: int | None
    position_x: float | None
    position_z: float | None
    alerts: tuple[str, ...]
    rpm: float | None = None
    current_gear: int | None = None
    throttle_percent: float | None = None
    brake_percent: float | None = None
    fuel_percent: float | None = None
    tyre_temperatures: tuple[float | None, ...] | None = None
    total_laps: int | None = None
    last_lap_ms: int | None = None
    best_lap_ms: int | None = None
    race_position: int | None = None
    consumption_percent_per_lap: float | None = None
    remaining_laps: float | None = None
    fuel_needed_percent: float | None = None
    measured_laps: int = 0
    strategy_status: str = "EN ATTENTE DE TOURS COMPLETS"
    has_data: bool = False
    activity_state: str = "unknown"

    @property
    def current_lap(self) -> int | None:
        return self.lap


@dataclass
class _CarState:
    last_seen: float | None = None
    telemetry: Any = None
    strategy: FuelStrategy = field(default_factory=FuelStrategy)
    estimate: FuelEstimate = field(default_factory=FuelEstimate)


def _field(t: Any, name: str, minimum: float | None = None, maximum: float | None = None) -> float | None:
    value = finite_number(getattr(t, name, None))
    if value is not None and (minimum is None or value >= minimum) and (maximum is None or value <= maximum):
        return value
    return None


def _lap_time(value: Any) -> int | None:
    result = integer(value, minimum=1)
    return None if result == 0xFFFFFFFF else result


class FleetMonitor:
    """State for exactly four cars, each with its own source and lap history."""

    def __init__(self, timeout_s: float = 3.0):
        timeout = finite_number(timeout_s)
        if timeout is None or timeout <= 0:
            raise ValueError("timeout_s doit être positif et fini")
        self.timeout_s = timeout
        self._cars = {number: _CarState() for number in range(1, 5)}
        self._lock = RLock()

    def ingest(self, number: int, telemetry: Any, *, now: float | None = None) -> None:
        if isinstance(number, bool) or not isinstance(number, int) or number not in self._cars:
            raise ValueError("Numéro de voiture attendu : 1 à 4")
        timestamp = monotonic() if now is None else finite_number(now)
        if timestamp is None:
            raise ValueError("Horodatage invalide")
        with self._lock:
            state = self._cars[number]
            if state.last_seen is not None:
                if timestamp < state.last_seen:
                    return  # A delayed callback must not replace newer data.
                if timestamp - state.last_seen > self.timeout_s:
                    state.strategy.reset()  # New observation after a signal loss.
            state.estimate = state.strategy.update(telemetry)
            state.telemetry = telemetry
            state.last_seen = timestamp

    def reset(self, number: int | None = None) -> None:
        """Clear a session, for example when stopping a receiver or demo."""
        if number is not None and (isinstance(number, bool) or not isinstance(number, int) or number not in self._cars):
            raise ValueError("Numéro de voiture attendu : 1 à 4")
        with self._lock:
            for target in self._cars if number is None else (number,):
                self._cars[target] = _CarState()

    @staticmethod
    def _fuel_percent(t: Any) -> float | None:
        fuel = fuel_value(t)
        capacity = _field(t, "fuel_capacity", minimum=0)
        if fuel is None or capacity is None or capacity <= 0 or not 0 <= fuel <= capacity:
            return None
        return fuel / capacity * 100.0

    @staticmethod
    def _make_alerts(t: Any, estimate: FuelEstimate | None = None) -> tuple[str, ...]:
        alerts = []
        percent = FleetMonitor._fuel_percent(t)
        if percent is not None:
            if percent < 10:
                alerts.append("CARBURANT CRITIQUE")
            elif percent < 20:
                alerts.append("CARBURANT BAS")
        if estimate is not None and estimate.insufficient:
            alerts.append("CARBURANT ESTIMÉ INSUFFISANT")
        return tuple(alerts)

    def snapshots(self, *, now: float | None = None) -> tuple[CarSnapshot, ...]:
        timestamp = monotonic() if now is None else finite_number(now)
        if timestamp is None:
            raise ValueError("Horodatage invalide")
        with self._lock:
            result = []
            for number, state in self._cars.items():
                age = None if state.last_seen is None else max(0.0, timestamp - state.last_seen)
                online = age is not None and age <= self.timeout_s
                activity = activity_state(state.telemetry) if online else "unknown"
                visible = online and activity not in ("loading", "inactive")
                t = state.telemetry if visible else None
                estimate = state.estimate if visible else FuelEstimate()
                tyres = getattr(t, "tyre_temperatures", None)
                if isinstance(tyres, (list, tuple)) and len(tyres) == 4:
                    tyres = tuple(finite_number(temperature) for temperature in tyres)
                else:
                    tyres = None
                raw_fuel = fuel_value(t)
                if raw_fuel is not None and raw_fuel < 0:
                    raw_fuel = None
                result.append(CarSnapshot(
                    number=number,
                    connected=online,
                    age_s=age,
                    speed_kmh=_field(t, "speed_kmh", minimum=0, maximum=1000),
                    fuel_l=raw_fuel,
                    lap=integer(getattr(t, "current_lap", None), maximum=32767),
                    position_x=_field(t, "position_x"),
                    position_z=_field(t, "position_z"),
                    alerts=self._make_alerts(t, estimate) if visible else (("SIGNAL PERDU",) if not online and state.last_seen is not None else ()),
                    rpm=_field(t, "rpm", minimum=0, maximum=30000),
                    current_gear=integer(getattr(t, "current_gear", None), maximum=15),
                    throttle_percent=_field(t, "throttle_percent", minimum=0, maximum=100),
                    brake_percent=_field(t, "brake_percent", minimum=0, maximum=100),
                    fuel_percent=self._fuel_percent(t),
                    tyre_temperatures=tyres,
                    total_laps=integer(getattr(t, "total_laps", None), maximum=32767),
                    last_lap_ms=_lap_time(getattr(t, "last_lap_ms", None)),
                    best_lap_ms=_lap_time(getattr(t, "best_lap_ms", None)),
                    race_position=integer(getattr(t, "race_position", None), minimum=1, maximum=32767),
                    consumption_percent_per_lap=estimate.consumption_percent_per_lap,
                    remaining_laps=estimate.remaining_laps,
                    fuel_needed_percent=estimate.fuel_needed_percent,
                    measured_laps=estimate.measured_laps,
                    strategy_status=estimate.status,
                    has_data=state.last_seen is not None,
                    activity_state=activity,
                ))
            return tuple(result)
