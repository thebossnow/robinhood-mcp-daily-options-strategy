#!/usr/bin/env python3
"""Small-account options income backtest (options_income_backtest_spec.md)
over the repo's full spec ticker universe: F/AAL/GME on the free DoltHub
EOD source, SOFI/RIVN/SNAP/AMC/MARA/CLSK on Databento's OPRA feed (see
options_trader/data/databento.py -- DoltHub has no usable chain history for
those six), routed together by MultiSourceHistory.

    python scripts/backtest_income.py --start 2021-01-01 --end 2026-08-27

Runs S1 (wheel) on F/AAL/SOFI/RIVN, S2 (bull put) on the same four, and S3
(iron condor) on GME/SNAP/AMC/MARA/CLSK as one combined book (portfolio.py's
caps/drawdown-halt apply across all three), then per-sleeve. Run
scripts/prefetch_databento.py first to warm the Databento cache -- this
script does not re-issue that (paid) prefetch itself, only DoltHub's (free)
one; an unwarmed Databento ticker will fetch live, slowly and at cost, one
symbol-day at a time.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.backtest.portfolio import (
    PortfolioBacktestEngine, PortfolioConfig, Sleeve,
)
from options_trader.data.databento import DatabentoHistory, load_api_key
from options_trader.data.dolthub import DoltHubHistory, build_spot_lookup
from options_trader.data.multi_source import MultiSourceHistory
from options_trader.reporting.regime import (
    compute_sma50, fetch_vix_closes, group_by, split_walk_forward,
    summarize_pnls, tag_trades,
)
from options_trader.signals.small_account_spec import (
    DATABENTO_TICKERS, S1_TICKERS, S1_WHEEL_CORE, S2_BULL_PUT, S2_TICKERS,
    S3_IC_SATELLITE, S3_TICKERS,
)

# Spec's window: train 2021-2023, test 2024-present.
WALK_FORWARD_TRAIN_END = "2023-12-31"


def summarize(trades, equity_curve, starting_equity):
    if not trades:
        return {"trades": 0}
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    peak, max_dd = starting_equity, 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        max_dd = min(max_dd, (eq - peak) / peak if peak else 0.0)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "total_pnl": round(sum(pnls), 2),
        "expectancy_per_trade": round(sum(pnls) / len(trades), 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else float("inf"),
        "max_drawdown_pct": round(max_dd, 4),
        "worst_trade": round(min(pnls), 2),
        "best_trade": round(max(pnls), 2),
        "final_equity": equity_curve[-1][1] if equity_curve else starting_equity,
        "exit_reasons": reasons,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--starting-equity", type=float, default=10_000.0)
    ap.add_argument("--cache-dir", default="data_dolthub_cache")
    ap.add_argument("--databento-cache-dir", default="data_databento_cache")
    ap.add_argument("--out-dir", default="runs")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--profit-take", type=float, default=None,
                    help="Override every sleeve's profit-take fraction "
                         "(spec section 11's 40%%/50%%/70%% sensitivity "
                         "sweep). Default: each sleeve's own configured "
                         "value (0.50 everywhere as of this spec).")
    ap.add_argument("--sleeves", default="S1_WHEEL_CORE,S2_BULL_PUT,S3_IC_SATELLITE",
                    help="Comma-separated sleeve IDs to run as one book "
                         "(default: all three, the combined book). Pass a "
                         "single sleeve to get that strategy's OWN isolated "
                         "equity curve -- report_income.py's consistency "
                         "score needs each sleeve's own drawdown, which the "
                         "combined book's shared equity/drawdown-halt "
                         "can't isolate.")
    args = ap.parse_args()
    wanted_sleeves = set(args.sleeves.split(","))

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    all_tickers = sorted(set(S1_TICKERS) | set(S2_TICKERS) | set(S3_TICKERS))
    dolthub_tickers = sorted(set(all_tickers) - set(DATABENTO_TICKERS))
    spot_end = (date.fromisoformat(args.end) + timedelta(days=60)).isoformat()
    print(f"Building spot lookup from yfinance: {all_tickers} "
          f"{args.start}..{spot_end}")
    spots = build_spot_lookup(all_tickers, args.start, spot_end)

    dolthub_history = DoltHubHistory(cache_dir=args.cache_dir)
    databento_history = DatabentoHistory(
        api_key=load_api_key("DATABENTO_API_KEY"),
        cache_dir=args.databento_cache_dir, spot_lookup=spots)
    history = MultiSourceHistory(
        {t: databento_history for t in DATABENTO_TICKERS},
        default=dolthub_history)

    all_days = sorted({d for (s, d) in spots})
    print(f"Prefetching up to {len(dolthub_tickers) * len(all_days)} "
          f"DoltHub symbol-day chains (free, cached ones are already free)...")
    dolthub_history.prefetch(dolthub_tickers, all_days, workers=args.workers)
    print("Databento tickers use the cache warmed by "
          "scripts/prefetch_databento.py; any cache miss here fetches live "
          "at cost -- run that script first for a long backtest range.")

    wheel_cfg, s2_cfg, s3_cfg = S1_WHEEL_CORE, S2_BULL_PUT, S3_IC_SATELLITE
    if args.profit_take is not None:
        wheel_cfg = dataclasses.replace(
            wheel_cfg, put_profit_take_frac=args.profit_take,
            call_profit_take_frac=args.profit_take)
        s2_cfg = dataclasses.replace(s2_cfg, profit_take_frac=args.profit_take)
        s3_cfg = dataclasses.replace(s3_cfg, profit_take_frac=args.profit_take)

    cfg = PortfolioConfig(starting_equity=args.starting_equity)
    sleeves = [
        Sleeve(sleeve_id="S1_WHEEL_CORE", tickers=S1_TICKERS,
              is_satellite=False, kind="wheel", wheel_cfg=wheel_cfg),
        Sleeve(sleeve_id="S2_BULL_PUT", tickers=S2_TICKERS,
              is_satellite=False, kind="credit", credit_cfg=s2_cfg),
        Sleeve(sleeve_id="S3_IC_SATELLITE", tickers=S3_TICKERS,
              is_satellite=True, kind="credit", credit_cfg=s3_cfg),
    ]
    sleeves = [s for s in sleeves if s.sleeve_id in wanted_sleeves]
    if not sleeves:
        raise SystemExit(f"--sleeves matched none of {wanted_sleeves!r}")

    engine = PortfolioBacktestEngine(history, spots, cfg)
    result = engine.run(sleeves, args.start, args.end)

    # --- Regime tagging + walk-forward split (spec section 4 and 3) ---
    # SMA50 needs ~70 calendar days of lead-in before `start` to have a
    # full 50-trading-day window by the first entry.
    lookback_start = (date.fromisoformat(args.start)
                      - timedelta(days=90)).isoformat()
    print(f"Fetching VIX and trend history {lookback_start}..{args.end} "
          "for regime tagging...")
    vix_by_day = fetch_vix_closes(lookback_start, args.end)
    price_history = build_spot_lookup(all_tickers, lookback_start, args.end)
    sma50_by_ticker_day: dict[tuple[str, str], float] = {}
    close_by_ticker_day: dict[tuple[str, str], float] = dict(price_history)
    for ticker in all_tickers:
        closes = {d: c for (s, d), c in price_history.items() if s == ticker}
        for day, sma in compute_sma50(closes).items():
            sma50_by_ticker_day[(ticker, day)] = sma
    tagged = tag_trades(result.trades, vix_by_day, sma50_by_ticker_day,
                        close_by_ticker_day)
    by_vix = group_by(tagged, "vix_bucket")
    by_trend = group_by(tagged, "trend_bucket")

    train_trades, test_trades = split_walk_forward(
        result.trades, WALK_FORWARD_TRAIN_END)
    walk_forward = {
        "train_2021_2023": summarize_pnls([t.pnl for t in train_trades]),
        "test_2024_present": summarize_pnls([t.pnl for t in test_trades]),
    }
    walk_forward_per_sleeve = {}
    for sl in sleeves:
        tr, te = split_walk_forward(
            [t for t in result.trades if t.sleeve_id == sl.sleeve_id],
            WALK_FORWARD_TRAIN_END)
        walk_forward_per_sleeve[sl.sleeve_id] = {
            "train_2021_2023": summarize_pnls([t.pnl for t in tr]),
            "test_2024_present": summarize_pnls([t.pnl for t in te]),
        }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)
    stamp = f"{args.start}_{args.end}".replace("-", "")

    trades_path = out_dir / f"income_trades_{stamp}.csv"
    with trades_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sleeve_id", "underlying", "kind", "entry_date",
                   "exit_date", "expiration", "exit_reason", "contracts",
                   "pnl", "equity_after", "credit_per_share", "hard_stop_mult"])
        for t in result.trades:
            w.writerow([t.sleeve_id, t.underlying, t.kind, t.entry_date,
                       t.exit_date, t.expiration, t.exit_reason,
                       t.contracts, t.pnl, t.equity_after,
                       t.credit_per_share, t.hard_stop_mult])

    equity_path = out_dir / f"income_equity_{stamp}.csv"
    with equity_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "equity"])
        w.writerows(result.equity_curve)

    combined = summarize(result.trades, result.equity_curve, cfg.starting_equity)
    per_sleeve = {
        sl.sleeve_id: summarize(
            [t for t in result.trades if t.sleeve_id == sl.sleeve_id],
            result.equity_curve, cfg.starting_equity)
        for sl in sleeves
    }
    summary_path = out_dir / f"income_summary_{stamp}.json"
    summary_path.write_text(json.dumps({
        "combined": combined, "per_sleeve": per_sleeve,
        "walk_forward": walk_forward,
        "walk_forward_per_sleeve": walk_forward_per_sleeve,
        "by_vix_bucket": by_vix, "by_trend_bucket": by_trend,
    }, indent=2))

    print(f"\n{len(result.trades)} trades, "
          f"final equity ${result.equity_curve[-1][1]:,.2f}" if result.equity_curve
          else "\nNo trades.")
    print(json.dumps(combined, indent=2))
    print("\nPer sleeve:")
    print(json.dumps(per_sleeve, indent=2))
    print(f"\nWalk-forward (train <= {WALK_FORWARD_TRAIN_END} / test after):")
    print(json.dumps(walk_forward, indent=2))
    print("\nWalk-forward per sleeve:")
    print(json.dumps(walk_forward_per_sleeve, indent=2))
    print("\nBy VIX bucket:")
    print(json.dumps(by_vix, indent=2))
    print("\nBy trend bucket:")
    print(json.dumps(by_trend, indent=2))
    print(f"\nWrote {trades_path}, {equity_path}, {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
