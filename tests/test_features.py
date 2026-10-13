from __future__ import annotations

import numpy as np
import pandas as pd

from predictive_maintenance.data import CalendarPMPolicy, tables
from predictive_maintenance.features import FEATURE_NAMES, _labels, _nanslope, build_table, window_features
from predictive_maintenance.plant import Plant
from tests.conftest import SMALL


def test_offline_and_live_features_agree() -> None:
    """Training rows and the scheduler's live features come from the same code and must match."""
    plant = Plant(seed=9, counts=SMALL, record=True)
    policy = CalendarPMPolicy(plant, 1)
    snap_day = 140
    live = None
    for _ in range(170):
        plant.step(policy.actions(plant))
        if plant.day - 1 == snap_day:
            dsm = (plant.day - 1) - plant.last_maint_day
            live = window_features(plant.win_sensors, plant.win_load, dsm, plant.type_idx)
    h = tables(plant)
    tab = build_table(h.assets, h.readings, h.maintenance)
    offline = tab[tab["day"] == snap_day].sort_values("asset_idx")[FEATURE_NAMES].to_numpy()
    assert live is not None
    dsm_col = FEATURE_NAMES.index("days_since_maint")
    assert np.array_equal(offline[:, dsm_col], live[:, dsm_col])
    # The CSV path rounds readings to 3 decimals and load to 0.1 %, nothing more.
    assert np.allclose(offline, live, rtol=0.02, atol=0.02, equal_nan=True)


def test_labels_drop_rows_a_preventive_job_hides() -> None:
    m = pd.DataFrame({"day": [10, 30], "kind": ["preventive", "corrective"]})
    days = np.arange(41)
    label, tte, failed = _labels(m, days, 41)
    assert np.isnan(label[5])  # preventive job at day 10 hides what would have happened
    assert label[20] == 1.0  # failure 10 days ahead
    assert label[12] == 0.0  # next event 18 days ahead, beyond the horizon
    assert np.isnan(label[30])  # data ends within the horizon
    assert tte[20] == 10 and failed[20]
    assert not failed[5]


def test_nanslope_ignores_gaps() -> None:
    y = np.arange(14, dtype=float) * 0.5 + 3
    y[[2, 7]] = np.nan
    assert np.isclose(_nanslope(y[None, :])[0], 0.5)
    assert np.isnan(_nanslope(np.full((1, 14), np.nan))[0])
