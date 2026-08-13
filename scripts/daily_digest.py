#!/usr/bin/env python3
"""Daily paper-trading digest — prints open positions, P&L vs target, and
closed-trade stats. Designed to be run from cron and appended to a log file.

    python scripts/daily_digest.py
    python scripts/daily_digest.py --journal journal.db
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.journal import Journal


def _bar(captured: float, target: float, width: int = 20) -> str:
    if target <= 0:
        return "[" + "?" * width + "]"
    frac = max(0.0, min(1.0, captured / target))
    filled = int(frac * width)
    return "[" + "=" * filled + "-" * (width - filled) + f"] {frac*100:.0f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="journal.db")
    args = ap.parse_args()

    journal = Journal(args.journal)
    today = date.today()
    now = datetime.now().isoformat(timespec="seconds")

    print(f"\n{'='*60}")
    print(f"PAPER TRADING DIGEST  {today}  ({now})")
    print(f"{'='*60}")

    # --- Open positions ---
    open_ = journal.open_credit_positions()
    print(f"\nOPEN POSITIONS ({len(open_)})")
    if not open_:
        print("  (none)")
    for r in open_:
        exp_date = date.fromisoformat(r.expiration)
        dte_left = (exp_date - today).days
        credit = r.entry_debit   # entry_debit holds the credit for strategy='credit' rows
        target = credit * 0.50  # 50% profit take
        # captured = credit - current_cost (we don't have live mark here)
        # Show static info; manage_credit.py logs the live mark
        print(f"  #{r.id} {r.kind:20s} exp {r.expiration} ({dte_left} DTE left)")
        print(f"       entry credit ${credit:.2f}  target ${target:.2f}  "
              f"max loss ${r.max_loss:.0f}")

    # --- Closed trade stats ---
    stats = journal.stats(strategy="credit")
    closed = stats.get("closed_trades", 0)
    print(f"\nCLOSED TRADES: {closed}")
    if closed > 0:
        for k, v in stats.items():
            if k != "closed_trades":
                print(f"  {k}: {v}")
        print(f"\n  Progress to 30-trade gate: {_bar(closed, 30)} ({closed}/30)")
    else:
        print("  (no closed trades yet — need 30 to evaluate edge)")

    print(f"\n{'='*60}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
