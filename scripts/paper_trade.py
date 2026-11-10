#!/usr/bin/env python3
"""Paper trading CLI on top of the journal + risk manager.

    open    — open a position from a scan file (risk-checked, slippage applied)
    close   — close at a given spread mid value
    settle  — settle expired positions at the underlying's expiry close
    status  — open positions and current risk state
    stats   — win rate, expectancy, drawdown across closed trades
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.config import StrategyConfig
from options_trader.data import SETTLEMENT_PROVIDERS
from options_trader.execution import PaperBroker
from options_trader.journal import Journal
from options_trader.risk.vol_regime import fetch_vix_closes
from options_trader.signals.candidates import SpreadCandidate


def _broker(args) -> PaperBroker:
    cfg = StrategyConfig.from_json(args.config) if args.config else StrategyConfig()
    return PaperBroker(
        cfg, Journal(args.journal),
        vix_provider=lambda: fetch_vix_closes(cfg.vix_spike_lookback_days),
    )


def cmd_open(args) -> int:
    broker = _broker(args)
    cands = json.loads(Path(args.scan_file).read_text())
    if not (0 <= args.index < len(cands)):
        print(f"index {args.index} out of range (scan has {len(cands)} candidates)")
        return 1
    cand = SpreadCandidate(**cands[args.index])
    trade_id, check = broker.open(cand, contracts=args.contracts)
    if trade_id is None:
        print("REFUSED by risk manager:")
        for r in check.reasons:
            print(f"  - {r}")
        return 1
    rec = broker.journal.get(trade_id)
    print(f"Opened paper trade #{trade_id}: {cand.order_description(rec.contracts)}")
    print(f"  filled at {rec.entry_debit:.2f} (mid {cand.debit:.2f} + slippage), "
          f"max loss ${rec.max_loss:.0f}, max profit ${rec.max_profit:.0f}")
    return 0


def cmd_close(args) -> int:
    broker = _broker(args)
    rec = broker.close(args.id, args.value)
    print(f"Closed #{rec.id} at {rec.exit_value:.2f}: P&L ${rec.realized_pnl:.2f}")
    return 0


def cmd_settle(args) -> int:
    broker = _broker(args)
    from datetime import date
    from options_trader.data import get_settlement_provider
    provider = get_settlement_provider(args.price_provider)
    today = date.today().isoformat()
    expired = [r for r in broker.journal.open_positions() if r.expiration < today]
    if not expired:
        print("No open positions past expiry.")
        return 0
    for rec in expired:
        px = provider.get_settlement_close(rec.underlying, rec.expiration)
        if px is None:
            print(f"#{rec.id}: no settlement close for {rec.underlying} "
                  f"{rec.expiration} yet — skipping")
            continue
        done = broker.settle_expired(rec.id, px)
        print(f"Settled #{done.id} ({done.underlying} {done.expiration}) at "
              f"{px:.2f}: P&L ${done.realized_pnl:.2f}")
    return 0


def cmd_status(args) -> int:
    """Risk state per book, then an account-wide total.

    The max-open-positions limit and the consecutive-loss kill switch are
    enforced per book (see RiskManager.check), so the status line reports
    them per book too. A single unscoped streak could read "3 consecutive
    losses" while the book about to trade had none. Open risk is shown per
    book and in total, since portfolio heat is checked account-wide.
    """
    broker = _broker(args)
    j = broker.journal
    cfg = broker.cfg
    books = j.books()
    if not books:
        print("No trades in the journal yet.")
    for book in books:
        n_open = len(j.open_positions(book))
        streak = j.consecutive_losses(book)
        flags = []
        if n_open >= cfg.max_open_positions:
            flags.append("AT POSITION LIMIT")
        if streak >= cfg.max_consecutive_losses:
            flags.append("KILL SWITCH")
        flag = f"  [{', '.join(flags)}]" if flags else ""
        print(f"[{book}] open positions: {n_open}/{cfg.max_open_positions} | "
              f"open risk ${j.open_risk(book):.0f} | consecutive losses: "
              f"{streak}/{cfg.max_consecutive_losses}{flag}")
    open_ = j.open_positions()
    heat_cap = 2.0 * cfg.daily_loss_limit
    print(f"Account-wide (all books): {len(open_)} open | open risk "
          f"${j.open_risk():.0f} of ${heat_cap:.0f} heat cap")
    for r in open_:
        line = (f"  #{r.id} [{r.strategy}] {r.underlying} {r.expiration} "
                f"{r.kind} {r.long_strike:g}/{r.short_strike:g} x{r.contracts} "
                f"@ {r.entry_debit:.2f} (max loss ${r.max_loss:.0f})")
        if r.strategy == "vertical":
            # The take-profit formula is for debit verticals; credit books
            # are managed by their own scripts with their own exit rules.
            tp = broker.take_profit_target(r)
            line += f" [TP target: spread mid >= {tp:.2f}]"
        print(line)
    return 0


def cmd_stats(args) -> int:
    broker = _broker(args)
    stats = broker.journal.stats(strategy="vertical")
    if stats.get("closed_trades", 0) == 0:
        print("No closed trades yet.")
        return 0
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="journal.db")
    ap.add_argument("--config", help="Path to StrategyConfig JSON")
    ap.add_argument("--price-provider", choices=SETTLEMENT_PROVIDERS,
                    default="yfinance",
                    help="Settlement-close source for `settle` (default "
                         "yfinance; alphavantage needs ALPHA_VANTAGE_API_KEY, "
                         "free tier)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("open")
    p.add_argument("--scan-file", required=True)
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--contracts", type=int, default=1)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("close")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--value", type=float, required=True,
                   help="Current spread mid value per share, e.g. 1.35")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("settle")
    p.set_defaults(func=cmd_settle)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("stats")
    p.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
