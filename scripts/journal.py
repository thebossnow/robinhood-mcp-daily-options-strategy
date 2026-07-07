#!/usr/bin/env python3
"""Trade journal: log every recommendation (including NO TRADE days),
record exits, and report win rate / expectancy.

The journal is the strategy's scoreboard. Until it shows positive
expectancy over 30+ logged trades, treat every parameter as unproven.

Usage:
  python scripts/journal.py log --ticker SPY --expiration 2026-07-10 \
      --type call --strike 625 --contracts 1 --entry 0.85 \
      --target 1.70 --stop 0.43 --thesis "held support, IV low"
  python scripts/journal.py log --no-trade --thesis "FOMC day, skipped"
  python scripts/journal.py close --id 3 --exit 1.65 --reason target
  python scripts/journal.py stats
"""

import argparse
import csv
import os
from datetime import datetime, timezone

JOURNAL_PATH = os.path.join(os.path.dirname(__file__), "..", "journal", "trades.csv")
FIELDS = ["id", "logged_at", "status", "ticker", "expiration", "type", "strike",
          "contracts", "entry", "target", "stop", "thesis",
          "closed_at", "exit", "exit_reason", "pnl"]


def _read_rows():
    if not os.path.exists(JOURNAL_PATH):
        return []
    with open(JOURNAL_PATH, newline="") as f:
        return list(csv.DictReader(f))


def _write_rows(rows):
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    with open(JOURNAL_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def cmd_log(args):
    rows = _read_rows()
    row = {k: "" for k in FIELDS}
    row["id"] = str(max((int(r["id"]) for r in rows), default=0) + 1)
    row["logged_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row["thesis"] = args.thesis
    if args.no_trade:
        row["status"] = "no_trade"
    else:
        for field in ("ticker", "expiration", "type", "strike", "contracts",
                      "entry", "target", "stop"):
            value = getattr(args, field)
            if value is None:
                raise SystemExit(f"--{field} is required unless --no-trade")
            row[field] = str(value)
        row["status"] = "open"
    rows.append(row)
    _write_rows(rows)
    print(f"Logged #{row['id']} ({row['status']})")


def cmd_close(args):
    rows = _read_rows()
    for row in rows:
        if row["id"] == str(args.id):
            if row["status"] != "open":
                raise SystemExit(f"#{args.id} is {row['status']}, not open")
            row["status"] = "closed"
            row["closed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            row["exit"] = str(args.exit)
            row["exit_reason"] = args.reason
            pnl = (args.exit - float(row["entry"])) * 100 * int(row["contracts"])
            row["pnl"] = f"{pnl:.2f}"
            _write_rows(rows)
            print(f"Closed #{args.id}: P&L ${pnl:.2f} ({args.reason})")
            return
    raise SystemExit(f"No trade with id {args.id}")


def cmd_stats(_args):
    rows = _read_rows()
    closed = [r for r in rows if r["status"] == "closed"]
    open_ = [r for r in rows if r["status"] == "open"]
    no_trade = [r for r in rows if r["status"] == "no_trade"]
    print(f"Entries: {len(rows)}  (closed {len(closed)}, open {len(open_)}, "
          f"no-trade days {len(no_trade)})")
    if not closed:
        print("No closed trades yet — no expectancy to report.")
        return
    pnls = [float(r["pnl"]) for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    expectancy = total / len(pnls)
    print(f"Win rate: {len(wins)}/{len(pnls)} = {len(wins)/len(pnls):.0%}")
    print(f"Avg win:  ${sum(wins)/len(wins):.2f}" if wins else "Avg win:  n/a")
    print(f"Avg loss: ${sum(losses)/len(losses):.2f}" if losses else "Avg loss: n/a")
    print(f"Total P&L: ${total:.2f}   Expectancy: ${expectancy:.2f}/trade")
    if len(pnls) < 30:
        print(f"NOTE: only {len(pnls)} closed trades — expectancy is not yet "
              "statistically meaningful (target 30+).")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_log = sub.add_parser("log", help="Log a new trade or a no-trade day")
    p_log.add_argument("--no-trade", action="store_true")
    p_log.add_argument("--ticker")
    p_log.add_argument("--expiration")
    p_log.add_argument("--type", choices=["call", "put"])
    p_log.add_argument("--strike", type=float)
    p_log.add_argument("--contracts", type=int)
    p_log.add_argument("--entry", type=float)
    p_log.add_argument("--target", type=float)
    p_log.add_argument("--stop", type=float)
    p_log.add_argument("--thesis", required=True)
    p_log.set_defaults(func=cmd_log)

    p_close = sub.add_parser("close", help="Record an exit for an open trade")
    p_close.add_argument("--id", type=int, required=True)
    p_close.add_argument("--exit", type=float, required=True)
    p_close.add_argument("--reason", required=True,
                         choices=["target", "stop", "time", "manual"])
    p_close.set_defaults(func=cmd_close)

    p_stats = sub.add_parser("stats", help="Win rate and expectancy report")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
