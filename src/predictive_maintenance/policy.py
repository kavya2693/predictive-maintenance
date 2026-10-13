"""Alert policies and the crew-budgeted scheduler that turns scores into jobs.

Every policy answers the same question each working morning: of the assets the crew could
reach today, which are worth a planned job? The scheduler then takes the highest scores up
to what is left of the week's crew budget. Policies differ only in how they score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from .config import COOLDOWN_DAYS, FAILURE_MODES, HORIZON_DAYS, SENSORS
from .features import FEATURE_NAMES, window_features
from .models import FAILURE_COST, PLANNED_COST
from .plant import Plant


@dataclass
class Observation:
    day: int  # the day the crew would do the work
    X: np.ndarray  # features as of the end of the previous day, (n, F)
    days_since_maint: np.ndarray
    type_idx: np.ndarray
    eligible: np.ndarray
    latent_remaining: np.ndarray  # read only by the oracle


def observe(plant: Plant) -> Observation:
    dsm = (plant.day - 1) - plant.last_maint_day
    X = window_features(plant.win_sensors, plant.win_load, dsm, plant.type_idx)
    eligible = (plant.down_left == 0) & (dsm >= COOLDOWN_DAYS)
    return Observation(plant.day, X, dsm, plant.type_idx, eligible, plant.latent_remaining())


class Predictor(Protocol):
    def predict(self, X: np.ndarray) -> np.ndarray: ...


class Policy(Protocol):
    name: str

    def score(self, obs: Observation) -> tuple[np.ndarray, np.ndarray]:
        """Priority per asset (NaN = do not propose) and the failure probability behind it."""
        ...


class RunToFailure:
    name = "run-to-failure"

    def score(self, obs: Observation) -> tuple[np.ndarray, np.ndarray]:
        nan = np.full(len(obs.type_idx), np.nan)
        return nan, nan


@dataclass
class FixedInterval:
    """Calendar maintenance at the cost-optimal age per asset type, most overdue first."""

    interval_by_type: np.ndarray
    name: str = "fixed-interval"

    def score(self, obs: Observation) -> tuple[np.ndarray, np.ndarray]:
        over = obs.days_since_maint - self.interval_by_type[obs.type_idx]
        return np.where(over >= 0, over.astype(float), np.nan), np.full(len(over), np.nan)


# Alarm limits on the 7-day median against the asset's own baseline, the kind a reliability
# engineer sets by hand. Pressure trips both ways: it falls with leaks and rises with fouling.
RULE_LIMITS = {"vibration": 0.30, "temperature": 0.06, "pressure": 0.10, "flow": -0.08, "current": 0.06}


@dataclass
class ThresholdRule:
    name: str = "alarm-limits"

    @staticmethod
    def ratio(X: np.ndarray) -> np.ndarray:
        """Worst sensor deviation as a multiple of its limit; >= 1 means an alarm."""
        ratios = []
        for s in SENSORS:
            dev = X[:, FEATURE_NAMES.index(f"{s}_rel7")]
            lim = RULE_LIMITS[s]
            r = np.abs(dev) / abs(lim) if s == "pressure" else dev / lim
            ratios.append(np.nan_to_num(r, nan=0.0))
        return np.max(np.stack(ratios, axis=1), axis=1)

    def score(self, obs: Observation) -> tuple[np.ndarray, np.ndarray]:
        worst = self.ratio(obs.X)
        return np.where(worst >= 1.0, worst, np.nan), np.full(len(worst), np.nan)


@dataclass
class CostAwareModel:
    """Propose a job when expected failure cost avoided exceeds the cost of the job.

    p * C_fail > C_planned is the break-even rule; ranking by expected saving rather than by p
    sends the crew to a compressor before a pump at the same risk.
    """

    model: Predictor
    name: str

    def score(self, obs: Observation) -> tuple[np.ndarray, np.ndarray]:
        p = self.model.predict(obs.X)
        saving = p * FAILURE_COST[obs.type_idx] - PLANNED_COST
        return np.where(saving > 0, saving, np.nan), p


class Oracle:
    """Knows each asset's latent remaining life. An upper bound, not a policy anyone can run."""

    name = "oracle"

    def score(self, obs: Observation) -> tuple[np.ndarray, np.ndarray]:
        rem = obs.latent_remaining
        hit = (rem > 0) & (rem <= HORIZON_DAYS)
        return np.where(hit, -rem, np.nan), hit.astype(float)


@dataclass
class Alert:
    day: int
    asset: int
    probability: float
    latent_remaining: float
    features: np.ndarray
    mode: str  # latent failure mode, for scoring only


@dataclass
class Crew:
    jobs_per_week: int
    working_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    left: int = field(init=False)

    def __post_init__(self) -> None:
        self.left = self.jobs_per_week

    @property
    def per_day(self) -> int:
        return math.ceil(self.jobs_per_week / len(self.working_days))

    def capacity(self, plant: Plant) -> int:
        _, dow, shutdown, _ = plant.calendar()
        if dow == 0:
            self.left = self.jobs_per_week
        if shutdown or dow not in self.working_days:
            return 0
        return min(self.left, self.per_day)


def schedule(policy: Policy, plant: Plant, crew: Crew) -> list[Alert]:
    cap = crew.capacity(plant)
    if cap == 0 or isinstance(policy, RunToFailure):
        return []
    obs = observe(plant)
    score, prob = policy.score(obs)
    ok = obs.eligible & np.isfinite(score)
    idx = np.flatnonzero(ok)
    if len(idx) == 0:
        return []
    chosen = idx[np.argsort(-score[idx], kind="stable")[:cap]]
    crew.left -= len(chosen)
    return [
        Alert(
            obs.day,
            int(i),
            float(prob[i]),
            float(obs.latent_remaining[i]),
            obs.X[i].copy(),
            FAILURE_MODES[plant.mode[i]],
        )
        for i in chosen
    ]
