#!/usr/bin/env python3
"""Live calibration study: does predicted edge cash in real (paper/live) fills?

Reads closed trades from the journal and compares each trade's predicted
`ev_after_costs` at entry against what actually settled in the account. This
is the graph's real-fills anchor — the number the article on graph
engineering calls the one that can't be argued with. See
options_trader/journal/live_calibration.py for the caveats (credit-strategy
entries don't currently record a prediction, so they're excluded).

    python scripts/calibrate_live.py
    python scripts/calibrate_live.py --journal journal.db --bins 4

Pass --backtest-trades to also print the gap against a backtest's predicted
expectancy_per_trade (the JSON written by `scripts/backtest.py --save-trades`):

    python scripts/calibrate_live.py --backtest-trades runs/trades.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.journal import Journal
from options_trader.journal.live_calibration import live_gap_report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", default="journal.db")
    ap.add_argument("--bins", type=int, default=5)
    ap.add_argument("--backtest-trades", metavar="PATH",
                    help="JSON from scripts/backtest.py --save-trades, for "
                         "the backtest-vs-live expectancy gap")
    args = ap.parse_args()

    journal = Journal(args.journal)
    records = journal.closed_trades()
    if not records:
        print(f"No closed trades in {args.journal} yet.")
        return 1

    backtest_expectancy = None
    if args.backtest_trades:
        payload = json.loads(Path(args.backtest_trades).read_text())
        backtest_expectancy = payload["summary"].get("expectancy_per_trade")
        if backtest_expectancy is None:
            print(f"Warning: {args.backtest_trades} has no "
                  "expectancy_per_trade in its summary — skipping the gap.")

    print(live_gap_report(records, n_bins=args.bins,
                          backtest_expectancy=backtest_expectancy))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
