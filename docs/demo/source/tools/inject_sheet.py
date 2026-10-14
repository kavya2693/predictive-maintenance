"""Inline the exported plant run into the compositions (renders cannot fetch files).

Writes three marker blocks from assets/plant_run.json (made by tools/export_work_orders.py):
  compositions/value.html       /* SHEET:BEGIN */ ... /* SHEET:END */   the week's work orders
  compositions/explainer.html   /* LOOP:BEGIN */  ... /* LOOP:END */    breakdown days per policy
  compositions/s04-running.html /* RUN:BEGIN */   ... /* RUN:END */     cumulative failures + trace

Usage: python3 tools/inject_sheet.py assets/plant_run.json compositions --rows 0,1,2,3,4,5
"""

import argparse
import json
import re
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("run")
ap.add_argument("comp_dir")
ap.add_argument("--rows", required=True, help="indexes of work orders shown as sheet rows, in order")
a = ap.parse_args()

d = json.loads(Path(a.run).read_text())
orders = d["work_orders"]
rows = [orders[int(i)] for i in a.rows.split(",")]
sheet = {
    "fork_date": d["fork_date"],
    "crew_jobs_per_week": d["crew_jobs_per_week"],
    "orders_two_weeks": len(orders),
    "rows": [
        {
            "id": o["work_order_id"],
            "created": o["created_on"],
            "priority": o["priority"],
            "asset": o["asset"]["asset_id"],
            "type": o["asset"]["asset_type"],
            "loc": o["asset"]["functional_location"],
            "p": o["risk"]["failure_probability"],
            "saving": o["risk"]["expected_saving"],
            "evidence": o["evidence"][0],
            "due": o["schedule"]["due_by"],
            "crew": o["schedule"]["crew"],
            "truth_days": o["truth"]["latent_days_to_failure"],
            "truth_mode": o["truth"]["latent_mode"],
        }
        for o in rows
    ],
}


def fail_days(cum: list[int]) -> list[int]:
    out, prev = [], 0
    for day, c in enumerate(cum):
        out += [day] * (c - prev)
        prev = c
    return out


daily = d["daily"]
loop = {
    k: fail_days(daily[p]["cum_failures"])
    for k, p in (("calendar", "fixed-interval"), ("alarm", "alarm-limits"), ("model", "gbm-sensors"))
}
run = {
    "seed": d["seed"],
    "curves": {p: v["cum_failures"] for p, v in daily.items()},
    "trace": d["trace"],
    "first_order": {
        "id": orders[0]["work_order_id"],
        "asset": orders[0]["asset"]["asset_id"],
        "type": orders[0]["asset"]["asset_type"],
        "loc": orders[0]["asset"]["functional_location"],
        "p": orders[0]["risk"]["failure_probability"],
        "priority": orders[0]["priority"],
        "evidence": orders[0]["evidence"][0],
        "rul": orders[0]["risk"]["fleet_median_remaining_life_days"],
        "due": orders[0]["schedule"]["due_by"],
        "crew": orders[0]["schedule"]["crew"],
        "truth_days": orders[0]["truth"]["latent_days_to_failure"],
        "truth_mode": orders[0]["truth"]["latent_mode"],
    },
    "totals": {p: {"failures": v["failures"], "planned_jobs": v["planned_jobs"]} for p, v in daily.items()},
}


def inject(path: Path, name: str, var: str, obj: object) -> None:
    html = path.read_text()
    data = json.dumps(obj, separators=(",", ":"))
    block = f"/* {name}:BEGIN */ const {var} = {data}; /* {name}:END */"
    html, n = re.subn(rf"/\* {name}:BEGIN \*/.*?/\* {name}:END \*/", lambda _: block, html, flags=re.S)
    assert n == 1, f"{name} marker not found in {path}"
    path.write_text(html)


comp = Path(a.comp_dir)
inject(comp / "value.html", "SHEET", "SHEET", sheet)
inject(comp / "explainer.html", "LOOP", "LOOP", loop)
inject(comp / "s04-running.html", "RUN", "RUN", run)
print("injected", len(sheet["rows"]), "rows;", {k: len(v) for k, v in loop.items()}, "breakdown days")
