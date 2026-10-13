"""Work orders as JSON: the hand-off from the model to the maintenance system of record.

The payload carries what a planner needs to accept or reject the job without opening a
notebook: the asset and its location, the risk and how it was computed, the sensor evidence,
a requested window, and the cost the job is expected to avoid.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd

from .config import HORIZON_DAYS, PLANNED_JOB_COST, SENSORS, START_DATE
from .features import FEATURE_NAMES
from .models import FAILURE_COST, PLANNED_COST
from .policy import Alert

SCHEMA_VERSION = "1.0"
REQUIRED = {
    "schema_version": str,
    "work_order_id": str,
    "created_on": str,
    "order_type": str,
    "priority": int,
    "asset": dict,
    "risk": dict,
    "evidence": list,
    "schedule": dict,
    "estimated_cost": float,
}
CREW_BY_TYPE = {
    "pump": "mechanical",
    "compressor": "rotating-equipment",
    "heat_exchanger": "static-equipment",
}


def priority(p: float) -> int:
    """1 = this shift, 2 = this week, 3 = next opening."""
    if p >= 0.5:
        return 1
    return 2 if p >= 0.25 else 3


def evidence(features: np.ndarray, top: int = 3) -> list[dict[str, Any]]:
    """The sensors that moved most against their own baseline over the last week."""
    moves = [(s, float(features[FEATURE_NAMES.index(f"{s}_rel7")])) for s in SENSORS]
    moves = sorted((m for m in moves if np.isfinite(m[1])), key=lambda m: -abs(m[1]))
    return [{"sensor": s, "change_vs_baseline_7d": round(v, 3)} for s, v in moves[:top]]


def build(
    alerts: list[Alert],
    assets: pd.DataFrame,
    type_idx: np.ndarray,
    rul_days: np.ndarray,
    model_name: str,
    first_number: int = 1,
) -> list[dict[str, Any]]:
    """rul_days is aligned with alerts: median remaining life from the fleet Weibull model."""
    orders = []
    for n, (a, rul) in enumerate(zip(alerts, rul_days, strict=True), start=first_number):
        row = assets.iloc[a.asset]
        day = START_DATE + timedelta(days=a.day)
        k = int(type_idx[a.asset])
        orders.append(
            {
                "schema_version": SCHEMA_VERSION,
                "work_order_id": f"CBM-{day.year}-{n:05d}",
                "created_on": day.isoformat(),
                "order_type": "condition_based",
                "priority": priority(a.probability),
                "asset": {
                    "asset_id": row["asset_id"],
                    "asset_type": row["asset_type"],
                    "functional_location": row["functional_location"],
                },
                "risk": {
                    "model": model_name,
                    "failure_probability": round(a.probability, 3),
                    "horizon_days": HORIZON_DAYS,
                    "fleet_median_remaining_life_days": round(float(rul), 1),
                    "expected_saving": round(float(a.probability * FAILURE_COST[k] - PLANNED_COST), 2),
                },
                "evidence": evidence(a.features),
                "schedule": {
                    "requested_start": day.isoformat(),
                    "due_by": (day + timedelta(days=3)).isoformat(),
                    "estimated_duration_hours": 8,
                    "crew": CREW_BY_TYPE[row["asset_type"]],
                },
                "estimated_cost": float(PLANNED_JOB_COST),
            }
        )
    return orders


def validate(order: dict[str, Any]) -> list[str]:
    """Problems that would make the receiving system reject the order; empty when it is fine."""
    errs = [f"missing {k}" for k in REQUIRED if k not in order]
    errs += [
        f"{k} should be {t.__name__}"
        for k, t in REQUIRED.items()
        if k in order and not isinstance(order[k], t)
    ]
    if errs:
        return errs
    p = order["risk"].get("failure_probability")
    if not isinstance(p, float) or not 0.0 <= p <= 1.0:
        errs.append("failure_probability outside [0, 1]")
    if order["priority"] not in (1, 2, 3):
        errs.append("priority must be 1, 2 or 3")
    if order["schedule"]["due_by"] < order["schedule"]["requested_start"]:
        errs.append("due_by before requested_start")
    if not order["asset"].get("functional_location"):
        errs.append("no functional location")
    return errs
