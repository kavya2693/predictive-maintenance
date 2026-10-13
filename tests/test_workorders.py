from __future__ import annotations

import numpy as np
import pandas as pd

from predictive_maintenance.features import FEATURE_NAMES
from predictive_maintenance.policy import Alert
from predictive_maintenance.workorders import build, priority, validate


def _alerts() -> list[Alert]:
    f = np.full(len(FEATURE_NAMES), np.nan)
    f[FEATURE_NAMES.index("vibration_rel7")] = 0.6
    f[FEATURE_NAMES.index("temperature_rel7")] = 0.04
    return [Alert(730, 0, 0.62, 5.0, f, "bearing"), Alert(731, 1, 0.3, 40.0, f, "seal")]


def _assets() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "asset_id": ["PUMP-000", "COMP-001"],
            "asset_type": ["pump", "compressor"],
            "functional_location": ["U100-P-000", "U200-K-001"],
        }
    )


def test_orders_validate_and_carry_evidence() -> None:
    orders = build(_alerts(), _assets(), np.array([0, 1]), np.array([12.0, 90.0]), "gbm-sensors")
    assert [validate(o) for o in orders] == [[], []]
    assert len({o["work_order_id"] for o in orders}) == 2
    assert orders[0]["priority"] == 1 and orders[1]["priority"] == 2
    assert orders[0]["evidence"][0]["sensor"] == "vibration"
    assert orders[0]["schedule"]["crew"] == "mechanical"


def test_validator_rejects_broken_orders() -> None:
    order = build(_alerts()[:1], _assets(), np.array([0, 1]), np.array([12.0]), "gbm-sensors")[0]
    order["risk"]["failure_probability"] = 1.7
    assert "failure_probability outside [0, 1]" in validate(order)
    del order["asset"]
    assert "missing asset" in validate(order)


def test_priority_bands() -> None:
    assert [priority(p) for p in (0.9, 0.3, 0.1)] == [1, 2, 3]
