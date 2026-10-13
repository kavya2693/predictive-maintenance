from __future__ import annotations

import pytest

from predictive_maintenance.data import History, run_history, tables

SMALL = {"pump": 14, "compressor": 6, "heat_exchanger": 6}


@pytest.fixture(scope="session")
def small_history() -> History:
    return tables(run_history(seed=3, days=500, counts=SMALL))
