#!/usr/bin/env python3
"""Import historical EOD option chains from Unusual Whales into snapshot
storage.

    export UW_API_KEY=...   # or put it in a .env this script loads
    python scripts/import_unusual_whales.py --symbols SPY QQQ IWM \
        --start 2026-04-07 --end 2026-08-14

Writes to data_snapshots_uw/ (separate from live collection and from the
DoltHub/EODHD imports). Since this dataset carries real volume/open_interest
and greeks, the strategy's normal liquidity filters apply — no override
config needed:

    python scripts/backtest.py --snapshots-dir data_snapshots_uw

Free-trial keys get a rolling ~90-trading-day historical access window;
days older than that are skipped with a warning rather than aborting the
run (see HistoricalAccessError in options_trader/data/unusual_whales.py).
Read README.md ("Unusual Whales historical backtest") for how this compares
to the DoltHub and EODHD imports.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.data import SnapshotStore
from options_trader.data.unusual_whales import (
    HistoricalAccessError, UWImporter, build_spot_lookup, load_api_key,
    rows_to_snapshots,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "IWM"])
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--max-dte", type=int, default=10,
                    help="Only import expirations within this many days of scan date")
    ap.add_argument("--out", default="data_snapshots_uw",
                    help="Snapshot root (kept separate from live collection)")
    args = ap.parse_args()

    api_key = load_api_key("UW_API_KEY")
    if not api_key:
        print("UW_API_KEY not set (env or .env) — aborting.", file=sys.stderr)
        return 1

    importer = UWImporter(api_key)
    store = SnapshotStore(args.out)
    print(f"Building spot lookup from yfinance for {args.symbols} "
          f"{args.start}..{args.end}")
    spots = build_spot_lookup(args.symbols, args.start, args.end)

    total = 0
    skipped_out_of_window = 0
    for symbol in args.symbols:
        days = sorted(d for (s, d) in spots if s == symbol)
        print(f"{symbol}: {len(days)} trading days in range (from yfinance)")
        for i, day in enumerate(days):
            try:
                rows = importer.fetch_day(symbol, day, max_dte=args.max_dte)
            except HistoricalAccessError as exc:
                skipped_out_of_window += 1
                if skipped_out_of_window == 1:
                    print(f"  {symbol} {day}: outside historical access window "
                          f"— {exc}")
                continue
            snaps = rows_to_snapshots(day, symbol, rows, spots)
            for snap in snaps:
                store.save(snap)
            total += len(snaps)
            if (i + 1) % 20 == 0:
                print(f"  {symbol}: {i + 1}/{len(days)} days imported...")

    print(f"\nDone: {total} snapshots written to {args.out}/"
          f" ({skipped_out_of_window} day(s) skipped, outside access window)")
    print(f"Next: python scripts/backtest.py --snapshots-dir {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
