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
from options_trader.journal.journal import CREDIT_STRATEGIES


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
    # Every credit-like book, not just 'credit'. This is an ACCOUNT view:
    # scan_income.py opens real paper capital under the 'income' tag, and a
    # digest that quietly showed one book would understate exposure by
    # whatever the other book happens to be carrying. Management stays
    # per-book (manage_credit.py / manage_income.py); only the reporting is
    # account-wide.
    open_ = journal.open_credit_positions(strategy=None)
    total_risk = sum(r.max_loss for r in open_)
    print(f"\nOPEN POSITIONS ({len(open_)})  total max loss ${total_risk:,.0f}")
    if not open_:
        print("  (none)")
    for r in open_:
        exp_date = date.fromisoformat(r.expiration)
        dte_left = (exp_date - today).days
        # entry_debit holds the credit received for every book in
        # CREDIT_STRATEGIES (that is what the set is for).
        credit = r.entry_debit
        target = credit * 0.50  # 50% profit take
        # captured = credit - current_cost (we don't have live mark here)
        # Show static info; manage_credit.py logs the live mark
        print(f"  #{r.id} [{r.strategy}] {r.kind:20s} exp {r.expiration} "
              f"({dte_left} DTE left)")
        print(f"       entry credit ${credit:.2f}  target ${target:.2f}  "
              f"max loss ${r.max_loss:.0f}")

    # --- Closed trade stats, per book ---
    # Deliberately NOT pooled. The books run different variant registries
    # and, on a non-index underlying, different geometry; one blended
    # win-rate would describe no strategy that was actually traded. The
    # 30-trade gate is per book for the same reason.
    print("\nCLOSED TRADES")
    any_closed = False
    for book in sorted(CREDIT_STRATEGIES):
        stats = journal.stats(strategy=book)
        closed = stats.get("closed_trades", 0)
        print(f"\n  [{book}] {closed} closed")
        if closed <= 0:
            print("    (none yet — need 30 to evaluate edge)")
            continue
        any_closed = True
        for k, v in stats.items():
            if k != "closed_trades":
                print(f"    {k}: {v}")
        print(f"    Progress to 30-trade gate: {_bar(closed, 30)} ({closed}/30)")
    if not any_closed:
        print("\n  (no closed trades in any book yet)")

    print(f"\n{'='*60}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
