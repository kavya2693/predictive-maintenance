"""Plant layout, failure physics, costs and policy constants.

Everything that a reviewer might want to argue with lives here, so a change of assumption
is one edit and one re-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

START_DATE = date(2023, 1, 1)
DAYS_PER_YEAR = 365
HISTORY_YEARS = 3  # two years to train on, the third held out
TRAIN_DAYS = 2 * DAYS_PER_YEAR
VALID_DAYS = 120  # tail of the training period used for early stopping and calibration

HORIZON_DAYS = 14  # "will this asset fail in the next two weeks"
WINDOW_DAYS = 60  # sensor history the features look back over
COOLDOWN_DAYS = 21  # an asset serviced recently is not offered to the crew again

SENSORS = ("vibration", "temperature", "pressure", "flow", "current")
FAILURE_MODES = ("bearing", "seal", "overheating", "valve", "fouling", "tube_leak", "sudden")

# Annual plant turnaround: everything stops, nothing ages, no readings.
TURNAROUND_DOY = range(152, 162)
# Public holidays run the plant at reduced throughput.
HOLIDAY_DOY = (1, 91, 92, 93, 159, 160, 335, 336, 359)

# Production lost per asset-day of downtime. A single figure keeps the cost model readable;
# in practice it depends on whether the asset has an installed spare.
DOWNTIME_COST_PER_DAY = 3_000.0
PLANNED_JOB_COST = 1_500.0
PLANNED_JOB_DOWNTIME_DAYS = 1


@dataclass(frozen=True)
class AssetType:
    name: str
    count: int
    weibull_shape: float
    weibull_scale_days: float  # operating days at the reference load
    modes: dict[str, float]
    fail_downtime_days: int
    fail_repair_cost: float
    history_pm_interval: int  # what the plant did before this project: calendar PM
    has_sensor: tuple[bool, bool, bool, bool, bool]  # aligned with SENSORS

    @property
    def failure_cost(self) -> float:
        return self.fail_repair_cost + self.fail_downtime_days * DOWNTIME_COST_PER_DAY


ASSET_TYPES: tuple[AssetType, ...] = (
    AssetType(
        name="pump",
        count=110,
        weibull_shape=2.2,
        weibull_scale_days=330.0,
        modes={"bearing": 0.42, "seal": 0.30, "overheating": 0.10, "sudden": 0.18},
        fail_downtime_days=3,
        fail_repair_cost=9_000.0,
        history_pm_interval=240,
        has_sensor=(True, True, True, True, True),
    ),
    AssetType(
        name="compressor",
        count=40,
        weibull_shape=2.0,
        weibull_scale_days=280.0,
        modes={"bearing": 0.32, "valve": 0.30, "overheating": 0.20, "sudden": 0.18},
        fail_downtime_days=5,
        fail_repair_cost=22_000.0,
        history_pm_interval=200,
        has_sensor=(True, True, True, True, True),
    ),
    AssetType(
        name="heat_exchanger",
        count=50,
        weibull_shape=3.0,
        weibull_scale_days=420.0,
        modes={"fouling": 0.60, "tube_leak": 0.25, "sudden": 0.15},
        fail_downtime_days=4,
        fail_repair_cost=12_000.0,
        history_pm_interval=365,
        # No vibration probe and no motor on a shell-and-tube exchanger.
        has_sensor=(False, True, True, True, False),
    ),
)

# Days of visible precursor before a failure, as (mean, sd). Overheating gives almost no
# warning; fouling builds for weeks. "sudden" has none by definition.
PRECURSOR_DAYS: dict[str, tuple[float, float]] = {
    "bearing": (21.0, 7.0),
    "seal": (10.0, 4.0),
    "overheating": (4.0, 1.5),
    "valve": (14.0, 5.0),
    "fouling": (35.0, 10.0),
    "tube_leak": (8.0, 3.0),
    "sudden": (0.0, 0.0),
}


@dataclass(frozen=True)
class PolicyConfig:
    crew_jobs_per_week: int = 6
    working_days: tuple[int, ...] = (0, 1, 2, 3, 4)  # Monday to Friday
    eval_seeds: tuple[int, ...] = (101, 102, 103, 104, 105)
    budgets_to_sweep: tuple[int, ...] = (3, 6, 10)
    lgbm_params: dict[str, float | int] = field(
        default_factory=lambda: {
            "n_estimators": 600,
            "learning_rate": 0.04,
            "num_leaves": 31,
            "min_child_samples": 60,
            "subsample": 0.8,
            "subsample_freq": 1,
            "colsample_bytree": 0.8,
            "reg_lambda": 1.0,
        }
    )
