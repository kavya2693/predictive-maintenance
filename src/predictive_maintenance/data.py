"""Generate the plant history and write the tables a historian and a CMMS would hold.

Four tables, joined on asset_id:

- assets.csv               asset register: type, functional location, last service before history
- sensor_readings.csv.gz   one row per asset per day: load, ambient, five sensors, run status
- maintenance_log.csv      every preventive and corrective job, with downtime and cost
- lives_truth.csv          latent life and failure mode per asset life; evaluation only, never a feature
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (
    DOWNTIME_COST_PER_DAY,
    HISTORY_YEARS,
    PLANNED_JOB_COST,
    SENSORS,
    START_DATE,
    TRAIN_DAYS,
)
from .plant import Plant

STATUS_NAMES = {0: "running", 1: "failed", 2: "planned_job", 3: "turnaround"}


class CalendarPMPolicy:
    """What the plant did before: service each asset on a calendar interval, often late.

    Planners postpone about a third of due jobs by a few weeks when production is tight,
    which is why the history holds both preventive jobs and failures.
    """

    def __init__(self, plant: Plant, seed: int) -> None:
        self.rng = np.random.default_rng(seed)
        base = np.array([plant.types[k].history_pm_interval for k in plant.type_idx], dtype=float)
        self.interval = base * self.rng.uniform(0.85, 1.15, plant.n)
        self.postponed_to = np.full(plant.n, -1)

    def actions(self, plant: Plant) -> list[int]:
        _, dow, shutdown, _ = plant.calendar()
        if shutdown or dow >= 5:
            return []
        due = np.flatnonzero(plant.days_since_maint() >= self.interval)
        out = []
        for i in due:
            if self.postponed_to[i] > plant.day:
                continue
            if self.postponed_to[i] < plant.last_maint_day[i] and self.rng.random() < 0.35:
                self.postponed_to[i] = plant.day + int(self.rng.integers(14, 60))
                continue
            out.append(int(i))
        return out


@dataclass
class History:
    plant: Plant
    assets: pd.DataFrame
    readings: pd.DataFrame
    maintenance: pd.DataFrame
    lives: pd.DataFrame


def run_history(seed: int, days: int, counts: dict[str, int] | None = None, record: bool = True) -> Plant:
    plant = Plant(seed, counts=counts, record=record)
    policy = CalendarPMPolicy(plant, seed + 7)
    for _ in range(days):
        plant.step(policy.actions(plant))
    return plant


def day_to_date(day: int | np.ndarray) -> pd.Series | pd.Timestamp:
    return pd.Timestamp(START_DATE) + pd.to_timedelta(day, unit="D")


def tables(plant: Plant) -> History:
    n = plant.n
    type_names = [plant.types[k].name for k in plant.type_idx]
    unit = {"pump": "P", "compressor": "K", "heat_exchanger": "E"}
    assets = pd.DataFrame(
        {
            "asset_id": plant.asset_ids,
            "asset_type": type_names,
            "functional_location": [
                f"U{100 + 100 * (i % 4)}-{unit[t]}-{i:03d}" for i, t in enumerate(type_names)
            ],
            "last_service_date": [
                (START_DATE + timedelta(days=int(plant.lives[i].start_day))).isoformat() for i in range(n)
            ],
        }
    )

    days = np.array([h.day for h in plant.history])
    sensors = np.stack([h.sensors for h in plant.history])  # (days, n, 5)
    load = np.stack([h.load for h in plant.history])
    status = np.stack([h.status for h in plant.history])
    ambient = np.array([h.ambient for h in plant.history])
    readings = pd.DataFrame(
        {
            "date": np.repeat(day_to_date(days).date, n),
            "asset_id": np.tile(plant.asset_ids, len(days)),
            "load_pct": np.round(100 * load.ravel(), 1),
            "ambient_c": np.round(np.repeat(ambient, n), 1),
            **{s: np.round(sensors[:, :, j].ravel(), 3) for j, s in enumerate(SENSORS)},
            "status": [STATUS_NAMES[x] for x in status.ravel()],
        }
    )
    readings.loc[readings["status"] != "running", "load_pct"] = 0.0

    rows = []
    for e in plant.events:
        t = plant.types[plant.type_idx[e.asset]]
        repair = PLANNED_JOB_COST if e.kind == "preventive" else t.fail_repair_cost
        rows.append(
            {
                "date": (START_DATE + timedelta(days=e.day)).isoformat(),
                "asset_id": plant.asset_ids[e.asset],
                "kind": e.kind,
                "failure_mode": e.failure_mode or "",
                "downtime_days": e.downtime_days,
                "cost": repair + e.downtime_days * DOWNTIME_COST_PER_DAY,
            }
        )
    maintenance = pd.DataFrame(rows)

    lives = pd.DataFrame(
        {
            "asset_id": [plant.asset_ids[x.asset] for x in plant.lives],
            "start_day": [x.start_day for x in plant.lives],
            "end_day": [x.end_day if x.end_day is not None else -1 for x in plant.lives],
            "ended_by": [x.ended_by or "open" for x in plant.lives],
            "mode": [x.mode for x in plant.lives],
            "life_days": [round(x.life_days, 2) for x in plant.lives],
            "latent_failure_day": [
                x.latent_failure_day
                if x.latent_failure_day is not None
                else plant.day - 1 + (plant.life[x.asset] - plant.age[x.asset])
                for x in plant.lives
            ],
        }
    )
    return History(plant, assets, readings, maintenance, lives)


def generate(seed: int = 7, out_dir: Path = Path("data")) -> History:
    plant = run_history(seed, HISTORY_YEARS * 365)
    hist = tables(plant)
    out_dir.mkdir(parents=True, exist_ok=True)
    hist.assets.to_csv(out_dir / "assets.csv", index=False)
    hist.readings.to_csv(out_dir / "sensor_readings.csv.gz", index=False)
    hist.maintenance.to_csv(out_dir / "maintenance_log.csv", index=False)
    hist.lives.to_csv(out_dir / "lives_truth.csv", index=False)
    return hist


def load(data_dir: Path = Path("data")) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    assets = pd.read_csv(data_dir / "assets.csv")
    readings = pd.read_csv(data_dir / "sensor_readings.csv.gz", parse_dates=["date"])
    maintenance = pd.read_csv(data_dir / "maintenance_log.csv", parse_dates=["date"])
    lives = pd.read_csv(data_dir / "lives_truth.csv")
    return assets, readings, maintenance, lives


def train_cutoff_day() -> int:
    return TRAIN_DAYS
