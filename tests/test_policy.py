from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from predictive_maintenance.features import FEATURE_NAMES
from predictive_maintenance.models import FAILURE_COST, PLANNED_COST
from predictive_maintenance.plant import Plant
from predictive_maintenance.policy import CostAwareModel, Observation, Oracle, RunToFailure, ThresholdRule
from predictive_maintenance.simulate import run_policy
from tests.conftest import SMALL


@dataclass
class Everyone:
    name: str = "everyone"

    def score(self, obs: Observation) -> tuple[np.ndarray, np.ndarray]:
        return np.ones(len(obs.type_idx)), np.ones(len(obs.type_idx))


@dataclass
class Coin:
    """A deliberately useless policy: the precision metric has to see through it."""

    name: str = "coin"
    rng: np.random.Generator = np.random.default_rng(0)  # noqa: RUF009

    def score(self, obs: Observation) -> tuple[np.ndarray, np.ndarray]:
        return self.rng.random(len(obs.type_idx)), np.full(len(obs.type_idx), 0.5)


def _warm_plant() -> Plant:
    p = Plant(seed=4, counts=SMALL)
    for _ in range(90):
        p.step()
    return p


def test_crew_budget_is_never_exceeded() -> None:
    run = run_policy(_warm_plant(), Everyone(), days=56, crew_jobs_per_week=3, seed=0)
    days = np.array([a.day for a in run.alerts])
    weeks = (days - days.min() + (90 % 7)) // 7
    assert np.bincount(weeks).max() <= 3
    assert np.bincount(days).max() <= 1  # ceil(3 / 5) per working day


def test_run_to_failure_plans_nothing_and_oracle_beats_it() -> None:
    base = _warm_plant()
    rtf = run_policy(base.fork(1), RunToFailure(), 200, 6, 1).metrics()
    orc = run_policy(base.fork(1), Oracle(), 200, 6, 1).metrics()
    assert rtf["planned_jobs"] == 0
    assert orc["precision_at_budget"] == 1.0
    assert orc["failures"] < rtf["failures"]
    assert orc["uptime"] > rtf["uptime"]


def test_precision_metric_sees_through_a_coin_flip() -> None:
    m = run_policy(_warm_plant().fork(2), Coin(), 200, 6, 2).metrics()
    assert m["alerts"] > 50
    assert m["precision_at_budget"] < 0.25


class _Fixed:
    def __init__(self, p: float) -> None:
        self.p = p

    def predict(self, X: np.ndarray) -> np.ndarray:
        return np.full(len(X), self.p)


def _obs(n: int = 1) -> Observation:
    return Observation(
        day=0,
        X=np.zeros((n, len(FEATURE_NAMES))),
        days_since_maint=np.full(n, 100),
        type_idx=np.zeros(n, dtype=int),
        eligible=np.ones(n, dtype=bool),
        latent_remaining=np.full(n, np.nan),
    )


def test_cost_aware_policy_proposes_only_above_break_even() -> None:
    breakeven = PLANNED_COST / FAILURE_COST[0]
    below, _ = CostAwareModel(_Fixed(breakeven * 0.99), "m").score(_obs())
    above, _ = CostAwareModel(_Fixed(breakeven * 1.01), "m").score(_obs())
    assert np.isnan(below[0])
    assert above[0] > 0


def test_alarm_rule_trips_on_vibration_and_either_way_on_pressure() -> None:
    obs = _obs(3)
    obs.X[0, FEATURE_NAMES.index("vibration_rel7")] = 0.31
    obs.X[1, FEATURE_NAMES.index("pressure_rel7")] = -0.12
    obs.X[2, FEATURE_NAMES.index("vibration_rel7")] = 0.2
    score, _ = ThresholdRule().score(obs)
    assert np.isfinite(score[:2]).all() and np.isnan(score[2])
