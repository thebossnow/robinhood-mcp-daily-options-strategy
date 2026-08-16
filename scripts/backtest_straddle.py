#!/usr/bin/env python3
"""Replay stored chain snapshots to expiry settlement for long straddles/
strangles — a volatility bet, not a directional or premium-selling one
(see options_trader/signals/straddle.py for the strategy and why it has no
BS-EV filter).

    python scripts/backtest_straddle.py --snapshots-dir data_snapshots_uw
    python scripts/backtest_straddle.py --snapshots-dir data_snapshots_uw \
        --strangle-width-frac 0.02   # OTM strangle instead of ATM straddle

Settlement closes are fetched from yfinance. Trades whose expiry hasn't
occurred yet are reported as skipped, never guessed.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.backtest import StraddleBacktestEngine
from options_trader.data import SnapshotStore, YFinanceProvider
from options_trader.signals.straddle import StraddleVariantConfig


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots-dir", default="data_snapshots_uw")
    ap.add_argument("--strangle-width-frac", type=float, default=0.0,
                    help="0 = ATM straddle; >0 = OTM strangle (fraction of spot)")
    ap.add_argument("--min-dte", type=int, default=5)
    ap.add_argument("--max-dte", type=int, default=14)
    ap.add_argument("--target-dte", type=int, default=7)
    ap.add_argument("--save-trades", metavar="PATH",
                    help="Write settled trades + summary to a JSON file")
    args = ap.parse_args()

    cfg = StraddleVariantConfig(
        name="straddle" if args.strangle_width_frac == 0.0 else "strangle",
        strangle_width_frac=args.strangle_width_frac,
        min_dte=args.min_dte, max_dte=args.max_dte, target_dte=args.target_dte,
    )
    snaps = SnapshotStore(args.snapshots_dir).load_all()
    if not snaps:
        print(f"No snapshots in {args.snapshots_dir}/.")
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

    result = StraddleBacktestEngine(cfg).run(snaps, settlements)
    print(f"\nBacktest summary ({cfg.name}, entry mid+slippage, hold to expiry):")
    for k, v in result.summary.items():
        print(f"  {k}: {v}")
    if result.trades:
        print("\nPer-trade results:")
        for t in result.trades:
            print(f"  {t['scan_date']} {t['underlying']} {t['kind']} "
                  f"C{t['call_strike']:g}/P{t['put_strike']:g} exp {t['expiration']}: "
                  f"entry {t['entry_debit']:.2f} → settle {t['settle_value']:.2f} "
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
        print(f"\nTrades written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
