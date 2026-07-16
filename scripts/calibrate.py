#!/usr/bin/env python3
"""Calibration study over settled backtest trades.

Answers the prerequisite question for any sizing/allocation upgrade: do the
scanner's probabilities (N(d2) at market IV) and after-cost EV actually
predict settled outcomes? See options_trader/backtest/calibration.py for
what is measured and the caveats printed with the report.

Workflow (DoltHub history example):

    python scripts/import_dolthub.py --symbols SPY QQQ IWM \\
        --start 2024-01-01 --end 2026-06-30
    python scripts/backtest.py --snapshots-dir data_snapshots_dolthub \\
        --config configs/dolthub_backtest.json \\
        --save-trades runs/dolthub_trades.json
    python scripts/calibrate.py runs/dolthub_trades.json

Use --by underlying / --by kind for per-slice reports on top of the pooled
one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.backtest.calibration import calibration_report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trades_file",
                    help="JSON written by scripts/backtest.py --save-trades")
    ap.add_argument("--bins", type=int, default=5,
                    help="Quantile bins for reliability/EV tables (default 5)")
    ap.add_argument("--by", choices=["underlying", "kind"], action="append",
                    default=[],
                    help="Also report per-slice (repeatable)")
    args = ap.parse_args()

    payload = json.loads(Path(args.trades_file).read_text())
    trades = payload.get("trades", [])
    if not trades:
        print(f"No settled trades in {args.trades_file} — nothing to calibrate.")
        return 1

    src = payload.get("snapshots_dir", "?")
    print(f"Source: {args.trades_file} (snapshots: {src})")
    if "dolthub" in str(src):
        print("Note: DoltHub snapshots carry no volume/OI — results are "
              "optimistic on liquidity (see README).")
    print()
    print(calibration_report(trades, n_bins=args.bins))

    for field in args.by:
        for value in sorted({t[field] for t in trades}):
            subset = [t for t in trades if t[field] == value]
            print()
            print(f"=== {field} = {value} ===")
            print(calibration_report(subset, n_bins=args.bins))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
