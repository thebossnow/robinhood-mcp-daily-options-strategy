#!/usr/bin/env python3
"""Daily management of open paper credit positions.

    python scripts/manage_credit.py --provider mcp

For each open credit position (journal rows with strategy='credit'), marks
the legs from the live chain and applies, in order:

1. profit target  — close when >= profit_take_frac of entry credit captured
2. time exit      — close at min(21 DTE, half the entry DTE)
3. settlement     — if expiration passed, settle at intrinsic

No breach stop — the 2022-2026 sweep measured it as the dominant loss
driver. Daily (vs the backtest's weekly) checks are the fidelity upgrade
this paper phase exists to measure: log lines record the mark and the
distance to the profit target even on days with no action.
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
from options_trader.risk.vol_regime import fetch_vix_closes
from options_trader.signals.credit import VALIDATED, CreditVariantConfig


def mark_position(legs: list[dict], chain) -> tuple[float, float] | None:
    """(cost_to_close_at_mid, half_spread_sum) from a live chain DataFrame,
    or None if any leg lacks a usable quote."""
    cost, half_sum = 0.0, 0.0
    for leg in legs:
        row = chain[(chain["type"] == leg["type"])
                    & (chain["strike"] == leg["strike"])]
        if row.empty:
            return None
        bid, ask = float(row.iloc[0]["bid"]), float(row.iloc[0]["ask"])
        if ask <= 0 or ask < bid:
            return None
        cost += -leg["side"] * (bid + ask) / 2.0
        half_sum += (ask - bid) / 2.0
    return cost, half_sum


def variant_config(kind: str) -> CreditVariantConfig:
    """Management parameters for a journaled position. Falls back to the
    shared defaults if the variant was renamed after entry."""
    return VALIDATED.get(kind, CreditVariantConfig(name=kind))


def manage_position(rec, cfg: StrategyConfig, journal: Journal,
                    broker: PaperBroker, provider, settlement_provider,
                    today: date) -> str:
    """Apply the profit-target / time-exit / settlement rules to one open
    position and return the line describing what happened. Raises on any
    provider/data failure — the caller decides whether that should abort
    the whole batch or just skip this position and retry it next run."""
    import json as _json

    vcfg = variant_config(rec.kind)
    entry = journal.candidate(rec.id) or {}
    dte_at_entry = int(entry.get("dte_at_entry", 45))
    dte = (date.fromisoformat(rec.expiration) - today).days
    legs = _json.loads(rec.legs_json or "[]")
    tag = f"#{rec.id} {rec.kind} exp {rec.expiration} ({dte} DTE)"

    if dte <= 0:
        px = settlement_provider.get_settlement_close(rec.underlying, rec.expiration)
        if px is None:
            return f"{tag}: expired but no settlement close yet — retry next run"
        closed = broker.settle_expired_credit(rec.id, px)
        return f"{tag}: SETTLED at {px:.2f}, pnl {closed.realized_pnl:+.2f}"

    snap = provider.get_chain(rec.underlying, rec.expiration)
    marks = mark_position(legs, snap.chain)
    if marks is None:
        return f"{tag}: unmarkable today (missing/crossed leg quote) — no action"
    cost_mid, half_sum = marks
    exit_cost_est = cost_mid + cfg.slippage_half_spread_frac * half_sum
    captured = rec.entry_debit - exit_cost_est
    target = vcfg.profit_take_frac * rec.entry_debit

    if captured >= target:
        closed = broker.close_credit(rec.id, cost_mid, half_sum,
                                     notes="profit target (daily check)")
        return (f"{tag}: PROFIT TARGET — captured {captured:.2f} of "
                f"{rec.entry_debit:.2f}, pnl {closed.realized_pnl:+.2f}")
    if dte <= vcfg.time_exit_threshold(dte_at_entry):
        closed = broker.close_credit(rec.id, cost_mid, half_sum,
                                     notes=f"time exit at {dte} DTE")
        return f"{tag}: TIME EXIT at {dte} DTE, pnl {closed.realized_pnl:+.2f}"
    return (f"{tag}: hold — captured {captured:+.2f} / target "
            f"{target:.2f}, exit cost est {exit_cost_est:.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/credit_paper.json")
    ap.add_argument("--provider", choices=["mcp", "yfinance"],
                    default="yfinance")
    ap.add_argument("--journal", default="journal.db")
    args = ap.parse_args()

    cfg = StrategyConfig.from_json(args.config)
    if args.provider == "mcp":
        from options_trader.data.mcp_provider import MCPDataProvider
        provider = MCPDataProvider()
    else:
        from options_trader.data.provider import YFinanceProvider
        provider = YFinanceProvider()

    journal = Journal(args.journal)
    # This script only closes/settles positions (risk.check() is never
    # called from that path — see close_credit/settle_expired_credit in
    # execution/paper.py), so the vol gate is currently a no-op here.
    # Wired anyway for consistency with scan_credit.py/paper_trade.py and
    # so a future roll/reopen path doesn't silently skip it.
    broker = PaperBroker(
        cfg, journal,
        vix_provider=lambda: fetch_vix_closes(cfg.vix_spike_lookback_days),
    )
    today = date.today()

    open_positions = journal.open_credit_positions()
    print(f"{datetime.now().isoformat(timespec='seconds')}: "
          f"{len(open_positions)} open credit position(s)")

    # Settlement only needs the underlying's official close, not a live
    # bid/ask — yfinance's delayed close is fine even when --provider mcp
    # is used for marking (MCPDataProvider has no settlement-close call).
    if args.provider == "yfinance":
        settlement_provider = provider
    else:
        from options_trader.data.provider import YFinanceProvider
        settlement_provider = YFinanceProvider()

    for rec in open_positions:
        try:
            print(manage_position(rec, cfg, journal, broker, provider,
                                  settlement_provider, today))
        except Exception as e:
            print(f"#{rec.id} {rec.kind} exp {rec.expiration}: ERROR during "
                  f"management — {e!r} — skipped, will retry next run")

    print(f"Journal stats: {journal.stats(strategy='credit')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
