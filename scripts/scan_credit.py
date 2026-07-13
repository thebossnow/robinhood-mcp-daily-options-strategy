#!/usr/bin/env python3
"""Weekly credit-strategy paper entries (validated variants, SPY only).

    python scripts/scan_credit.py --provider mcp

Opens one paper position per VALIDATED variant (spy_condor15, spy_put10)
using live chains, on Mondays (pass --force to override the weekday check).
Every gate failure is journaled as NO QUALIFYING TRADE — a valid outcome.

Sizing note: configs/credit_paper.json runs a NOTIONAL $50k account chosen
so the validated book fits the RiskManager limits. The paper phase answers
"does the edge survive live fills?"; capitalization is a separate, later
decision. Per-trade risk here (~$1.2-2.4k max loss per contract) does NOT
fit a $5k account — do not point this config at real money.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.config import StrategyConfig
from options_trader.execution.paper import PaperBroker
from options_trader.journal import Journal
from options_trader.signals.credit import (
    VALIDATED, build_position, leg_passes_live_liquidity,
)

TARGET_DTE = 45


def pick_expiration(expirations: list[str], today: date,
                    min_dte: int, max_dte: int) -> str | None:
    def dte(e: str) -> int:
        return (date.fromisoformat(e) - today).days
    ok = [e for e in expirations if min_dte <= dte(e) <= max_dte]
    if not ok:
        return None
    return min(ok, key=lambda e: abs(dte(e) - TARGET_DTE))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/credit_paper.json")
    ap.add_argument("--provider", choices=["mcp", "yfinance"],
                    default="yfinance")
    ap.add_argument("--journal", default="journal.db")
    ap.add_argument("--force", action="store_true",
                    help="Enter even if today isn't Monday")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print candidates without journaling anything")
    args = ap.parse_args()

    today = date.today()
    if today.weekday() != 0 and not args.force:
        print(f"{today} is not a Monday — no entries (use --force to override)")
        return 0

    cfg = StrategyConfig.from_json(args.config)
    if args.provider == "mcp":
        from options_trader.data.mcp_provider import MCPDataProvider
        provider = MCPDataProvider()
    else:
        from options_trader.data.provider import YFinanceProvider
        provider = YFinanceProvider()

    journal = Journal(args.journal)
    broker = PaperBroker(cfg, journal)
    underlying = cfg.underlyings[0]

    expirations = provider.get_expirations(underlying)
    expiration = pick_expiration(expirations, today, cfg.min_dte, cfg.max_dte)
    if expiration is None:
        msg = (f"credit scan: no expiration in {cfg.min_dte}-{cfg.max_dte} "
               f"DTE for {underlying}")
        print(msg)
        if not args.dry_run:
            journal.log_no_trade(msg)
        return 0

    snap = provider.get_chain(underlying, expiration)
    liquid = snap.chain[snap.chain.apply(leg_passes_live_liquidity, axis=1)]
    print(f"{underlying} {expiration} ({snap.dte} DTE): spot {snap.spot:.2f}, "
          f"{len(liquid)}/{len(snap.chain)} contracts pass liquidity")

    # Idempotence: a cron double-fire or manual re-run must not stack a
    # second copy of the same weekly entry.
    open_kinds = {(r.kind, r.expiration)
                  for r in journal.open_credit_positions()}

    for name, vcfg in VALIDATED.items():
        if (name, expiration) in open_kinds:
            print(f"{name}: already open for {expiration} — skipped")
            continue
        pos = build_position(liquid, snap.spot, underlying,
                             today.isoformat(), expiration, snap.dte, vcfg,
                             cfg.slippage_half_spread_frac)
        if pos is None:
            msg = f"{name}: NO QUALIFYING TRADE (gates failed on live chain)"
            print(msg)
            if not args.dry_run:
                journal.log_no_trade(msg)
            continue

        desc = (f"{name}: credit {pos.credit:.2f} (frac {pos.credit_frac:.3f}), "
                f"max loss {pos.max_loss * 100:.0f}/contract, legs "
                + ", ".join(f"{'-' if l.side < 0 else '+'}{l.strike:g}{l.type[0].upper()}"
                            for l in pos.legs))
        if args.dry_run:
            print(f"[dry-run] {desc}")
            continue
        trade_id, check = broker.open_credit(pos, contracts=1,
                                             notes=f"paper entry {name}")
        if trade_id is None:
            msg = f"{name}: risk manager refused — {'; '.join(check.reasons)}"
            print(msg)
            journal.log_no_trade(msg)
        else:
            print(f"OPENED #{trade_id} {desc}")

    print(f"\n{datetime.now().isoformat(timespec='seconds')} scan complete. "
          f"Journal stats: {journal.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
