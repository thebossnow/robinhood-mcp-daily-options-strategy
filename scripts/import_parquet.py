#!/usr/bin/env python3
"""Import the philippdubach historical options parquet into snapshot storage.

Fastest historical backtest path — one download per ticker, full liquidity
columns (volume + open interest), so the standard config applies unchanged:

    python scripts/import_parquet.py --tickers SPY QQQ IWM \
        --start 2024-01-01 --end 2026-06-30
    python scripts/backtest.py --snapshots-dir data_snapshots_parquet

Files are downloaded to parquet_cache/ on first run (or place them there
manually; see options_trader/data/parquet_import.py for URLs).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.data import SnapshotStore
from options_trader.data.parquet_import import (
    download_urls, fetch_to, frame_to_snapshots, load_options, load_underlying,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="+", default=["SPY", "QQQ", "IWM"])
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--max-dte", type=int, default=10)
    ap.add_argument("--cache-dir", default="parquet_cache")
    ap.add_argument("--out", default="data_snapshots_parquet")
    args = ap.parse_args()

    cache = Path(args.cache_dir)
    store = SnapshotStore(args.out)
    total = 0
    settlements: dict[str, float] = {}
    for ticker in args.tickers:
        opt_url, und_url = download_urls(ticker)
        opt_path = cache / ticker / "options.parquet"
        und_path = cache / ticker / "underlying.parquet"
        fetch_to(opt_path, opt_url)
        fetch_to(und_path, und_url)

        print(f"{ticker}: loading {args.start}..{args.end} (DTE<={args.max_dte})")
        options = load_options(opt_path, args.start, args.end)
        spots = load_underlying(und_path)
        snaps = frame_to_snapshots(options, spots, ticker, args.max_dte)
        for snap in snaps:
            store.save(snap)
            # Settlement = the underlying's close on expiration day, from
            # the same dataset — no yfinance calls needed at backtest time.
            close = spots.get(snap.expiration)
            if close is not None:
                settlements[f"{ticker}|{snap.expiration}"] = float(close)
        total += len(snaps)
        print(f"{ticker}: {len(snaps)} snapshots written")

    settle_path = Path(args.out) / "settlements.json"
    settle_path.write_text(json.dumps(settlements, indent=2, sort_keys=True))
    print(f"\nDone: {total} snapshots in {args.out}/ "
          f"(+{len(settlements)} settlement closes)")
    print(f"Next: python scripts/backtest.py --snapshots-dir {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
