"""Write three years of plant history to data/ from a fixed seed."""

from __future__ import annotations

import argparse
from pathlib import Path

from predictive_maintenance.data import generate


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=Path("data"))
    args = ap.parse_args()
    hist = generate(args.seed, args.out)
    m = hist.maintenance
    print(
        f"assets={len(hist.assets)}  reading rows={len(hist.readings):,}  "
        f"preventive jobs={(m['kind'] == 'preventive').sum()}  failures={(m['kind'] == 'corrective').sum()}"
    )
    print(m[m["kind"] == "corrective"]["failure_mode"].value_counts().to_string())
    print(
        "missing sensor share:", hist.readings[["vibration", "temperature"]].isna().mean().round(3).to_dict()
    )


if __name__ == "__main__":
    main()
