"""Re-run the repo's own pipeline and export what the demo video shows from the closed loop.

Same data (seed 7), models and settings as `make run`; evaluation seed 101 (the first of the
five forks), crew budget 6 jobs a week. Writes one JSON with:

- work_orders: the gbm-sensors work orders of the first two weeks (identical to
  outputs/work_orders.json, checked here), plus what the latent plant says would have happened
  to each asset (days to its latent failure and the failure mode) -- evaluation-only truth;
- daily: cumulative failures per policy over the closed-loop year, recorded with the repo's own
  schedule() and Plant.step() (totals checked against outputs/closed_loop_runs.csv);
- trace: the daily vibration of the first work order's asset over the 60 days before its alert,
  read from data/sensor_readings.csv.gz.

Run:  cd <repo> && .venv/bin/python <this file> <out.json>     (about one minute)
"""

import copy
import json
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from predictive_maintenance.cli import fit_models, make_policies
from predictive_maintenance.config import DAYS_PER_YEAR, START_DATE, TRAIN_DAYS, PolicyConfig
from predictive_maintenance.data import load, run_history
from predictive_maintenance.features import FEATURE_NAMES, build_table, day_index
from predictive_maintenance.plant import FAILED, PLANNED, RUNNING
from predictive_maintenance.policy import Crew, schedule
from predictive_maintenance.simulate import PolicyRun, run_policy
from predictive_maintenance.workorders import build, validate

SEED = 101
POLICIES = ("run-to-failure", "fixed-interval", "alarm-limits", "cox-sensors", "gbm-sensors", "oracle")

cfg = PolicyConfig()
k0 = cfg.crew_jobs_per_week
assets, readings, maint, _lives = load(Path("data"))
table = build_table(assets, readings, maint)
m = fit_models(table, assets, maint, cfg)
policies = make_policies(m)
base = run_history(7, TRAIN_DAYS, record=False)
fork = base.fork(SEED)
runs_csv = pd.read_csv("outputs/closed_loop_runs.csv")


def run_daily(name: str) -> tuple[PolicyRun, list[int]]:
    """simulate.run_policy, with the cumulative failure count recorded each day."""
    plant = copy.deepcopy(fork)
    run = PolicyRun(name, k0, SEED)
    crew = Crew(k0)
    cum, total = [], 0
    for _ in range(DAYS_PER_YEAR):
        alerts = schedule(policies[name], plant, crew)
        res = plant.step([a.asset for a in alerts])
        run.alerts.extend(alerts)
        run.running_days += int((res.status == RUNNING).sum())
        run.down_days += int(((res.status == FAILED) | (res.status == PLANNED)).sum())
        for e in res.events:
            if e.kind == "preventive":
                run.planned_jobs += 1
            else:
                run.failures += 1
                total += 1
                run.repair_cost += plant.types[plant.type_idx[e.asset]].fail_repair_cost
        cum.append(total)
    return run, cum


daily = {}
for name in POLICIES:
    run, cum = run_daily(name)
    ref = runs_csv[(runs_csv.policy == name) & (runs_csv.crew == k0) & (runs_csv.seed == SEED)].iloc[0]
    assert run.failures == int(ref.failures), (name, run.failures, ref.failures)
    assert run.planned_jobs == int(ref.planned_jobs), (name, run.planned_jobs, ref.planned_jobs)
    mm = run.metrics()
    daily[name] = {
        "cum_failures": cum,
        "failures": run.failures,
        "planned_jobs": run.planned_jobs,
        "uptime": mm["uptime"],
        "cost_total": mm["cost_total"],
    }

# work orders exactly as cli.main builds them
first = run_policy(copy.deepcopy(fork), policies["gbm-sensors"], DAYS_PER_YEAR, k0, SEED)
window = [a for a in first.alerts if a.day < TRAIN_DAYS + 14]
rul = m["rul"].median_remaining(
    base.type_idx[[a.asset for a in window]],
    np.array([m["duty"].get(assets.iloc[a.asset]["asset_id"], m["duty"].mean()) for a in window]),
    np.array([a.features[FEATURE_NAMES.index("days_since_maint")] for a in window]),
)
orders = build(window, assets, base.type_idx, rul, "gbm-sensors")
assert not [e for o in orders for e in validate(o)]
published = json.loads(Path("outputs/work_orders.json").read_text())
assert orders == published, "re-run work orders differ from outputs/work_orders.json"
for o, a in zip(orders, window, strict=True):
    o["truth"] = {"latent_days_to_failure": round(a.latent_remaining, 1), "latent_mode": a.mode}

# vibration trace of the first order's asset, 60 days up to the evening before the alert
aid = orders[0]["asset"]["asset_id"]
alert_day = window[0].day
r = readings[readings.asset_id == aid].copy()
r["day"] = day_index(r["date"])
r = r[(r.day >= alert_day - 60) & (r.day < alert_day)].sort_values("day")
trace = {
    "asset_id": aid,
    "sensor": "vibration",
    "alert_date": (START_DATE + timedelta(days=alert_day)).isoformat(),
    "days": [int(d - alert_day) for d in r.day],
    "values": [None if pd.isna(v) else round(float(v), 3) for v in r.vibration],
    "probability": orders[0]["risk"]["failure_probability"],
    "latent_days_to_failure": orders[0]["truth"]["latent_days_to_failure"],
}

fork_date = (START_DATE + timedelta(days=TRAIN_DAYS)).isoformat()
out = {"seed": SEED, "crew_jobs_per_week": k0, "fork_date": fork_date, "work_orders": orders, "daily": daily}
out["trace"] = trace
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
print({n: (d["failures"], d["planned_jobs"]) for n, d in daily.items()})
print([(o["asset"]["asset_id"], o["truth"]) for o in orders])
