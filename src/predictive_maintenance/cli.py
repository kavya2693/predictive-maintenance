"""One command: train on two years of history, score the third offline, then run every policy
for a year in the closed loop and write results and work orders to outputs/."""

from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import DAYS_PER_YEAR, HORIZON_DAYS, TRAIN_DAYS, VALID_DAYS, PolicyConfig
from .data import generate, load, run_history
from .evaluate import average_precision, concordance, lead_times, policy_scores, weekly_precision
from .features import FEATURE_NAMES, TYPE_NAMES, build_table, day_index, latent_labels
from .models import AgeHazard, CoxModel, GBMModel, RemainingLife, WeibullByType, lives_from_log
from .plant import Plant
from .policy import CostAwareModel, FixedInterval, Oracle, Policy, RunToFailure, ThresholdRule
from .simulate import PolicyRun, run_policy
from .workorders import build, validate

SCHEDULE_LEAD = 2  # days a crew needs between an alert and the job


def log(msg: str) -> None:
    print(msg, flush=True)


def _censor_at(rows: pd.DataFrame, cutoff: int) -> pd.DataFrame:
    """Survival targets that do not look past the training cutoff."""
    rows = rows.copy()
    horizon = cutoff - 1 - rows["day"]
    beyond = rows["time_to_event"] > horizon
    rows.loc[beyond, "time_to_event"] = horizon[beyond]
    rows.loc[beyond, "event_is_failure"] = False
    return rows[rows["time_to_event"] > 0]


def fit_models(
    table: pd.DataFrame, assets: pd.DataFrame, maint: pd.DataFrame, cfg: PolicyConfig
) -> dict[str, Any]:
    run = table[table["running"]]
    # A label is only usable if its whole horizon is inside the period it belongs to.
    fit_end = TRAIN_DAYS - VALID_DAYS
    train = run[(run["day"] + HORIZON_DAYS < fit_end) & run["label"].notna()]
    valid = run[(run["day"] >= fit_end) & (run["day"] + HORIZON_DAYS < TRAIN_DAYS) & run["label"].notna()]
    gbm = GBMModel(cfg.lgbm_params).fit(train, train["label"].to_numpy(), valid, valid["label"].to_numpy())

    landmarks = _censor_at(run[(run["day"] < TRAIN_DAYS) & (run["day"] % 7 == 0)], TRAIN_DAYS)
    cox = CoxModel().fit(landmarks)

    duty = run[run["day"] < TRAIN_DAYS].groupby("asset_id")["load_m7"].mean()
    lives = lives_from_log(assets, maint, duty, 0, TRAIN_DAYS - 1)
    weib = WeibullByType.fit(lives)
    intervals = np.array([weib.optimal_interval(k)[0] for k in range(len(TYPE_NAMES))])
    rul = RemainingLife().fit(lives)
    return {
        "gbm": gbm,
        "cox": cox,
        "weibull": weib,
        "intervals": intervals,
        "rul": rul,
        "duty": duty,
        "n_train": len(train),
        "n_valid": len(valid),
        "train_pos_rate": float(train["label"].mean()),
        "n_lives": len(lives),
        "n_landmarks": len(landmarks),
        "best_iteration": int(gbm.clf.best_iteration_ or cfg.lgbm_params["n_estimators"]),
    }


def make_policies(m: dict[str, Any]) -> dict[str, Policy]:
    return {
        "run-to-failure": RunToFailure(),
        "fixed-interval": FixedInterval(m["intervals"]),
        "alarm-limits": ThresholdRule(),
        "weibull-age": CostAwareModel(AgeHazard(m["weibull"]), "weibull-age"),
        "cox-sensors": CostAwareModel(m["cox"], "cox-sensors"),
        "gbm-sensors": CostAwareModel(m["gbm"], "gbm-sensors"),
        "oracle": Oracle(),
    }


def offline(
    table: pd.DataFrame, lives: pd.DataFrame, m: dict[str, Any], policies: dict[str, Policy], k: int
) -> dict:
    test = table[table["running"] & (table["day"] >= TRAIN_DAYS)].copy()
    test["y_latent"] = latent_labels(test, lives)
    probs = {
        "gbm-sensors": m["gbm"].predict(test),
        "cox-sensors": m["cox"].predict(test),
        "weibull-age": AgeHazard(m["weibull"]).predict(test),
    }
    landmarks = test[test["day"] % 7 == 0]
    out: dict[str, Any] = {"base_rate": float(test["y_latent"].mean()), "rows": len(test), "policies": {}}
    test_lives = lives[lives["end_day"] >= TRAIN_DAYS]
    for name in ["fixed-interval", "alarm-limits", "weibull-age", "cox-sensors", "gbm-sensors"]:
        score, _ = policy_scores(policies[name], test)
        prec, n = weekly_precision(test, score, k)
        lt = lead_times(test, score, test_lives)
        gradual = lt[lt["mode"] != "sudden"]
        usable = gradual["lead_days"] >= SCHEDULE_LEAD
        row: dict[str, Any] = {
            "weekly_precision_at_budget": prec,
            "weekly_alerts": n,
            "gradual_failures": len(gradual),
            "gradual_detected_share": float(usable.mean()) if len(gradual) else float("nan"),
            "median_lead_days_detected": float(gradual.loc[usable, "lead_days"].median())
            if usable.any()
            else 0.0,
            "sudden_detected_share": float(
                (lt.loc[lt["mode"] == "sudden", "lead_days"] >= SCHEDULE_LEAD).mean()
            ),
            "by_mode": {
                mode: round(float((g["lead_days"] >= SCHEDULE_LEAD).mean()), 3)
                for mode, g in lt.groupby("mode")
            },
        }
        if name in probs:
            p = probs[name]
            row["average_precision"] = average_precision(test["y_latent"].to_numpy(), p)
            row["mean_predicted"] = float(np.mean(p))
            row["c_index"] = concordance(landmarks, p[test["day"].to_numpy() % 7 == 0])
        elif name == "alarm-limits":
            raw = ThresholdRule.ratio(test[FEATURE_NAMES].to_numpy())
            row["average_precision"] = average_precision(test["y_latent"].to_numpy(), raw)
            row["c_index"] = concordance(landmarks, raw[test["day"].to_numpy() % 7 == 0])
        out["policies"][name] = row
    rul = m["rul"].median_remaining(
        landmarks["type_idx"].to_numpy(),
        landmarks["asset_id"].map(m["duty"]).fillna(m["duty"].mean()).to_numpy(),
        landmarks["days_since_maint"].to_numpy(),
    )
    out["rul_c_index"] = concordance(landmarks, -rul)
    return out


def closed_loop(
    data_seed: int, policies: dict[str, Policy], cfg: PolicyConfig, seeds: tuple[int, ...], sweep: bool
) -> tuple[list[PolicyRun], Plant]:
    base = run_history(data_seed, TRAIN_DAYS, record=False)
    runs = []
    plan: list[tuple[str, int]] = [(name, cfg.crew_jobs_per_week) for name in policies]
    if sweep:
        plan += [
            (name, k)
            for k in cfg.budgets_to_sweep
            if k != cfg.crew_jobs_per_week
            for name in ("fixed-interval", "alarm-limits", "gbm-sensors", "oracle")
        ]
    for s in seeds:
        fork = base.fork(s)
        for name, k in plan:
            t0 = time.time()
            r = run_policy(copy.deepcopy(fork), policies[name], DAYS_PER_YEAR, k, s)
            runs.append(r)
            mm = r.metrics()
            log(
                f"  seed={s} crew={k:>2}/wk {name:<15} uptime={mm['uptime']:.4f} "
                f"failures={mm['failures']:>3} "
                f"jobs={mm['planned_jobs']:>3} precision={mm['precision_at_budget']:.2f} "
                f"cost={mm['cost_total'] / 1e6:.2f}M ({time.time() - t0:.1f}s)"
            )
    return runs, base


def summarise(runs: list[PolicyRun]) -> pd.DataFrame:
    rows = []
    for r in runs:
        mm = r.metrics()
        rows.append({"policy": r.policy, "crew": r.crew_jobs_per_week, "seed": r.seed, **mm})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--out", type=Path, default=Path("outputs"))
    ap.add_argument("--data-seed", type=int, default=7)
    ap.add_argument("--seeds", type=int, default=5, help="closed-loop evaluation seeds")
    ap.add_argument("--no-sweep", action="store_true", help="skip the crew-budget sweep")
    args = ap.parse_args()
    cfg = PolicyConfig()
    args.out.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    if not (args.data / "sensor_readings.csv.gz").exists():
        log("no data found; generating")
        generate(args.data_seed, args.data)
    assets, readings, maint, lives = load(args.data)
    log(f"data: {len(assets)} assets, {len(readings):,} daily readings, {len(maint)} maintenance jobs")
    table = build_table(assets, readings, maint)

    m = fit_models(table, assets, maint, cfg)
    log(
        f"gbm: {m['n_train']:,} train rows ({m['train_pos_rate']:.3%} positive), {m['n_valid']:,} valid, "
        f"stopped at {m['best_iteration']} trees"
    )
    log(f"cox: {m['n_landmarks']:,} landmark rows; weibull: {m['n_lives']} observed lives")
    w = m["weibull"]
    for k, name in enumerate(TYPE_NAMES):
        t_opt, ratio = w.optimal_interval(k)
        log(
            f"  {name:<15} shape={w.shape[k]:.2f} scale={w.scale[k]:.0f}d  "
            f"interval={t_opt:.0f}d  cost-rate vs RTF={ratio:.2f}"
        )
    policies = make_policies(m)

    off = offline(table, lives, m, policies, cfg.crew_jobs_per_week)
    log(f"offline (held-out year, base rate {off['base_rate']:.3%}):")
    for name, row in off["policies"].items():
        log(f"  {name:<15} " + "  ".join(f"{k}={v:.3f}" for k, v in row.items() if isinstance(v, float)))
    log(f"  weibull RUL c-index={off['rul_c_index']:.3f}")

    # The CSV history and the forked plant must be the same plant, or the closed loop tests
    # policies on a plant the models never saw.
    seeds = cfg.eval_seeds[: args.seeds]
    log(f"closed loop: {len(seeds)} seeds x {DAYS_PER_YEAR} days")
    runs, base = closed_loop(args.data_seed, policies, cfg, seeds, sweep=not args.no_sweep)
    logged = int((day_index(maint["date"]) < TRAIN_DAYS).sum())
    assert len(base.events) == logged, f"fork has {len(base.events)} events, log has {logged}"

    df = summarise(runs)
    agg = (
        df.groupby(["policy", "crew"])
        .agg(
            uptime=("uptime", "mean"),
            uptime_sd=("uptime", "std"),
            failures=("failures", "mean"),
            planned_jobs=("planned_jobs", "mean"),
            precision_at_budget=("precision_at_budget", "mean"),
            precision_30d=("precision_30d", "mean"),
            median_lead_days=("median_lead_days", "mean"),
            failures_caught_share=("failures_caught_share", "mean"),
            cost_total=("cost_total", "mean"),
        )
        .reset_index()
    )
    k0 = cfg.crew_jobs_per_week
    main_tbl = agg[agg["crew"] == k0].set_index("policy")
    paired = {}
    for other in ["run-to-failure", "fixed-interval", "alarm-limits", "weibull-age", "cox-sensors"]:
        a = df[(df["policy"] == "gbm-sensors") & (df["crew"] == k0)].set_index("seed")
        b = df[(df["policy"] == other) & (df["crew"] == k0)].set_index("seed")
        du = (a["uptime"] - b["uptime"]) * 100
        dc = a["cost_total"] - b["cost_total"]
        paired[other] = {
            "uptime_pp_mean": float(du.mean()),
            "uptime_pp_min": float(du.min()),
            "uptime_pp_max": float(du.max()),
            "cost_mean": float(dc.mean()),
            "seeds_gbm_better_uptime": int((du > 0).sum()),
        }
    by_mode: dict[str, tuple[int, int]] = {}
    for r in runs:
        if r.policy == "gbm-sensors" and r.crew_jobs_per_week == k0:
            for mode, (c, tot) in r.caught_by_mode().items():
                cc, tt = by_mode.get(mode, (0, 0))
                by_mode[mode] = (cc + c, tt + tot)

    first = next(r for r in runs if r.policy == "gbm-sensors" and r.crew_jobs_per_week == k0)
    window = [a for a in first.alerts if a.day < TRAIN_DAYS + 14]
    rul = m["rul"].median_remaining(
        base.type_idx[[a.asset for a in window]],
        np.array([m["duty"].get(assets.iloc[a.asset]["asset_id"], m["duty"].mean()) for a in window]),
        np.array([a.features[FEATURE_NAMES.index("days_since_maint")] for a in window]),
    )
    orders = build(window, assets, base.type_idx, rul, "gbm-sensors")
    bad = [e for o in orders for e in validate(o)]
    assert not bad, bad

    results = {
        "horizon_days": HORIZON_DAYS,
        "crew_jobs_per_week": k0,
        "fixed_intervals_days": dict(zip(TYPE_NAMES, map(float, m["intervals"]), strict=True)),
        "training": {k: v for k, v in m.items() if isinstance(v, (int, float))},
        "offline": off,
        "closed_loop": agg.to_dict(orient="records"),
        "paired_gbm_minus": paired,
        "gbm_caught_by_mode": {k: {"caught": v[0], "would_fail": v[1]} for k, v in by_mode.items()},
        "work_orders_first_two_weeks": len(orders),
        "runtime_seconds": round(time.time() - t_start, 1),
    }
    (args.out / "results.json").write_text(json.dumps(results, indent=2, default=float))
    (args.out / "work_orders.json").write_text(json.dumps(orders, indent=2))
    df.to_csv(args.out / "closed_loop_runs.csv", index=False)
    (args.out / "results.md").write_text(report(results, main_tbl, agg, k0))
    log(report(results, main_tbl, agg, k0))
    log(
        f"wrote {args.out}/results.json, results.md, closed_loop_runs.csv, "
        f"work_orders.json ({len(orders)} orders)"
    )
    log(f"total runtime {time.time() - t_start:.0f}s")


def _fmt(v: float, pct: bool = False, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    return f"{100 * v:.{nd}f}%" if pct else f"{v:,.{nd}f}"


def report(res: dict[str, Any], main_tbl: pd.DataFrame, agg: pd.DataFrame, k0: int) -> str:
    order = [
        "run-to-failure",
        "fixed-interval",
        "alarm-limits",
        "weibull-age",
        "cox-sensors",
        "gbm-sensors",
        "oracle",
    ]
    lines = [f"## Closed loop, one year, crew budget {k0} jobs/week (mean of seeds)", ""]
    lines.append(
        "| policy | uptime | failures | planned jobs | precision at budget | alert within 30d of failure "
        "| median lead (d) | failures caught | cost |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for p in order:
        r = main_tbl.loc[p]
        lines.append(
            f"| {p} | {_fmt(r.uptime, True)} | {r.failures:.1f} | {r.planned_jobs:.1f} | "
            f"{_fmt(r.precision_at_budget, True, 1)} | {_fmt(r.precision_30d, True, 1)} | "
            f"{_fmt(r.median_lead_days, nd=1)} | "
            f"{_fmt(r.failures_caught_share, True, 1)} | {r.cost_total / 1e6:.2f}M |"
        )
    lines += ["", "## Paired difference, gbm-sensors minus each policy (same seeds)", ""]
    lines.append("| vs | uptime pp (mean) | min..max | seeds gbm better | cost difference |")
    lines.append("|---|---|---|---|---|")
    for o, d in res["paired_gbm_minus"].items():
        lines.append(
            f"| {o} | {d['uptime_pp_mean']:+.2f} | {d['uptime_pp_min']:+.2f}..{d['uptime_pp_max']:+.2f} | "
            f"{d['seeds_gbm_better_uptime']} | {d['cost_mean'] / 1e3:+,.0f}k |"
        )
    lines += ["", "## Crew budget sweep (uptime / precision at budget)", ""]
    crews = sorted(agg["crew"].unique())
    lines.append("| policy | " + " | ".join(f"{c}/wk" for c in crews) + " |")
    lines.append("|---|" + "---|" * len(crews))
    for p in ["fixed-interval", "alarm-limits", "gbm-sensors", "oracle"]:
        cells = []
        for c in crews:
            r = agg[(agg["policy"] == p) & (agg["crew"] == c)]
            cells.append(
                "-"
                if r.empty
                else f"{_fmt(r.uptime.iloc[0], True)} / {_fmt(r.precision_at_budget.iloc[0], True, 0)}"
            )
        lines.append(f"| {p} | " + " | ".join(cells) + " |")
    off = res["offline"]
    lines += [
        "",
        f"## Offline, held-out year (latent base rate {_fmt(off['base_rate'], True)})",
        "",
        "| policy | avg precision | weekly precision at budget | gradual failures flagged >=2d ahead "
        "| median lead (d) | sudden flagged | C-index |",
        "|---|---|---|---|---|---|---|",
    ]
    for p, r in off["policies"].items():
        lines.append(
            f"| {p} | {_fmt(r.get('average_precision', float('nan')), nd=3)} | "
            f"{_fmt(r['weekly_precision_at_budget'], True, 1)} | "
            f"{_fmt(r['gradual_detected_share'], True, 1)} | "
            f"{_fmt(r['median_lead_days_detected'], nd=1)} | {_fmt(r['sudden_detected_share'], True, 1)} | "
            f"{_fmt(r.get('c_index', float('nan')), nd=3)} |"
        )
    lines.append(f"\nWeibull AFT remaining-life C-index (age, type, duty only): {off['rul_c_index']:.3f}")
    lines += [
        "",
        "## gbm-sensors in the closed loop, by latent failure mode",
        "",
        "| mode | caught | would have failed | share |",
        "|---|---|---|---|",
    ]
    for mode, v in res["gbm_caught_by_mode"].items():
        if v["would_fail"]:
            share = _fmt(v["caught"] / v["would_fail"], True, 1)
            lines.append(f"| {mode} | {v['caught']} | {v['would_fail']} | {share} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
