#!/usr/bin/env python3
"""Import historical EOD option chains from DoltHub into snapshot storage.

    python scripts/import_dolthub.py --symbols SPY QQQ IWM \
        --start 2024-01-01 --end 2026-06-30

Writes to data_snapshots_dolthub/ (separate from live collection). Then:

    python scripts/backtest.py --snapshots-dir data_snapshots_dolthub \
        --config configs/dolthub_backtest.json

The dolthub_backtest config zeroes the volume/OI liquidity minimums because
this dataset does not provide those columns — read README.md ("DoltHub
historical backtest") for why that makes results optimistic on liquidity.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.data import SnapshotStore
from options_trader.data.dolthub import (
    DoltHubImporter, build_spot_lookup, rows_to_snapshots,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "IWM"])
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--max-dte", type=int, default=10,
                    help="Only import expirations within this many days of scan date")
    ap.add_argument("--out", default="data_snapshots_dolthub",
                    help="Snapshot root (kept separate from live collection)")
    args = ap.parse_args()

    importer = DoltHubImporter()
    store = SnapshotStore(args.out)
    print(f"Building spot lookup from yfinance for {args.symbols} "
          f"{args.start}..{args.end}")
    spots = build_spot_lookup(args.symbols, args.start, args.end)

    total = 0
    for symbol in args.symbols:
        days = importer.available_dates(symbol, args.start, args.end)
        print(f"{symbol}: {len(days)} scan days on DoltHub in range")
        for i, day in enumerate(days):
            rows = importer.fetch_day(symbol, day, max_dte=args.max_dte)
            snaps = rows_to_snapshots(rows, spots)
            for snap in snaps:
                store.save(snap)
            total += len(snaps)
            if (i + 1) % 20 == 0:
                print(f"  {symbol}: {i + 1}/{len(days)} days imported...")

    print(f"\nDone: {total} snapshots written to {args.out}/")
    print("Next: python scripts/backtest.py "
          f"--snapshots-dir {args.out} --config configs/dolthub_backtest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
