#!/usr/bin/env python3
"""Replay the pre-emptive vol-regime gate (risk/vol_regime.py) day-by-day
over real historical VIX data, to check it actually would have fired
during known crashes — not just the calm 2026-04-07..08-15 window it was
originally calibrated against.

    python scripts/backtest_vol_gate.py --start 2020-01-01 --end 2020-04-30
    python scripts/backtest_vol_gate.py --start 2022-01-01 --end 2022-10-31

For each trading day, calls the real check_vol_regime() with only the
trailing vix_spike_lookback_days of history available as of that day (no
lookahead) — an honest simulation of what the gate would have told
scan_credit.py/paper_trade.py on that morning.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.risk.vol_regime import check_vol_regime


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--vix-entry-ceiling", type=float, default=30.0)
    ap.add_argument("--vix-spike-pct", type=float, default=0.30)
    ap.add_argument("--vix-spike-lookback-days", type=int, default=5)
    args = ap.parse_args()

    import yfinance as yf
    # Pull extra lead-in so the first days in [start, end] have a real
    # trailing lookback window instead of being starved of history.
    from datetime import date, timedelta
    fetch_start = (date.fromisoformat(args.start) - timedelta(days=20)).isoformat()
    hist = yf.Ticker("^VIX").history(start=fetch_start, end=args.end, auto_adjust=False)
    closes = hist["Close"].dropna()
    if closes.empty:
        print("No VIX data returned.", file=sys.stderr)
        return 1
    dates = [ts.strftime("%Y-%m-%d") for ts in closes.index]

    rows = []
    for i, d in enumerate(dates):
        if d < args.start:
            continue
        lo = max(0, i - args.vix_spike_lookback_days)
        window = closes.iloc[lo:i + 1]
        check = check_vol_regime(
            args.vix_entry_ceiling, args.vix_spike_pct,
            args.vix_spike_lookback_days, vix_closes=window,
        )
        rows.append((d, check.vix_level, check.vix_pct_change,
                    check.allowed, check.reason))

    blocked = [r for r in rows if not r[3]]
    allowed = [r for r in rows if r[3]]
    print(f"{args.start}..{args.end}: {len(rows)} trading days, "
          f"{len(blocked)} blocked ({len(blocked) / len(rows):.1%}), "
          f"{len(allowed)} allowed")

    if blocked:
        first = blocked[0]
        print(f"\nFirst block: {first[0]}  VIX={first[1]:.2f}  "
              f"5d_chg={first[2]:+.1%}  reason: {first[4]}")
        level_blocks = [r for r in blocked if r[4] and "level" in r[4]]
        spike_blocks = [r for r in blocked if r[4] and "spiked" in r[4]]
        print(f"Total blocks: {len(blocked)} "
              f"(level-triggered: {len(level_blocks)}, "
              f"spike-triggered: {len(spike_blocks)})")
        if spike_blocks:
            first_spike = spike_blocks[0]
            print(f"First SPIKE-triggered block: {first_spike[0]}  "
                  f"VIX={first_spike[1]:.2f}  5d_chg={first_spike[2]:+.1%}")
        if level_blocks:
            first_level = level_blocks[0]
            print(f"First LEVEL-triggered block: {first_level[0]}  "
                  f"VIX={first_level[1]:.2f}")
            if spike_blocks and spike_blocks[0][0] < first_level[0]:
                from datetime import date as date_cls
                lead_days = (date_cls.fromisoformat(first_level[0])
                            - date_cls.fromisoformat(spike_blocks[0][0])).days
                print(f"-> spike trigger fired {lead_days} calendar days "
                      f"BEFORE the level trigger would have")
    else:
        print("\nNever blocked in this window.")

    print("\nDay-by-day (first 15 rows shown after any transition, "
          "full detail via --verbose not implemented — see script output above)")
    # Print every transition (allowed<->blocked) for a readable timeline.
    prev = None
    for d, level, chg, ok, reason in rows:
        if ok != prev:
            state = "ALLOW" if ok else "BLOCK"
            extra = f"  ({reason})" if reason else ""
            print(f"  {d}  VIX={level:6.2f}  5d_chg={chg:+7.1%}  -> {state}{extra}")
        prev = ok
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
