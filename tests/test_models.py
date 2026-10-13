from __future__ import annotations

import numpy as np
from sklearn.metrics import average_precision_score

from predictive_maintenance.features import FEATURE_NAMES, build_table, latent_labels
from predictive_maintenance.models import AgeHazard, GBMModel, WeibullByType


def test_age_replacement_only_pays_when_hazard_rises() -> None:
    w = WeibullByType(shape=np.array([1.0, 3.0, 3.0]), scale=np.array([300.0, 300.0, 300.0]))
    t_flat, ratio_flat = w.optimal_interval(0)
    t_wear, ratio_wear = w.optimal_interval(1)
    assert ratio_flat > 0.99  # exponential life: replacing early never beats running to failure
    assert t_flat > 1500
    assert ratio_wear < 0.8 and t_wear < 300


def test_age_hazard_rises_with_age() -> None:
    w = WeibullByType(shape=np.array([2.0, 2.0, 2.0]), scale=np.array([300.0, 300.0, 300.0]))
    X = np.zeros((3, len(FEATURE_NAMES)))
    X[:, FEATURE_NAMES.index("type_pump")] = 1
    X[:, FEATURE_NAMES.index("days_since_maint")] = [10, 200, 400]
    p = AgeHazard(w).predict(X)
    assert np.all(np.diff(p) > 0) and p[0] < 0.01


def test_gbm_finds_the_precursor_signal(small_history) -> None:
    h = small_history
    tab = build_table(h.assets, h.readings, h.maintenance)
    tab = tab[tab["running"]]
    train = tab[(tab["day"] < 300) & tab["label"].notna()]
    valid = tab[(tab["day"] >= 300) & (tab["day"] < 360) & tab["label"].notna()]
    test = tab[tab["day"] >= 380].copy()
    params = {"n_estimators": 150, "learning_rate": 0.05, "num_leaves": 15, "min_child_samples": 20}
    model = GBMModel(params).fit(train, train["label"].to_numpy(), valid, valid["label"].to_numpy())
    y = latent_labels(test, h.lives)
    ap = average_precision_score(y, model.predict(test))
    assert ap > 5 * y.mean()
