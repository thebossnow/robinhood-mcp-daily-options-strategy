#!/usr/bin/env python3
"""Sweep short_put_delta x DTE window for the put-only credit spread
structure — the strongest, most repeated positive signal found across every
backtest in this repo (spy_put10, put_spread_15: positive on SPY/QQQ/IWM in
both the original DoltHub sweep and the fresh UW decomposition). Existing
configs only ever tested deltas {0.10, 0.15, 0.20, 0.30} and DTE windows
{5-14, 25-50} — this fills the gaps: a finer delta grid and the untested
14-25 and 50-90 DTE buckets.

    python scripts/sweep_put_credit.py --symbols SPY QQQ IWM \
        --start 2026-04-07 --end 2026-08-15

Reuses UWHistory's on-disk cache (data_uw_cache/) directly rather than
going through backtest_credit.py's --variants CLI, because that CLI's
ManagedBacktestEngine._enter() shares one DTE window across every variant
passed in a single call (issue #20) — fine within one DTE bucket (all
variants here share it by construction) but wrong across buckets, so each
bucket gets its own engine.run() call.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.backtest.managed import ManagedBacktestEngine, weekly_entry_days
from options_trader.data.unusual_whales import UWHistory, build_spot_lookup, load_api_key
from options_trader.signals.credit import CreditVariantConfig

DELTAS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
DTE_BUCKETS = [
    ("05-14", 5, 14, 7),
    ("14-25", 14, 25, 20),
    ("25-50", 25, 50, 40),
    ("50-90", 50, 90, 65),   # tail may include early-settled entries near the
                              # data window's tail — see printed caveat below
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "IWM"])
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--wing-width-frac", type=float, default=0.03)
    ap.add_argument("--cache-dir", default="data_uw_cache")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    api_key = load_api_key("UW_API_KEY")
    if not api_key:
        print("UW_API_KEY not set (env or .env) — aborting.", file=sys.stderr)
        return 1
    history = UWHistory(api_key, cache_dir=args.cache_dir)

    spot_end = (date.fromisoformat(args.end) + timedelta(days=95)).isoformat()
    print(f"Building spot lookup from yfinance: {args.symbols} {args.start}..{args.end}")
    spots = build_spot_lookup(args.symbols, args.start, spot_end)

    checkpoint_days: set[str] = set()
    for s in args.symbols:
        sym_days = sorted(d for (sym, d) in spots if sym == s)
        checkpoint_days.update(weekly_entry_days(sym_days))

    rows: list[dict] = []
    for label, min_dte, max_dte, target in DTE_BUCKETS:
        variants = [
            CreditVariantConfig(
                name=f"put_{int(round(d * 100)):02d}d_{label}",
                short_put_delta=d, short_call_delta=None,
                wing_width_frac=args.wing_width_frac, min_credit_frac=0.01,
                exit_on_breach=False, min_dte=min_dte, max_dte=max_dte,
                target_dte=target,
            )
            for d in DELTAS
        ]
        fetch_dte = max_dte + 5   # engine's own default (50) is too narrow
                                   # for the 50-90 bucket — override per run
        print(f"\n[{label} DTE] prefetching "
              f"{len(args.symbols) * len(checkpoint_days)} symbol-week "
              f"day-chains (cached ones are free)...")
        history.prefetch(args.symbols, sorted(checkpoint_days),
                         max_dte=fetch_dte, workers=args.workers)

        engine = ManagedBacktestEngine(history, spots)
        engine.MAX_DTE_FETCH = fetch_dte
        result = engine.run(args.symbols, args.start, args.end, variants,
                            progress=False)
        for v in variants:
            s = result.summary(variant=v.name)
            rows.append({"dte_bucket": label, "delta": v.short_put_delta, **s})

    print(f"\n{'DTE':<8}{'delta':>7}{'trades':>8}{'win%':>7}{'exp/trade':>11}"
          f"{'total':>10}{'PF':>8}{'maxDD':>10}")
    for r in rows:
        if r["trades"] == 0:
            print(f"{r['dte_bucket']:<8}{r['delta']:>7.2f}{r['trades']:>8}   (no trades)")
            continue
        pf = r["profit_factor"]
        pf_s = f"{pf:.2f}" if pf != float("inf") else "inf"
        print(f"{r['dte_bucket']:<8}{r['delta']:>7.2f}{r['trades']:>8}"
              f"{r['win_rate'] * 100:>6.1f}%{r['expectancy_per_trade']:>11.2f}"
              f"{r['total_pnl']:>10.2f}{pf_s:>8}{r['max_drawdown']:>10.2f}")

    print("\nCaveat on 50-90 DTE: entries near the tail of the data window "
          "(late June onward) may be early-settled at the last available "
          "day's spot rather than true expiry — data only extends to "
          f"{args.end}, and 50-90 DTE positions entered late need forward "
          "chain data past that to settle properly. Read that bucket's "
          "numbers as provisional, weighted toward early-window entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
