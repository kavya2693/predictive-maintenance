from __future__ import annotations

import numpy as np

from predictive_maintenance.data import run_history
from predictive_maintenance.plant import Plant
from tests.conftest import SMALL


def test_same_seed_same_plant() -> None:
    a = run_history(seed=11, days=120, counts=SMALL)
    b = run_history(seed=11, days=120, counts=SMALL)
    sa = np.stack([h.sensors for h in a.history])
    sb = np.stack([h.sensors for h in b.history])
    assert np.array_equal(sa, sb, equal_nan=True)
    assert [(e.day, e.asset, e.kind) for e in a.events] == [(e.day, e.asset, e.kind) for e in b.events]


def test_bearing_faults_show_in_vibration_before_failure(small_history) -> None:
    """The generator's core promise: a gradual fault is visible in the right sensor ahead of time."""
    r = small_history.readings.assign(day=lambda d: (d["date"] - d["date"].min()).map(lambda x: x.days))
    lives = small_history.lives
    ratios = []
    for _, lf in lives[(lives["ended_by"] == "failure") & (lives["mode"] == "bearing")].iterrows():
        end = int(lf["end_day"])
        if end - lf["start_day"] < 60:
            continue
        v = r[r["asset_id"] == lf["asset_id"]].set_index("day")["vibration"]
        ratios.append(v.loc[end - 4 : end - 1].median() / v.loc[end - 50 : end - 30].median())
    assert len(ratios) >= 3
    assert np.nanmedian(ratios) > 1.3


def test_sudden_failures_have_no_precursor(small_history) -> None:
    r = small_history.readings.assign(day=lambda d: (d["date"] - d["date"].min()).map(lambda x: x.days))
    lives = small_history.lives
    ratios = []
    for _, lf in lives[(lives["ended_by"] == "failure") & (lives["mode"] == "sudden")].iterrows():
        end = int(lf["end_day"])
        if end - lf["start_day"] < 60:
            continue
        v = r[r["asset_id"] == lf["asset_id"]].set_index("day")["temperature"]
        ratios.append(v.loc[end - 4 : end - 1].median() / v.loc[end - 50 : end - 30].median())
    assert ratios
    assert abs(np.nanmedian(ratios) - 1.0) < 0.05


def test_fork_gives_every_policy_the_same_future() -> None:
    """Servicing one asset must not change the noise or lives any other asset sees."""
    base = Plant(seed=5, counts=SMALL)
    for _ in range(80):
        base.step()
    a, b = base.fork(1), base.fork(1)
    target = int(np.argmax(a.latent_remaining() > 30))
    ra, rb = [], []
    for t in range(40):
        ra.append(a.step().sensors)
        rb.append(b.step([target] if t == 0 else []).sensors)
    ra_, rb_ = np.stack(ra), np.stack(rb)
    others = np.arange(base.n) != target
    assert np.array_equal(ra_[:, others], rb_[:, others], equal_nan=True)
    assert not np.array_equal(ra_[:, target], rb_[:, target], equal_nan=True)


def test_turnaround_stops_ageing() -> None:
    p = Plant(seed=2, counts=SMALL)
    while not p.calendar()[2]:
        p.step()
    age = p.age.copy()
    res = p.step()
    assert (res.status == 3).sum() + (res.status == 1).sum() == p.n
    assert np.array_equal(p.age, age)
