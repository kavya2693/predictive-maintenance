"""Day-by-day plant model: asset lives, failure precursors in the sensors, and maintenance.

The same object produces the history the models learn from and the closed loop the alert
policies are judged in. Each asset carries a latent life drawn from a Weibull distribution
and a failure mode; the sensors start to drift some days before the end of that life, by an
amount and lead that depend on the mode. Because the latent life is known here, an alert can
be scored against the failure it would have prevented even when the service removed it.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np

from .config import (
    ASSET_TYPES,
    FAILURE_MODES,
    HOLIDAY_DOY,
    PLANNED_JOB_DOWNTIME_DAYS,
    PRECURSOR_DAYS,
    SENSORS,
    START_DATE,
    TURNAROUND_DOY,
    WINDOW_DAYS,
    AssetType,
)

RUNNING, FAILED, PLANNED, SHUTDOWN = 0, 1, 2, 3
SUDDEN = FAILURE_MODES.index("sudden")
_TEMP = SENSORS.index("temperature")

# Effect of a fully developed fault on each sensor (vibration, temperature, pressure, flow,
# current). Temperature is an additive shift in deg C; the others are fractions of baseline.
_EFFECTS = {
    "bearing": (1.20, 8.0, 0.00, 0.00, 0.06),
    "seal": (0.15, 0.0, -0.18, -0.15, 0.00),
    "overheating": (0.00, 25.0, 0.00, 0.00, 0.12),
    "valve": (0.35, 6.0, -0.20, -0.08, 0.00),
    # On an exchanger "pressure" is the differential pressure, which rises as it fouls.
    "fouling": (0.00, 14.0, 0.25, -0.12, 0.00),
    "tube_leak": (0.00, -4.0, -0.22, -0.10, 0.00),
    "sudden": (0.00, 0.0, 0.00, 0.00, 0.00),
}
EFFECTS = np.array([_EFFECTS[m] for m in FAILURE_MODES])

# Baseline ranges per type: vibration mm/s, temperature C, pressure bar, flow m3/h, current A.
_BASELINES = {
    "pump": ((2.0, 4.5), (45.0, 65.0), (6.0, 14.0), (80.0, 400.0), (40.0, 160.0)),
    "compressor": ((3.0, 6.0), (70.0, 95.0), (20.0, 40.0), (2000.0, 6000.0), (150.0, 400.0)),
    "heat_exchanger": ((1.0, 1.0), (60.0, 90.0), (0.8, 2.0), (100.0, 500.0), (1.0, 1.0)),
}
_NOISE = np.array([0.06, 1.2, 0.025, 0.03, 0.025])  # temperature noise in deg C, rest relative


@dataclass
class Event:
    day: int
    asset: int
    kind: str  # "preventive" or "corrective"
    downtime_days: int
    failure_mode: str | None
    latent_remaining: float  # operating days the asset had left; 0 for a failure


@dataclass
class Life:
    asset: int
    start_day: int
    mode: str
    life_days: float
    end_day: int | None = None
    ended_by: str | None = None
    latent_failure_day: float | None = None


@dataclass
class DayResult:
    day: int
    sensors: np.ndarray  # (n, 5)
    load: np.ndarray  # (n,)
    ambient: float
    status: np.ndarray  # (n,)
    events: list[Event] = field(default_factory=list)


class Plant:
    """A fleet of pumps, compressors and heat exchangers stepped one day at a time."""

    def __init__(
        self,
        seed: int,
        counts: dict[str, int] | None = None,
        record: bool = False,
    ) -> None:
        self.types: tuple[AssetType, ...] = ASSET_TYPES
        counts = counts or {t.name: t.count for t in self.types}
        ss = np.random.SeedSequence(seed)
        setup_ss, noise_ss, life_ss = ss.spawn(3)
        setup = np.random.default_rng(setup_ss)

        self.type_idx = np.concatenate([np.full(counts[t.name], k) for k, t in enumerate(self.types)])
        self.n = len(self.type_idx)
        self.asset_ids = [f"{self.types[k].name[:4].upper()}-{i:03d}" for i, k in enumerate(self.type_idx)]
        self.load_mean = setup.uniform(0.55, 0.95, self.n)
        self.base = np.empty((self.n, len(SENSORS)))
        self.has = np.empty((self.n, len(SENSORS)), dtype=bool)
        for i, k in enumerate(self.type_idx):
            t = self.types[k]
            self.base[i] = [setup.uniform(lo, hi) for lo, hi in _BASELINES[t.name]]
            self.has[i] = t.has_sensor
        # Assets driven harder wear out sooner.
        self.scale = np.array([self.types[k].weibull_scale_days for k in self.type_idx])
        self.scale *= (self.load_mean / 0.75) ** -1.5
        self.shape = np.array([self.types[k].weibull_shape for k in self.type_idx])

        self.rng = np.random.default_rng(noise_ss)
        self.life_rngs = [np.random.default_rng(s) for s in life_ss.spawn(self.n)]

        self.day = 0
        self.age = np.zeros(self.n)
        self.life = np.zeros(self.n)
        self.mode = np.zeros(self.n, dtype=int)
        self.tau = np.ones(self.n)
        self.amp = np.ones(self.n)
        self.down_left = np.zeros(self.n, dtype=int)
        self.offline_left = np.zeros((self.n, len(SENSORS)), dtype=int)
        self.last_maint_day = np.zeros(self.n, dtype=int)
        self.current_life: list[Life] = [None] * self.n  # type: ignore[list-item]
        self.lives: list[Life] = []
        self.events: list[Event] = []
        self.win_sensors = np.full((self.n, WINDOW_DAYS, len(SENSORS)), np.nan)
        self.win_load = np.full((self.n, WINDOW_DAYS), np.nan)

        for i in range(self.n):
            self._new_life(i, start_day=0)
            # Start each asset part-way through its life so day 0 is not a fleet of new machines.
            self.age[i] = setup.uniform(0.0, 0.85) * self.life[i]
            self.last_maint_day[i] = -int(self.age[i])
            self.current_life[i].start_day = -int(self.age[i])

        self.record = record
        self.history: list[DayResult] = []

    # ------------------------------------------------------------------ lives
    def _new_life(self, i: int, start_day: int) -> None:
        r = self.life_rngs[i]
        t = self.types[self.type_idx[i]]
        self.life[i] = max(5.0, self.scale[i] * r.weibull(self.shape[i]))
        names = list(t.modes)
        mode = names[int(r.choice(len(names), p=np.array(list(t.modes.values()))))]
        self.mode[i] = FAILURE_MODES.index(mode)
        mu, sd = PRECURSOR_DAYS[mode]
        self.tau[i] = max(1.5, r.normal(mu, sd)) if mu > 0 else 1.0
        self.amp[i] = r.uniform(0.7, 1.3)
        self.age[i] = 0.0
        life = Life(asset=i, start_day=start_day, mode=mode, life_days=float(self.life[i]))
        self.current_life[i] = life
        self.lives.append(life)

    def _close_life(self, i: int, day: int, ended_by: str) -> float:
        life = self.current_life[i]
        remaining = float(self.life[i] - self.age[i])
        life.end_day = day
        life.ended_by = ended_by
        life.latent_failure_day = day + max(remaining, 0.0)
        return remaining

    def latent_remaining(self) -> np.ndarray:
        """Operating days left before each asset fails. Only the evaluation may look at this."""
        return self.life - self.age

    def calendar(self, day: int | None = None) -> tuple[int, int, bool, bool]:
        day = self.day if day is None else day
        doy = day % 365 + 1
        dow = (START_DATE.weekday() + day) % 7
        return doy, dow, doy in TURNAROUND_DOY, doy in HOLIDAY_DOY

    def days_since_maint(self) -> np.ndarray:
        return self.day - self.last_maint_day

    # ------------------------------------------------------------------ step
    def step(self, actions: Iterable[int] = ()) -> DayResult:
        d = self.day
        n, s = self.n, len(SENSORS)
        doy, dow, shutdown, holiday = self.calendar(d)

        # Draw a fixed block of numbers every day whatever happens, so two policies run from
        # the same seed see identical noise, dropouts and weather.
        z = self.rng.standard_normal((n, s))
        u_miss = self.rng.random((n, s))
        u_out = self.rng.random((n, s))
        out_mag = self.rng.uniform(1.8, 3.5, (n, s))
        u_off = self.rng.random((n, s))
        off_len = self.rng.integers(3, 12, (n, s))
        zl = self.rng.standard_normal(n)
        ambient = 31.0 + 9.0 * np.sin(2 * np.pi * (doy - 110) / 365) + 1.5 * self.rng.standard_normal()

        status = np.full(n, RUNNING)
        events: list[Event] = []

        # Assets already in repair stay down; the last repair day renews them.
        in_repair = self.down_left > 0
        status[in_repair] = FAILED
        self.down_left[in_repair] -= 1
        for i in np.flatnonzero(in_repair & (self.down_left == 0)).tolist():
            self.last_maint_day[i] = d
            self._new_life(i, start_day=d + 1)

        if shutdown:
            status[status == RUNNING] = SHUTDOWN
        else:
            for i in map(int, actions):
                if status[i] != RUNNING:
                    continue
                remaining = self._close_life(i, d, "preventive")
                events.append(Event(d, i, "preventive", PLANNED_JOB_DOWNTIME_DAYS, None, remaining))
                status[i] = PLANNED
                self.last_maint_day[i] = d
                self._new_life(i, start_day=d + 1)

            running = status == RUNNING
            self.age[running] += 1.0
            failing = running & (self.age >= self.life)
            for i in np.flatnonzero(failing).tolist():
                self._close_life(i, d, "failure")
                t = self.types[self.type_idx[i]]
                events.append(
                    Event(d, i, "corrective", t.fail_downtime_days, FAILURE_MODES[self.mode[i]], 0.0)
                )
                status[i] = FAILED
                self.down_left[i] = t.fail_downtime_days - 1
                if self.down_left[i] == 0:
                    self.last_maint_day[i] = d
                    self._new_life(i, start_day=d + 1)

        running = status == RUNNING
        weekend = 0.85 if dow >= 5 else 1.0
        load = self.load_mean * weekend * (0.6 if holiday else 1.0) * (1.0 + 0.05 * zl)
        load = np.where(running, np.clip(load, 0.2, 1.05), 0.0)

        left = np.maximum(self.life - self.age, 0.0)
        deg = np.where(self.mode == SUDDEN, 0.0, self.amp * np.exp(-left / self.tau))
        eff = EFFECTS[self.mode] * deg[:, None]
        L = load
        mult = np.stack(
            [0.7 + 0.4 * L, np.ones(n), 0.75 + 0.35 * L, 0.2 + 0.9 * L, 0.45 + 0.65 * L],
            axis=1,
        )
        readings = self.base * mult * (1.0 + eff) * (1.0 + _NOISE * z)
        readings[:, _TEMP] = (
            self.base[:, _TEMP]
            + 12.0 * L
            + 0.3 * (ambient - 30.0)
            + eff[:, _TEMP]
            + _NOISE[_TEMP] * z[:, _TEMP]
        )
        spikes = u_out < 0.002
        readings = np.where(spikes, readings * out_mag, readings)

        # Sensor dropouts: scattered single days and occasional multi-day outages.
        self.offline_left = np.maximum(self.offline_left - 1, 0)
        self.offline_left = np.where((self.offline_left == 0) & (u_off < 0.0015), off_len, self.offline_left)
        missing = (u_miss < 0.015) | (self.offline_left > 0) | ~self.has | ~running[:, None]
        readings[missing] = np.nan

        self.win_sensors = np.roll(self.win_sensors, -1, axis=1)
        self.win_sensors[:, -1, :] = readings
        self.win_load = np.roll(self.win_load, -1, axis=1)
        self.win_load[:, -1] = np.where(running, load, np.nan)

        self.events.extend(events)
        result = DayResult(d, readings, load, float(ambient), status, events)
        if self.record:
            self.history.append(result)
        self.day += 1
        return result

    def fork(self, seed: int) -> Plant:
        """Copy the plant and give it fresh randomness for the days ahead.

        Every policy evaluated from one fork sees the same future noise and the same next
        life draws, so differences between policies are not luck of the draw.
        """
        other = copy.deepcopy(self)
        other.record = False
        other.history = []
        ss = np.random.SeedSequence([seed, self.day])
        noise_ss, life_ss = ss.spawn(2)
        other.rng = np.random.default_rng(noise_ss)
        other.life_rngs = [np.random.default_rng(x) for x in life_ss.spawn(self.n)]
        return other
