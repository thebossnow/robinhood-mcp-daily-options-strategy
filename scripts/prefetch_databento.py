#!/usr/bin/env python3
"""Warm the Databento (OPRA) EOD-quote cache for the tickers DoltHub can't
serve (see options_trader/data/databento.py's module docstring: SOFI/RIVN/
SNAP/AMC/MARA/CLSK have zero or near-zero DoltHub coverage).

Only issues narrow per-(symbol, day) market-close windows (~$10 total for
all 6 tickers over 2021-2026, confirmed via metadata.get_cost sampling
before this was ever run) -- never widen the window in DatabentoHistory
without re-checking cost first, a naive full-day pull over this same range
was quoted at ~$570.

    python scripts/prefetch_databento.py --start 2021-01-01 --end 2026-08-29

Cached rows land in data_databento_cache/ (default), reused by
scripts/backtest_income.py once that script is updated to include these
tickers.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.data.databento import DatabentoHistory, load_api_key
from options_trader.data.dolthub import build_spot_lookup

GAP_TICKERS = ["SOFI", "RIVN", "SNAP", "AMC", "MARA", "CLSK"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--tickers", nargs="+", default=GAP_TICKERS)
    ap.add_argument("--cache-dir", default="data_databento_cache")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger(__name__)

    logger.info("Building spot lookup from yfinance: %s %s..%s",
               args.tickers, args.start, args.end)
    spots = build_spot_lookup(args.tickers, args.start, args.end)
    all_days = sorted({d for (_, d) in spots})
    logger.info("%d trading days found across %d tickers",
               len(all_days), len(args.tickers))

    history = DatabentoHistory(api_key=load_api_key("DATABENTO_API_KEY"),
                               cache_dir=args.cache_dir, spot_lookup=spots)
    logger.info("Prefetching up to %d symbol-day EOD quote windows "
               "(cached ones are free, no re-charge)...",
               len(args.tickers) * len(all_days))
    history.prefetch(args.tickers, all_days, workers=args.workers,
                     progress_every=100)
    logger.info("Done. Cache at %s", args.cache_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
