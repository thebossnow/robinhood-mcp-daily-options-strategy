#!/usr/bin/env python3
"""Managed credit-strategy backtest over DoltHub EOD history.

    python scripts/backtest_credit.py --symbols SPY QQQ IWM XLF XLE \
        --start 2022-01-03 --end 2026-06-30

Runs the three shipped variants (put_spread, condor_sym, condor_asym) with
weekly entries, 30-45 DTE, managed at 50% profit / 21 DTE / short-strike
breach. Queries DoltHub lazily with an on-disk cache (data_dolthub_cache/),
so the first run over a long range takes a while and reruns are fast.

Writes per-trade CSV and a summary JSON to runs/, prints a summary table.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.backtest.managed import (
    ManagedBacktestEngine, weekly_entry_days,
)
from options_trader.data.dolthub import DoltHubHistory, build_spot_lookup
from options_trader.signals.credit import VALIDATED, VALIDATED_UNIVERSE, VARIANTS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=None,
                    help="Default: SPY for validated, SPY XLF XLE for legacy "
                         "(the DoltHub dataset has no QQQ/IWM)")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--variant-set", choices=["validated", "legacy"],
                    default="validated",
                    help="validated = the paper-trading candidates; "
                         "legacy = the original playbook parameters")
    ap.add_argument("--variants", nargs="+", default=None,
                    choices=list(VARIANTS) + list(VALIDATED))
    ap.add_argument("--cache-dir", default="data_dolthub_cache")
    ap.add_argument("--out-dir", default="runs")
    ap.add_argument("--workers", type=int, default=4,
                    help="Concurrent DoltHub fetches during prefetch")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    pool = VALIDATED if args.variant_set == "validated" else VARIANTS
    names = args.variants or list(pool)
    variants = [(VALIDATED | VARIANTS)[name] for name in names]
    if args.symbols is None:
        args.symbols = (list(VALIDATED_UNIVERSE)
                        if args.variant_set == "validated"
                        else ["SPY", "XLF", "XLE"])
    history = DoltHubHistory(cache_dir=args.cache_dir)
    print(f"Building spot lookup from yfinance: {args.symbols} "
          f"{args.start}..{args.end}")
    # Spots must extend past `end` so positions entered near the end of the
    # range can be marked/settled through expiration (~45 days later).
    from datetime import date, timedelta
    spot_end = (date.fromisoformat(args.end) + timedelta(days=60)).isoformat()
    spots = build_spot_lookup(args.symbols, args.start, spot_end)

    # Warm the day-chain cache concurrently: one fetch per symbol-week is
    # the entire API footprint of the backtest.
    checkpoint_days: set[str] = set()
    for s in args.symbols:
        sym_days = sorted(d for (sym, d) in spots if sym == s)
        checkpoint_days.update(weekly_entry_days(sym_days))
    print(f"Prefetching {len(args.symbols) * len(checkpoint_days)} "
          f"symbol-week day-chains from DoltHub (cached ones are free)...")
    history.prefetch(args.symbols, sorted(checkpoint_days),
                     workers=args.workers)

    engine = ManagedBacktestEngine(history, spots)
    result = engine.run(args.symbols, args.start, args.end, variants,
                        progress=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    trades_path = out_dir / f"credit_backtest_{stamp}_trades.csv"
    if result.trades:
        with trades_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=result.trades[0].to_dict().keys())
            writer.writeheader()
            for t in result.trades:
                writer.writerow(t.to_dict())

    summary = {
        "params": {
            "symbols": args.symbols, "start": args.start, "end": args.end,
            "variants": {v.name: v.to_dict() for v in variants},
        },
        "skips": {
            "no_expiration": result.skipped_no_expiration,
            "no_position": result.skipped_no_position,
            "no_data": result.skipped_no_data,
        },
        "by_variant": {v.name: result.summary(variant=v.name) for v in variants},
        "by_variant_symbol": {
            v.name: {s: result.summary(variant=v.name, underlying=s)
                     for s in args.symbols}
            for v in variants
        },
    }
    summary_path = out_dir / f"credit_backtest_{stamp}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    print(f"\n{'variant':<12} {'trades':>6} {'win%':>6} {'exp/trade':>10} "
          f"{'total':>10} {'PF':>6} {'maxDD':>9} {'worst':>9}")
    for v in variants:
        s = result.summary(variant=v.name)
        if s["trades"] == 0:
            print(f"{v.name:<12} {'0':>6}  (no trades)")
            continue
        print(f"{v.name:<12} {s['trades']:>6} {s['win_rate']*100:>5.1f}% "
              f"{s['expectancy_per_trade']:>10.2f} {s['total_pnl']:>10.2f} "
              f"{s['profit_factor']:>6.2f} {s['max_drawdown']:>9.2f} "
              f"{s['worst_trade']:>9.2f}")
    print(f"\nSkips: {summary['skips']}")
    print(f"Trades: {trades_path if result.trades else '(none)'}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
