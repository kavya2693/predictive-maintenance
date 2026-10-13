"""Offline scoring on the held-out year, against the latent lives the generator recorded."""

from __future__ import annotations

import numpy as np
import pandas as pd
from lifelines.utils import concordance_index
from sklearn.metrics import average_precision_score

from .config import COOLDOWN_DAYS, START_DATE
from .features import FEATURE_NAMES
from .policy import Observation, Policy


def policy_scores(policy: Policy, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Run a policy's scoring over table rows exactly as the scheduler would run it live."""
    obs = Observation(
        day=-1,
        X=rows[FEATURE_NAMES].to_numpy(),
        days_since_maint=rows["days_since_maint"].to_numpy(),
        type_idx=rows["type_idx"].to_numpy(),
        eligible=np.ones(len(rows), dtype=bool),
        latent_remaining=np.full(len(rows), np.nan),
    )
    return policy.score(obs)


def weekly_precision(rows: pd.DataFrame, score: np.ndarray, k: int) -> tuple[float, int]:
    """Each Monday take the top-k proposed assets; share whose latent life ends within the horizon."""
    monday = (START_DATE.weekday() + rows["day"].to_numpy()) % 7 == 0
    ok = monday & np.isfinite(score) & (rows["days_since_maint"].to_numpy() >= COOLDOWN_DAYS)
    df = pd.DataFrame(
        {"day": rows["day"].to_numpy()[ok], "s": score[ok], "y": rows["y_latent"].to_numpy()[ok]}
    )
    top = df.sort_values(["day", "s"], ascending=[True, False]).groupby("day").head(k)
    return (float(top["y"].mean()) if len(top) else float("nan")), len(top)


def average_precision(y: np.ndarray, p: np.ndarray) -> float:
    return float(average_precision_score(y, np.nan_to_num(p, nan=-1.0)))


def lead_times(
    rows: pd.DataFrame, score: np.ndarray, lives: pd.DataFrame, lookback: int = 30
) -> pd.DataFrame:
    """For every failure in the held-out year: did the policy propose the asset beforehand, and how early?

    The lead is measured from the first proposal in the 30 days before the failure. A lead of
    one day is recorded but is not enough to schedule a crew.
    """
    first_day = int(rows["day"].min()) + lookback
    fails = lives[(lives["ended_by"] == "failure") & (lives["end_day"] >= first_day)]
    by_asset = {aid: g for aid, g in rows.assign(_s=score).groupby("asset_id")}
    out = []
    for _, f in fails.iterrows():
        g = by_asset.get(f["asset_id"])
        if g is None:
            continue
        end = int(f["end_day"])
        w = g[(g["day"] >= end - lookback) & (g["day"] < end)]
        hit = w[np.isfinite(w["_s"].to_numpy())]
        lead = float(end - hit["day"].min()) if len(hit) else 0.0
        out.append({"asset_id": f["asset_id"], "mode": f["mode"], "lead_days": lead})
    return pd.DataFrame(out)


def concordance(landmarks: pd.DataFrame, risk: np.ndarray) -> float:
    """Harrell's C: does a higher risk go with an earlier failure? Preventive jobs censor."""
    return float(
        concordance_index(
            landmarks["time_to_event"], -np.nan_to_num(risk, nan=0.0), landmarks["event_is_failure"]
        )
    )
