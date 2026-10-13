"""Closed loop: run a policy against the plant for a year and count what it cost.

The plant is forked at the end of the training history, and every policy runs from the same
fork with the same random stream, so a difference in uptime is caused by the policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import DOWNTIME_COST_PER_DAY, FAILURE_MODES, HORIZON_DAYS, PLANNED_JOB_COST
from .plant import FAILED, PLANNED, RUNNING, Plant
from .policy import Alert, Crew, Policy, schedule


@dataclass
class PolicyRun:
    policy: str
    crew_jobs_per_week: int
    seed: int
    alerts: list[Alert] = field(default_factory=list)
    failures: int = 0
    sudden_failures: int = 0
    planned_jobs: int = 0
    running_days: int = 0
    down_days: int = 0
    repair_cost: float = 0.0
    failures_by_mode: dict[str, int] = field(default_factory=dict)

    @property
    def true_alerts(self) -> list[Alert]:
        return [a for a in self.alerts if 0 < a.latent_remaining <= HORIZON_DAYS]

    def caught_by_mode(self) -> dict[str, tuple[int, int]]:
        """(caught, would-have-failed) per latent failure mode."""
        out: dict[str, tuple[int, int]] = {}
        for m in FAILURE_MODES:
            caught = sum(1 for a in self.true_alerts if a.mode == m)
            out[m] = (caught, caught + self.failures_by_mode.get(m, 0))
        return out

    def metrics(self) -> dict[str, float]:
        tp = self.true_alerts
        n_alerts = len(self.alerts)
        caught = len(tp)
        cost = (
            self.planned_jobs * PLANNED_JOB_COST + self.repair_cost + self.down_days * DOWNTIME_COST_PER_DAY
        )
        return {
            "alerts": n_alerts,
            "precision_at_budget": caught / n_alerts if n_alerts else float("nan"),
            # Lenient reading: the alert landed within 30 days of a failure, i.e. early rather than wrong.
            "precision_30d": (
                sum(1 for a in self.alerts if 0 < a.latent_remaining <= 30) / n_alerts
                if n_alerts
                else float("nan")
            ),
            "median_lead_days": float(np.median([a.latent_remaining for a in tp])) if tp else float("nan"),
            # A caught failure is one an alert pre-empted inside the horizon; the share is of
            # all failures that would otherwise have happened in the year.
            "failures_caught_share": caught / (caught + self.failures) if caught + self.failures else 0.0,
            "failures": self.failures,
            "sudden_failures": self.sudden_failures,
            "planned_jobs": self.planned_jobs,
            "uptime": self.running_days / (self.running_days + self.down_days),
            "cost_total": cost,
        }


def run_policy(plant: Plant, policy: Policy, days: int, crew_jobs_per_week: int, seed: int) -> PolicyRun:
    run = PolicyRun(policy.name, crew_jobs_per_week, seed)
    crew = Crew(crew_jobs_per_week)
    for _ in range(days):
        alerts = schedule(policy, plant, crew)
        res = plant.step([a.asset for a in alerts])
        run.alerts.extend(alerts)
        run.running_days += int((res.status == RUNNING).sum())
        run.down_days += int(((res.status == FAILED) | (res.status == PLANNED)).sum())
        for e in res.events:
            if e.kind == "preventive":
                run.planned_jobs += 1
            else:
                run.failures += 1
                run.sudden_failures += int(e.failure_mode == "sudden")
                mode = e.failure_mode or "unknown"
                run.failures_by_mode[mode] = run.failures_by_mode.get(mode, 0) + 1
                run.repair_cost += plant.types[plant.type_idx[e.asset]].fail_repair_cost
    return run
