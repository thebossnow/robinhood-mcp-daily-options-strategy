#!/usr/bin/env python3
"""Replay stored chain snapshots to expiry settlement.

Snapshots accumulate from daily `scan.py --save-snapshot` runs. Settlement
closes are fetched from yfinance. Trades whose expiry hasn't occurred yet
are reported as skipped, never guessed.

    python scripts/backtest.py
    python scripts/backtest.py --per-snapshot-trades 2 --config my.json

Pass --save-trades to write the settled trades to JSON for downstream
analysis (scripts/calibrate.py reads that file).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.backtest import BacktestEngine
from options_trader.config import StrategyConfig
from options_trader.data import SnapshotStore, YFinanceProvider


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="Path to StrategyConfig JSON")
    ap.add_argument("--snapshots-dir", default="data_snapshots")
    ap.add_argument("--per-snapshot-trades", type=int, default=1)
    ap.add_argument("--save-trades", metavar="PATH",
                    help="Write settled trades + summary to a JSON file "
                         "(input for scripts/calibrate.py)")
    args = ap.parse_args()

    cfg = StrategyConfig.from_json(args.config) if args.config else StrategyConfig()
    snaps = SnapshotStore(args.snapshots_dir).load_all()
    if not snaps:
        print(f"No snapshots in {args.snapshots_dir}/ — run "
              "`python scripts/scan.py --save-snapshot` daily to collect them.")
        return 1
    print(f"Loaded {len(snaps)} snapshots.")

    provider = YFinanceProvider()
    settlements: dict[tuple[str, str], float] = {}
    for snap in snaps:
        key = (snap.underlying, snap.expiration)
        if key in settlements:
            continue
        px = provider.get_settlement_close(snap.underlying, snap.expiration)
        if px is not None:
            settlements[key] = px

    result = BacktestEngine(cfg).run(
        snaps, settlements, per_snapshot_trades=args.per_snapshot_trades
    )
    print("\nBacktest summary (entry mid+slippage, hold to expiry):")
    for k, v in result.summary.items():
        print(f"  {k}: {v}")
    if result.trades:
        print("\nPer-trade results:")
        for t in result.trades:
            print(f"  {t['scan_date']} {t['underlying']} {t['kind']} "
                  f"{t['long_strike']:g}/{t['short_strike']:g} exp {t['expiration']}: "
                  f"entry {t['entry_debit']:.2f} → exit {t['exit_value']:.2f} "
                  f"(P&L ${t['pnl']:.2f})")

    if args.save_trades:
        out_path = Path(args.save_trades)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "snapshots_dir": args.snapshots_dir,
            "config": asdict(cfg),
            "summary": result.summary,
            "trades": result.trades,
        }, indent=2))
        print(f"\nTrades written to {out_path} — analyze with "
              f"`python scripts/calibrate.py {out_path}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
