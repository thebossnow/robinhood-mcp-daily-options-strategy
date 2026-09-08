#!/usr/bin/env python3
"""Daily management + weekly-review reporting for the CSP income book.

    python scripts/manage_csp.py --net-liq 25000 --cash-available 18000

For every open cash-secured put (strategy='csp'):
  1. settlement  — past expiry: assign (ITM) or expire worthless (OTM)
  2. profit take — close when captured >= profit_take_frac of premium
  3. delta alert — |delta| >= delta_alert or already ITM: NOTIFY ONLY,
     proposes roll-down-and-out / buy-to-close / accept-assignment. This
     script never rolls or closes on a delta alert by itself — the user's
     rules give the human that decision.

For shares acquired via assignment (strategy='shares') from a PRIOR
session (never the same session as the assignment, per "next session
sell a covered call"): proposes/enters a covered call at 0.05-0.15 delta,
strike >= cost basis when possible.

For open covered calls (strategy='covered_call'): the same settle/profit-
take handling, plus "called away -> shares sold, back to cash" on
assignment.

Also prints the cash-buffer / SGOV-sweep check and the weekly-review table
(open shorts, delta, DTE, % of max profit, cash %, notional SGOV). Run it
daily for management, or any time for the report alone with --report-only.

No live Robinhood trading tool is connected — this only ever updates the
paper journal; a human places any real order it recommends.
"""

from __future__ import annotations

import argparse
import json as _json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.config import CSPConfig
from options_trader.execution.paper_csp import CSPPaperBroker
from options_trader.journal import Journal
from options_trader.risk.csp_manager import CSPRiskManager
from options_trader.signals.covered_call import CSPLiquidityRules, build_candidate as build_cc_candidate
from options_trader.signals.credit import bs_delta


def _mark_leg(chain, opt_type: str, strike: float) -> tuple[float, float] | None:
    """(mid, half_spread) for one leg from a live chain, or None if unmarkable."""
    row = chain[(chain["type"] == opt_type) & (chain["strike"] == strike)]
    if row.empty:
        return None
    bid, ask = float(row.iloc[0]["bid"]), float(row.iloc[0]["ask"])
    if ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0, (ask - bid) / 2.0


def manage_csp(rec, cfg: CSPConfig, journal: Journal, broker: CSPPaperBroker,
               provider, settlement_provider, today: date) -> str:
    dte = (date.fromisoformat(rec.expiration) - today).days
    tag = f"#{rec.id} CSP {rec.underlying} -{rec.short_strike:g}P exp {rec.expiration} ({dte} DTE)"

    if dte <= 0:
        px = settlement_provider.get_settlement_close(rec.underlying, rec.expiration)
        if px is None:
            return f"{tag}: expired but no settlement close yet — retry next run"
        if px < rec.short_strike:
            closed, shares_id = broker.assign(rec.id, px)
            return (f"{tag}: ASSIGNED at {px:.2f} — pnl {closed.realized_pnl:+.2f}, "
                    f"opened shares position #{shares_id} "
                    f"({rec.contracts * 100} shares, cost basis "
                    f"{rec.short_strike - rec.entry_debit:.2f}). "
                    f"Propose a covered call NEXT session.")
        expired = broker.expire_worthless(rec.id, px)
        return f"{tag}: EXPIRED WORTHLESS at {px:.2f}, pnl {expired.realized_pnl:+.2f}"

    try:
        snap = provider.get_chain(rec.underlying, rec.expiration)
    except Exception as e:
        return f"{tag}: ERROR fetching chain — {e!r} — no action"

    marks = _mark_leg(snap.chain, "put", rec.short_strike)
    if marks is None:
        return f"{tag}: unmarkable today (missing/crossed quote) — no action"
    mid, half_spread = marks

    captured = rec.entry_debit - mid
    target = cfg.profit_take_frac * rec.entry_debit
    if captured >= target:
        closed = broker.close(rec.id, mid, half_spread, notes="profit target")
        return (f"{tag}: PROFIT TARGET — captured {captured:.2f} of "
                f"{rec.entry_debit:.2f}, pnl {closed.realized_pnl:+.2f}")

    t_years = max(dte, 1) / 365.0
    row = snap.chain[(snap.chain["type"] == "put") & (snap.chain["strike"] == rec.short_strike)]
    iv = float(row.iloc[0]["iv"]) if not row.empty else 0.0
    delta = abs(bs_delta("put", snap.spot, rec.short_strike, iv, t_years)) if iv > 0 else 0.0
    itm = snap.spot < rec.short_strike

    if itm or delta >= cfg.delta_alert:
        urgency = "URGENT" if dte <= cfg.urgent_dte else "ALERT"
        return (f"{tag}: {urgency} — spot {snap.spot:.2f}, delta {delta:.3f}"
                f"{' (ITM)' if itm else ''}. Propose: (a) roll down-and-out for a "
                f"credit, (b) buy to close at ~{mid:.2f}, or (c) accept assignment. "
                f"NO ACTION TAKEN — human decision required.")

    return (f"{tag}: hold — captured {captured:+.2f} / target {target:.2f}, "
            f"spot {snap.spot:.2f}, delta {delta:.3f}, mark {mid:.2f}")


def manage_cc(rec, cfg: CSPConfig, journal: Journal, broker: CSPPaperBroker,
             provider, settlement_provider, today: date,
             shares_by_underlying: dict[str, object]) -> str:
    dte = (date.fromisoformat(rec.expiration) - today).days
    tag = f"#{rec.id} CC {rec.underlying} +{rec.short_strike:g}C exp {rec.expiration} ({dte} DTE)"
    shares = shares_by_underlying.get(rec.underlying)

    if dte <= 0:
        px = settlement_provider.get_settlement_close(rec.underlying, rec.expiration)
        if px is None:
            return f"{tag}: expired but no settlement close yet — retry next run"
        if px > rec.short_strike and shares is not None:
            cc_closed, sh_closed = broker.call_away(rec.id, shares.id, px)
            return (f"{tag}: CALLED AWAY at {rec.short_strike:.2f} — call pnl "
                    f"{cc_closed.realized_pnl:+.2f}, shares pnl "
                    f"{sh_closed.realized_pnl:+.2f}. Back to cash — resume CSP scans.")
        expired = broker.close_cc(rec.id, 0.0, 0.0, status="expired",
                                  notes=f"expired worthless, underlying {px:.2f}")
        return f"{tag}: EXPIRED WORTHLESS at {px:.2f}, pnl {expired.realized_pnl:+.2f}"

    try:
        snap = provider.get_chain(rec.underlying, rec.expiration)
    except Exception as e:
        return f"{tag}: ERROR fetching chain — {e!r} — no action"
    marks = _mark_leg(snap.chain, "call", rec.short_strike)
    if marks is None:
        return f"{tag}: unmarkable today (missing/crossed quote) — no action"
    mid, half_spread = marks
    captured = rec.entry_debit - mid
    target = cfg.profit_take_frac * rec.entry_debit
    if captured >= target:
        closed = broker.close_cc(rec.id, mid, half_spread, notes="profit target")
        return (f"{tag}: PROFIT TARGET — captured {captured:.2f} of "
                f"{rec.entry_debit:.2f}, pnl {closed.realized_pnl:+.2f}")
    return f"{tag}: hold — captured {captured:+.2f} / target {target:.2f}, mark {mid:.2f}"


def propose_covered_calls(cfg: CSPConfig, journal: Journal, broker: CSPPaperBroker,
                          provider, today: date, dry_run: bool) -> list[str]:
    lines: list[str] = []
    open_cc_underlyings = {r.underlying for r in journal.open_cc_positions()}
    for shares in journal.open_shares_positions():
        if shares.underlying in open_cc_underlyings:
            continue
        opened_day = shares.opened_at[:10]
        if opened_day >= today.isoformat():
            lines.append(f"shares #{shares.id} {shares.underlying}: assigned today — "
                        f"covered call proposed NEXT session, per the rules.")
            continue

        delta_min, delta_max, delta_target = cfg.delta_window(shares.underlying)
        rules = CSPLiquidityRules(
            min_open_interest=cfg.min_open_interest, min_volume=cfg.min_volume,
            max_spread_pct=cfg.max_spread_pct, min_premium=cfg.min_premium,
            require_nonzero_bid=cfg.require_nonzero_bid,
        )
        try:
            expirations = provider.get_expirations(shares.underlying)
        except Exception as e:
            lines.append(f"shares #{shares.id} {shares.underlying}: ERROR fetching "
                        f"expirations — {e!r}")
            continue

        def dte_of(e: str) -> int:
            return (date.fromisoformat(e) - today).days
        candidates_exp = [e for e in expirations if cfg.min_dte <= dte_of(e) <= cfg.max_dte]
        if not candidates_exp:
            lines.append(f"shares #{shares.id} {shares.underlying}: no covered-call "
                        f"expiration in {cfg.min_dte}-{cfg.max_dte} DTE")
            continue
        expiration = min(candidates_exp, key=lambda e: abs(dte_of(e) - cfg.target_dte))
        dte = dte_of(expiration)

        try:
            snap = provider.get_chain(shares.underlying, expiration)
        except Exception as e:
            lines.append(f"shares #{shares.id} {shares.underlying}: ERROR fetching "
                        f"chain — {e!r}")
            continue

        cand = build_cc_candidate(
            snap.chain, snap.spot, shares.underlying, expiration, dte,
            shares.short_strike, delta_min, delta_max, delta_target, rules,
            cfg.slippage_half_spread_frac,
        )
        if cand is None:
            lines.append(f"shares #{shares.id} {shares.underlying}: no covered-call "
                        f"strike qualifies ({expiration}, {dte} DTE)")
            continue

        note = "" if cand.at_or_above_cost_basis else \
            " [WARNING: below cost basis — no qualifying strike at/above it]"
        desc = (f"{shares.underlying} {expiration} ({dte} DTE) +{cand.strike:g}C "
                f"delta {cand.delta:.3f}, premium {cand.premium:.2f}{note}")
        if dry_run:
            lines.append(f"[dry-run] shares #{shares.id}: would sell {desc}")
            continue
        trade_id = broker.open_cc(cand, contracts=shares.contracts,
                                  notes="paper covered call, next-session entry")
        lines.append(f"shares #{shares.id}: OPENED CC #{trade_id} {desc}")
    return lines


def cash_report(cfg: CSPConfig, journal: Journal, net_liq: float,
                cash_available: float, sgov_shares: float,
                sgov_price: float | None) -> list[str]:
    lines: list[str] = []
    buffer_pct = cash_available / net_liq if net_liq > 0 else 0.0
    lines.append(f"Cash buffer: {cash_available:.0f} / {net_liq:.0f} = "
                f"{buffer_pct:.1%} (floor {cfg.min_cash_buffer_pct:.0%}, "
                f"target {cfg.target_cash_buffer_pct:.0%})")
    if buffer_pct < cfg.min_cash_buffer_pct:
        lines.append("  ** BELOW FLOOR ** — new CSP entries are refused until this recovers.")

    sweep = cash_available - cfg.target_cash_buffer_pct * net_liq
    if sweep > 0:
        line = f"  Sweep suggestion: park ~{sweep:.0f} in SGOV (excess above target buffer)."
        if sgov_price:
            line += f" ~{sweep / sgov_price:.1f} shares at {sgov_price:.2f}."
        lines.append(line)
    elif sweep < 0:
        line = f"  Sweep suggestion: sell ~{-sweep:.0f} of SGOV to rebuild the buffer."
        if sgov_price:
            line += f" ~{-sweep / sgov_price:.1f} shares at {sgov_price:.2f}."
        lines.append(line)

    if sgov_shares:
        sgov_value = sgov_shares * sgov_price if sgov_price else None
        lines.append(f"  Current SGOV holding: {sgov_shares:g} shares"
                    + (f" (~{sgov_value:.0f})" if sgov_value else ""))
    return lines


def weekly_review(journal: Journal, today: date) -> list[str]:
    lines = ["", "=== Weekly review: open CSP + covered-call book ==="]
    for rec in journal.open_csp_positions():
        dte = (date.fromisoformat(rec.expiration) - today).days
        entry = journal.candidate(rec.id) or {}
        entry_delta = entry.get("delta")
        lines.append(
            f"  CSP  {rec.underlying:6s} -{rec.short_strike:g}P exp {rec.expiration} "
            f"({dte} DTE) x{rec.contracts}  entry premium {rec.entry_debit:.2f}  "
            f"entry delta {entry_delta}"
        )
    for rec in journal.open_cc_positions():
        dte = (date.fromisoformat(rec.expiration) - today).days
        lines.append(
            f"  CC   {rec.underlying:6s} +{rec.short_strike:g}C exp {rec.expiration} "
            f"({dte} DTE) x{rec.contracts}  entry premium {rec.entry_debit:.2f}"
        )
    for rec in journal.open_shares_positions():
        lines.append(
            f"  SHARES {rec.underlying:6s} {rec.contracts * 100} shares  "
            f"cost basis {rec.short_strike:.2f}  (assigned {rec.opened_at[:10]})"
        )
    if not (journal.open_csp_positions() or journal.open_cc_positions()
            or journal.open_shares_positions()):
        lines.append("  (nothing open)")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/csp_paper.json")
    ap.add_argument("--provider", choices=["mcp", "yfinance"], default="yfinance")
    ap.add_argument("--journal", default="journal.db")
    ap.add_argument("--net-liq", type=float, required=True)
    ap.add_argument("--cash-available", type=float, default=None)
    ap.add_argument("--sgov-shares", type=float, default=0.0,
                    help="Current SGOV shares held (for the cash report).")
    ap.add_argument("--sgov-price", type=float, default=None,
                    help="SGOV price for the sweep-size estimate; fetched "
                         "via the provider if omitted.")
    ap.add_argument("--report-only", action="store_true",
                    help="Skip management actions, print reports only.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = CSPConfig.from_json(args.config)
    if args.provider == "mcp":
        from options_trader.data.mcp_provider import MCPDataProvider
        provider = MCPDataProvider()
    else:
        from options_trader.data.provider import YFinanceProvider
        provider = YFinanceProvider()

    journal = Journal(args.journal)
    committed = sum(journal.csp_collateral_by_underlying().values())
    cash_available = args.cash_available
    if cash_available is None:
        cash_available = args.net_liq - committed
    risk = CSPRiskManager(cfg, journal, args.net_liq, cash_available)
    broker = CSPPaperBroker(cfg, journal, risk)
    today = date.today()

    if args.provider == "yfinance":
        settlement_provider = provider
    else:
        from options_trader.data.provider import YFinanceProvider
        settlement_provider = YFinanceProvider()

    print(f"{datetime.now().isoformat(timespec='seconds')}: "
          f"{len(journal.open_csp_positions())} open CSP(s), "
          f"{len(journal.open_cc_positions())} open covered call(s), "
          f"{len(journal.open_shares_positions())} open shares position(s)")

    if not args.report_only:
        for rec in journal.open_csp_positions():
            try:
                print(manage_csp(rec, cfg, journal, broker, provider,
                                 settlement_provider, today))
            except Exception as e:
                print(f"#{rec.id} CSP {rec.underlying}: ERROR — {e!r} — skipped")

        shares_by_underlying = {r.underlying: r for r in journal.open_shares_positions()}
        for rec in journal.open_cc_positions():
            try:
                print(manage_cc(rec, cfg, journal, broker, provider,
                               settlement_provider, today, shares_by_underlying))
            except Exception as e:
                print(f"#{rec.id} CC {rec.underlying}: ERROR — {e!r} — skipped")

        for line in propose_covered_calls(cfg, journal, broker, provider, today,
                                          args.dry_run):
            print(line)

    print()
    for line in cash_report(cfg, journal, args.net_liq, cash_available,
                            args.sgov_shares, args.sgov_price):
        print(line)

    for line in weekly_review(journal, today):
        print(line)

    print(f"\nJournal stats — CSP: {journal.stats(strategy='csp')}")
    print(f"Journal stats — covered calls: {journal.stats(strategy='covered_call')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
