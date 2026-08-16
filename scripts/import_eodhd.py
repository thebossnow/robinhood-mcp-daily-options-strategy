#!/usr/bin/env python3
"""Import historical EOD option chains from EODHD into snapshot storage.

    export EODHD_API_KEY=...   # or put it in a .env this script loads
    python scripts/import_eodhd.py --symbols SPY QQQ IWM \
        --start 2024-01-01 --end 2026-06-30

Writes to data_snapshots_eodhd/ (separate from live collection and from the
DoltHub import). Then, since this dataset carries real volume/open_interest,
the strategy's normal liquidity filters apply — no override config needed:

    python scripts/backtest.py --snapshots-dir data_snapshots_eodhd

Requires an EODHD API key with the "US Stock Options" marketplace add-on
enabled — the base/free plan returns 403 on this endpoint. Each request
costs 10 API calls against your EODHD quota; watch --symbols/date range
against your plan's daily limit. Read README.md ("EODHD historical
backtest") for how this compares to the DoltHub import.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.data import SnapshotStore
from options_trader.data.eodhd import (
    EODHDImporter, build_spot_lookup, rows_to_snapshots,
)


def _load_api_key() -> str:
    key = os.environ.get("EODHD_API_KEY")
    if key:
        return key
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("EODHD_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "IWM"])
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--max-dte", type=int, default=10,
                    help="Only import expirations within this many days of scan date")
    ap.add_argument("--out", default="data_snapshots_eodhd",
                    help="Snapshot root (kept separate from live collection)")
    args = ap.parse_args()

    api_key = _load_api_key()
    if not api_key:
        print("EODHD_API_KEY not set (env or .env) — aborting.", file=sys.stderr)
        return 1

    importer = EODHDImporter(api_key)
    store = SnapshotStore(args.out)
    print(f"Building spot lookup from yfinance for {args.symbols} "
          f"{args.start}..{args.end}")
    spots = build_spot_lookup(args.symbols, args.start, args.end)

    total = 0
    for symbol in args.symbols:
        days = sorted(d for (s, d) in spots if s == symbol)
        print(f"{symbol}: {len(days)} trading days in range (from yfinance)")
        for i, day in enumerate(days):
            rows = importer.fetch_day(symbol, day, max_dte=args.max_dte)
            snaps = rows_to_snapshots(rows, spots)
            for snap in snaps:
                store.save(snap)
            total += len(snaps)
            if (i + 1) % 20 == 0:
                print(f"  {symbol}: {i + 1}/{len(days)} days imported...")

    print(f"\nDone: {total} snapshots written to {args.out}/")
    print(f"Next: python scripts/backtest.py --snapshots-dir {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
