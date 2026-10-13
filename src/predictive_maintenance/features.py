"""Rolling features over a sensor window, and the observable training labels.

One function, `window_features`, computes features for both the offline table and the live
closed loop. Having two implementations is the usual way train/serve skew creeps in, and a
test asserts the two paths agree.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from .config import ASSET_TYPES, HORIZON_DAYS, SENSORS, START_DATE, WINDOW_DAYS

RECENT = 14  # days excluded from the baseline so a developing fault does not raise its own bar

TYPE_NAMES = [t.name for t in ASSET_TYPES]
SENSOR_FEATURES = [f"{s}_{f}" for s in SENSORS for f in ("rel3", "rel7", "slope14", "sd7")]
FEATURE_NAMES = [
    *SENSOR_FEATURES,
    "load_m7",
    "load_rel3",
    "days_since_maint",
    *[f"type_{t}" for t in TYPE_NAMES],
]


def _nanslope(y: np.ndarray) -> np.ndarray:
    """Least-squares slope per row along the last axis, ignoring NaNs."""
    x = np.arange(y.shape[-1], dtype=float)
    mask = ~np.isnan(y)
    cnt = mask.sum(-1)
    xm = np.where(mask, x, 0.0).sum(-1) / np.maximum(cnt, 1)
    ym = np.where(mask, y, 0.0).sum(-1) / np.maximum(cnt, 1)
    dx = np.where(mask, x - xm[..., None], 0.0)
    dy = np.where(mask, y - ym[..., None], 0.0)
    den = (dx * dx).sum(-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = (dx * dy).sum(-1) / den
    return np.where(cnt >= 5, slope, np.nan)


def window_features(
    win: np.ndarray, load_win: np.ndarray, days_since_maint: np.ndarray, type_idx: np.ndarray
) -> np.ndarray:
    """Features for m windows. win is (m, W, sensors), load_win (m, W)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        base = np.nanmedian(win[:, :-RECENT, :], axis=1)  # (m, S)
        med3 = np.nanmedian(win[:, -3:, :], axis=1)
        med7 = np.nanmedian(win[:, -7:, :], axis=1)
        sd7 = np.nanstd(win[:, -7:, :], axis=1)
        slope = _nanslope(np.moveaxis(win[:, -RECENT:, :], 1, 2))  # (m, S)
        with np.errstate(invalid="ignore", divide="ignore"):
            rel3 = med3 / base - 1.0
            rel7 = med7 / base - 1.0
            sdr = sd7 / np.abs(base)
            slr = slope / np.abs(base)
        sensor_block = np.stack([rel3, rel7, slr, sdr], axis=2).reshape(len(win), -1)

        load_base = np.nanmedian(load_win[:, :-RECENT], axis=1)
        load_m7 = np.nanmean(load_win[:, -7:], axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            load_rel3 = np.nanmean(load_win[:, -3:], axis=1) / load_base - 1.0
    onehot = np.eye(len(TYPE_NAMES))[type_idx]
    return np.column_stack([sensor_block, load_m7, load_rel3, days_since_maint, onehot]).astype(float)


def day_index(dates: pd.Series) -> np.ndarray:
    return np.asarray((pd.to_datetime(dates) - pd.Timestamp(START_DATE)).dt.days)


def last_service_day(assets: pd.DataFrame, maintenance: pd.DataFrame, n_days: int) -> np.ndarray:
    """(assets, days) array of the day each asset last came back from maintenance, as a CMMS would know it."""
    ids = list(assets["asset_id"])
    out = np.empty((len(ids), n_days), dtype=int)
    m = maintenance.copy()
    m["day"] = day_index(m["date"])
    m["back"] = m["day"] + m["downtime_days"] - 1
    first = day_index(assets["last_service_date"])
    for a, aid in enumerate(ids):
        marks = np.full(n_days, -(10**6))
        marks[0] = first[a]
        for b in m.loc[m["asset_id"] == aid, "back"]:
            if b < n_days:
                marks[b] = max(marks[b], b)
        out[a] = np.maximum.accumulate(marks)
    return out


def build_table(assets: pd.DataFrame, readings: pd.DataFrame, maintenance: pd.DataFrame) -> pd.DataFrame:
    """One row per asset per running day from day WINDOW_DAYS-1 on, with features and labels."""
    ids = list(assets["asset_id"])
    type_idx = assets["asset_type"].map({t: k for k, t in enumerate(TYPE_NAMES)}).to_numpy()
    r = readings.copy()
    r["day"] = day_index(r["date"])
    n_days = int(r["day"].max()) + 1
    r = r.set_index(["asset_id", "day"]).sort_index()
    since = last_service_day(assets, maintenance, n_days)

    m = maintenance.copy()
    m["day"] = day_index(m["date"])
    frames = []
    for a, aid in enumerate(ids):
        g = r.loc[aid]
        sens = g[list(SENSORS)].to_numpy(dtype=float)
        load = np.where(g["status"].to_numpy() == "running", g["load_pct"].to_numpy() / 100.0, np.nan)
        wins = np.moveaxis(sliding_window_view(sens, WINDOW_DAYS, axis=0), 2, 1)  # (k, W, S)
        lwins = sliding_window_view(load, WINDOW_DAYS)
        days = np.arange(WINDOW_DAYS - 1, n_days)
        dsm = days - since[a, days]
        X = window_features(wins, lwins, dsm, np.full(len(days), type_idx[a]))
        df = pd.DataFrame(X, columns=FEATURE_NAMES)
        df.insert(0, "day", days)
        df.insert(0, "asset_id", aid)
        df["asset_idx"] = a
        df["type_idx"] = type_idx[a]
        df["running"] = g["status"].to_numpy()[days] == "running"
        df["label"], df["time_to_event"], df["event_is_failure"] = _labels(m[m["asset_id"] == aid], days, n_days)
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def _labels(m: pd.DataFrame, days: np.ndarray, n_days: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Observable label: failure within the horizon, NaN where a preventive job or the end of
    the data hides the answer. Also time to the next event, for the survival models."""
    ev_days = m["day"].to_numpy()
    ev_fail = (m["kind"] == "corrective").to_numpy()
    order = np.argsort(ev_days, kind="stable")
    ev_days, ev_fail = ev_days[order], ev_fail[order]
    idx = np.searchsorted(ev_days, days, side="right")  # next event strictly after the day
    has_next = idx < len(ev_days)
    nxt = np.where(has_next, ev_days[np.minimum(idx, len(ev_days) - 1)], n_days - 1)
    nxt_fail = np.where(has_next, ev_fail[np.minimum(idx, len(ev_days) - 1)], False)
    dt = nxt - days
    label = np.full(len(days), np.nan)
    label[(dt <= HORIZON_DAYS) & nxt_fail & has_next] = 1.0
    label[dt > HORIZON_DAYS] = 0.0
    return label, dt.astype(float), (nxt_fail & has_next)


def latent_labels(table: pd.DataFrame, lives: pd.DataFrame) -> np.ndarray:
    """Truth by construction: does the asset's current latent life end within the horizon?

    Unlike the observable label this is defined even when a preventive job removed the
    failure, so it is the right yardstick for evaluation and the wrong one for training.
    """
    out = np.zeros(len(table))
    for aid, idx in table.groupby("asset_id").indices.items():
        lv = lives[lives["asset_id"] == aid].sort_values("start_day")
        starts = lv["start_day"].to_numpy()
        lf = lv["latent_failure_day"].to_numpy()
        days = table["day"].to_numpy()[idx]
        k = np.searchsorted(starts, days, side="right") - 1
        rem = lf[np.clip(k, 0, len(lf) - 1)] - days
        out[idx] = ((rem > 0) & (rem <= HORIZON_DAYS)).astype(float)
    return out
