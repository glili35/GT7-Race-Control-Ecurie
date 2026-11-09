"""Fuel estimates from complete laps, expressed as tank percentages.

GT7's raw fuel values are used as a ratio to its reported capacity; no litre
unit is assumed. Estimates are advisory and do not model timed-race finish rules.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Any


def finite_number(value: Any) -> float | None:
    """Accept actual finite numeric values, never bools or numeric strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def integer(value: Any, *, minimum: int = 0, maximum: int = 2**32 - 1) -> int | None:
    number = finite_number(value)
    if number is None or not number.is_integer() or not minimum <= number <= maximum:
        return None
    return int(number)


def fuel_value(telemetry: Any) -> float | None:
    """Prefer the real decoder field while supporting the historical fleet API."""
    value = getattr(telemetry, "fuel_level", None)
    if value is None:
        value = getattr(telemetry, "fuel", None)
    return finite_number(value)


@dataclass(frozen=True)
class FuelEstimate:
    consumption_percent_per_lap: float | None = None
    remaining_laps: float | None = None
    fuel_needed_percent: float | None = None
    measured_laps: int = 0
    status: str = "EN ATTENTE DE TOURS COMPLETS"
    insufficient: bool = False


class FuelStrategy:
    """Bounded, per-car lap history; callers provide their own synchronization."""

    def __init__(self, window: int = 5):
        if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 100:
            raise ValueError("La fenêtre de consommation doit contenir de 1 à 100 tours")
        self._consumptions: deque[float] = deque(maxlen=window)
        self.reset()

    def reset(self) -> None:
        self._consumptions.clear()
        self._lap: int | None = None
        self._lap_start: float | None = None
        self._last_fuel: float | None = None
        self._capacity: float | None = None
        self._car_id: int | None = None
        self._total_laps: int | None = None
        self._complete_start = False
        self._refuelled = False
        self._measured_laps = 0

    def update(self, telemetry: Any) -> FuelEstimate:
        fuel = fuel_value(telemetry)
        capacity = finite_number(getattr(telemetry, "fuel_capacity", None))
        lap = integer(getattr(telemetry, "current_lap", None), maximum=32767)
        total = integer(getattr(telemetry, "total_laps", None), maximum=32767)
        car_id = integer(getattr(telemetry, "car_id", None))
        flags = integer(getattr(telemetry, "flags", None), maximum=65535)

        # Loading marks a discontinuity. A normal pause need not discard history.
        if flags is not None and flags & (1 << 2):
            self.reset()
            return FuelEstimate()

        if fuel is None or capacity is None or capacity <= 0 or not 0 <= fuel <= capacity or lap is None:
            return FuelEstimate()

        changed = (
            (self._capacity is not None and not math.isclose(capacity, self._capacity, rel_tol=1e-5))
            or (self._car_id is not None and car_id is not None and self._car_id != car_id)
            or (self._total_laps is not None and total is not None and self._total_laps != total)
            or (self._lap is not None and lap < self._lap)
        )
        if changed:
            self.reset()
        self._capacity = capacity
        if car_id is not None:
            self._car_id = car_id
        if total is not None:
            self._total_laps = total

        # A rise over 0.1% of the tank excludes the lap and pre-refuel history.
        # The same check precedes line-crossing handling to catch refuelling there.
        refuelled_now = self._last_fuel is not None and fuel - self._last_fuel > capacity * 0.001
        if refuelled_now:
            self._consumptions.clear()
            self._measured_laps = 0
            self._refuelled = True

        if self._lap is None:
            self._lap = lap
            self._lap_start = fuel
            self._complete_start = False  # First observed lap can be partial/out-lap.
        elif lap != self._lap:
            if lap == self._lap + 1:
                if self._complete_start and not self._refuelled and self._lap_start is not None:
                    used_percent = (self._lap_start - fuel) / capacity * 100.0
                    if 0.01 < used_percent <= 100.0:
                        self._consumptions.append(used_percent)
                        self._measured_laps += 1
                # Lap 0 and the first lap following it may be an out-lap.
                self._complete_start = self._lap >= 1
            else:
                # Missing lap crossings cannot yield a per-lap consumption.
                self._consumptions.clear()
                self._measured_laps = 0
                self._complete_start = False
            self._lap = lap
            self._lap_start = fuel
            self._refuelled = refuelled_now

        self._last_fuel = fuel
        if not self._consumptions:
            return FuelEstimate()

        consumption = sum(self._consumptions) / len(self._consumptions)
        fuel_percent = fuel / capacity * 100.0
        autonomy = fuel_percent / consumption
        needed = None
        if total is not None and total > 0 and lap > 0:
            # Conservative: the current lap is counted in full. No pit command
            # or exact finish prediction is inferred from this estimate.
            needed = consumption * max(0, total - lap + 1)
        insufficient = self._measured_laps >= 2 and needed is not None and needed > fuel_percent
        if self._measured_laps < 3:
            status = "ESTIMATION PROVISOIRE"
        else:
            status = "ESTIMATION SUR TOURS COMPLETS"
        if insufficient:
            status = "CARBURANT ESTIMÉ INSUFFISANT"
        return FuelEstimate(
            consumption_percent_per_lap=consumption,
            remaining_laps=autonomy,
            fuel_needed_percent=needed,
            measured_laps=self._measured_laps,
            status=status,
            insufficient=insufficient,
        )
