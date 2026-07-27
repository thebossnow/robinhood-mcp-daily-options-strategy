#!/usr/bin/env python3
"""Slow loop auditing the fast loop: periodic check of live calibration.

Run this weekly (cron, or by hand) against the paper/live journal. If the
predicted-EV-vs-realized-P&L relationship has broken down over a large
enough sample, it writes loop/live_halt.json, and every subsequent
RiskManager.check() refuses new entries until a human reviews the data and
clears it explicitly — same "requires human review to resume" convention as
the consecutive-loss kill switch, just anchored to real fills instead of a
losing streak.

This is the audit node in the graph: a loop that watches another loop and
can veto it, anchored to Journal.closed_trades() (what settled in the
account) rather than to another backtest.

    python loop/audit_live.py                # check, halt if warranted
    python loop/audit_live.py --clear         # human review done, resume
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.backtest.calibration import MIN_TRADES_FOR_VERDICT, ev_calibration
from options_trader.journal import Journal, TradeRecord
from options_trader.journal.live_calibration import (
    LIVE_HALT_PATH, closed_trades_with_predicted_ev,
)


def check_drift(records: list[TradeRecord]) -> tuple[bool, dict]:
    """(should_halt, details). Halts only once there's enough live sample to
    trust a verdict — matching the data-readiness discipline in
    loop/evaluate.py, applied here to live fills instead of backtest days."""
    trades = closed_trades_with_predicted_ev(records)
    n = len(trades)
    if n < MIN_TRADES_FOR_VERDICT:
        return False, {
            "n": n,
            "verdict": f"only {n} live trades with a recorded prediction "
                       f"(< {MIN_TRADES_FOR_VERDICT}) — no verdict yet",
        }

    ev = ev_calibration(trades)
    slope = ev["ols_slope"]
    mean_pnl = ev["mean_realized_pnl"]
    reasons: list[str] = []
    if slope is not None and slope <= 0:
        reasons.append(
            f"EV-calibration slope {slope} <= 0 — predicted edge no longer "
            "predicts realized P&L on live fills"
        )
    if mean_pnl <= 0:
        reasons.append(
            f"mean realized P&L {mean_pnl:.2f}/trade <= 0 over {n} live trades"
        )
    return bool(reasons), {
        "n": n, "ols_slope": slope, "mean_realized_pnl": mean_pnl,
        "reasons": reasons,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", default="journal.db")
    ap.add_argument("--clear", action="store_true",
                    help="Clear an existing halt after human review")
    args = ap.parse_args()

    if args.clear:
        if LIVE_HALT_PATH.exists():
            LIVE_HALT_PATH.unlink()
            print(f"Cleared {LIVE_HALT_PATH} — trading may resume.")
        else:
            print("No active halt.")
        return 0

    journal = Journal(args.journal)
    halt, details = check_drift(journal.closed_trades())
    print(f"Live drift audit: {details}")

    if halt:
        LIVE_HALT_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIVE_HALT_PATH.write_text(json.dumps({
            "halted_at": datetime.now(timezone.utc).isoformat(),
            **details,
        }, indent=2))
        print(f"HALTED — wrote {LIVE_HALT_PATH}. New entries are refused "
              "until `python loop/audit_live.py --clear` after human review.")
        return 1

    if LIVE_HALT_PATH.exists():
        print(f"Note: {LIVE_HALT_PATH} exists from a prior halt but current "
              "data no longer triggers it — still requires --clear to "
              "resume (no silent auto-clear).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
