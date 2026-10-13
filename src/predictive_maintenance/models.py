"""The three model views of failure risk.

- Gradient boosting on rolling sensor features: probability of failure in the next 14 days.
- Cox proportional hazards on landmark snapshots of the same features: a survival model that
  sees the sensors, so it can be compared with the classifier on the same footing.
- Weibull on asset lives per type, with no sensors: the fleet-level view that sets the
  fixed-interval baseline and gives each work order a remaining-life figure.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from math import gamma

import lightgbm as lgb
import numpy as np
import pandas as pd
from lifelines import CoxPHFitter, WeibullAFTFitter, WeibullFitter

from .config import ASSET_TYPES, HORIZON_DAYS, PLANNED_JOB_COST, PLANNED_JOB_DOWNTIME_DAYS
from .config import DOWNTIME_COST_PER_DAY as DAY_COST
from .features import FEATURE_NAMES, SENSORS, TYPE_NAMES, day_index

PLANNED_COST = PLANNED_JOB_COST + PLANNED_JOB_DOWNTIME_DAYS * DAY_COST
FAILURE_COST = np.array([t.failure_cost for t in ASSET_TYPES])

COX_FEATURES = [
    *[f"{s}_rel7" for s in SENSORS],
    *[f"{s}_slope14" for s in SENSORS],
    "load_m7",
    "days_since_maint",
    *[f"type_{t}" for t in TYPE_NAMES[1:]],  # first type is the reference level
]


class GBMModel:
    def __init__(self, params: dict[str, float | int], seed: int = 0) -> None:
        self.clf = lgb.LGBMClassifier(**params, random_state=seed, n_jobs=2, verbose=-1)  # type: ignore[arg-type]

    def fit(self, X: pd.DataFrame, y: np.ndarray, Xv: pd.DataFrame, yv: np.ndarray) -> GBMModel:
        self.clf.fit(
            X[FEATURE_NAMES],
            y,
            eval_X=(Xv[FEATURE_NAMES],),
            eval_y=(yv,),
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        return self

    def predict(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        if isinstance(X, np.ndarray):
            X = pd.DataFrame(X, columns=FEATURE_NAMES)
        return np.asarray(self.clf.predict_proba(X[FEATURE_NAMES])[:, 1])


class CoxModel:
    """Cox PH on landmark rows: from each sampled asset-day, time to the next maintenance event."""

    def __init__(self, penalizer: float = 0.05) -> None:
        self.cph = CoxPHFitter(penalizer=penalizer)
        self.fill: pd.Series | None = None

    def _x(self, X: pd.DataFrame) -> pd.DataFrame:
        assert self.fill is not None
        return X[COX_FEATURES].fillna(self.fill).clip(-5, 5).assign(
            days_since_maint=X["days_since_maint"].clip(0, 1500) / 100.0
        )

    def fit(self, rows: pd.DataFrame) -> CoxModel:
        # A missing deviation is treated as no deviation; heat exchangers have no vibration probe.
        self.fill = pd.Series(0.0, index=COX_FEATURES)
        df = self._x(rows)
        df["T"] = rows["time_to_event"].clip(lower=0.5).to_numpy()
        df["E"] = rows["event_is_failure"].astype(int).to_numpy()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.cph.fit(df, duration_col="T", event_col="E")
        return self

    def predict(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        if isinstance(X, np.ndarray):
            X = pd.DataFrame(X, columns=FEATURE_NAMES)
        surv = self.cph.predict_survival_function(self._x(X), times=[HORIZON_DAYS])
        return np.asarray(1.0 - surv.to_numpy()[0])


@dataclass
class WeibullByType:
    shape: np.ndarray
    scale: np.ndarray

    @classmethod
    def fit(cls, lives: pd.DataFrame) -> WeibullByType:
        shapes, scales = [], []
        for name in TYPE_NAMES:
            g = lives[lives["asset_type"] == name]
            wf = WeibullFitter().fit(g["duration"].clip(lower=0.5), g["failed"])
            shapes.append(wf.rho_)
            scales.append(wf.lambda_)
        return cls(np.array(shapes), np.array(scales))

    def optimal_interval(self, type_k: int, grid: np.ndarray | None = None) -> tuple[float, float]:
        """Age-replacement interval minimising expected cost per operating day.

        cost rate(T) = (Cp R(T) + Cf (1 - R(T))) / integral_0^T R(t) dt
        Returns (T*, cost-rate ratio of T* against run-to-failure). When the shape is at or
        below 1 the hazard is flat or falling, and replacing early never pays.
        """
        grid = np.arange(10, 2000, 1.0) if grid is None else grid
        k, lam = self.shape[type_k], self.scale[type_k]
        t = np.arange(0, grid[-1] + 1, 1.0)
        R = np.exp(-((t / lam) ** k))
        cumR = np.concatenate([[0.0], np.cumsum((R[1:] + R[:-1]) / 2)])
        Ti = grid.astype(int)
        cf = FAILURE_COST[type_k]
        rate = (PLANNED_COST * R[Ti] + cf * (1 - R[Ti])) / cumR[Ti]
        mean_life = lam * gamma(1 + 1 / k)
        rtf_rate = cf / mean_life
        j = int(np.argmin(rate))
        return float(grid[j]), float(rate[j] / rtf_rate)


class RemainingLife:
    """Weibull AFT on lives with type and duty as covariates; conditional median remaining life."""

    def __init__(self) -> None:
        self.aft = WeibullAFTFitter(penalizer=0.01)

    @staticmethod
    def _x(type_idx: np.ndarray, duty: np.ndarray) -> pd.DataFrame:
        df = pd.DataFrame({f"type_{t}": (type_idx == k).astype(float) for k, t in enumerate(TYPE_NAMES)})
        return df.drop(columns=f"type_{TYPE_NAMES[0]}").assign(duty=duty)

    def fit(self, lives: pd.DataFrame) -> RemainingLife:
        df = self._x(lives["type_idx"].to_numpy(), lives["duty"].to_numpy())
        df["T"] = lives["duration"].clip(lower=0.5).to_numpy()
        df["E"] = lives["failed"].astype(int).to_numpy()
        self.aft.fit(df, duration_col="T", event_col="E")
        return self

    def median_remaining(self, type_idx: np.ndarray, duty: np.ndarray, age: np.ndarray) -> np.ndarray:
        """Median of T - a given T > a, from the fitted Weibull: lam((a/lam)^k + ln 2)^(1/k) - a."""
        df = self._x(type_idx, duty)
        lam_coef = self.aft.params_.loc["lambda_"]
        lin = np.full(len(df), float(lam_coef["Intercept"]))
        for c in df.columns:
            lin += float(lam_coef[c]) * df[c].to_numpy()
        lam = np.exp(lin)
        k = float(np.exp(self.aft.params_.loc["rho_"]["Intercept"]))
        a = np.maximum(age, 0.0)
        return lam * ((a / lam) ** k + np.log(2.0)) ** (1.0 / k) - a


def lives_from_log(
    assets: pd.DataFrame, maintenance: pd.DataFrame, duty: pd.Series, first_day: int, last_day: int
) -> pd.DataFrame:
    """Observable lives: spans between returns from maintenance, ended by a failure or censored.

    Only lives that start inside the window are kept, so none is left-truncated.
    """
    m = maintenance.copy()
    m["day"] = day_index(m["date"])
    m = m[(m["day"] >= first_day) & (m["day"] <= last_day)].sort_values("day")
    tmap = dict(zip(assets["asset_id"], assets["asset_type"], strict=True))
    rows = []
    for aid, g in m.groupby("asset_id"):
        days = g["day"].to_numpy()
        back = days + g["downtime_days"].to_numpy() - 1
        failed = (g["kind"] == "corrective").to_numpy()
        for j in range(len(days)):
            start = back[j]
            if j + 1 < len(days):
                rows.append((aid, start, days[j + 1] - start, bool(failed[j + 1])))
            else:
                rows.append((aid, start, last_day - start, False))
    out = pd.DataFrame(rows, columns=["asset_id", "start_day", "duration", "failed"])
    out = out[out["duration"] > 0]
    out["asset_type"] = out["asset_id"].map(tmap)
    out["type_idx"] = out["asset_type"].map({t: k for k, t in enumerate(TYPE_NAMES)})
    out["duty"] = out["asset_id"].map(duty).fillna(duty.mean())
    return out.reset_index(drop=True)
