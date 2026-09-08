#!/usr/bin/env python3
"""Cash-secured-put income scan: sell puts on a liquid, ownable universe.

    python scripts/scan_csp.py --net-liq 25000 --cash-available 18000

Walks configs/csp_paper.json's universe in priority order (QQQ first, then
HOOD/PLTR/TSLA/TSLL/NVDA/MU/SOXL). For each underlying it picks the
nearest-to-target expiration in the DTE window, selects the strike whose
delta falls in [delta_min, delta_max] closest to delta_target, and asks
CSPRiskManager whether a new position is allowed — which enforces the
collateral cap per underlying, the cash-buffer floor, the one-underlying-
at-a-time rule (QQQ laddering excepted), the kill switch, and the earnings
blackout. Every refusal and every empty scan is journaled as NO QUALIFYING
TRADE (strategy='csp') — a valid, expected outcome, not a script failure.

No live Robinhood trading tool is connected to this pipeline. This script
only ever produces PAPER fills (options_trader/execution/paper_csp.py); a
human reads its output and decides whether to place the real order.

--net-liq and --cash-available describe the account and there is no
account tool wired in to fetch them — pass your actual current numbers by
hand each run. If --cash-available is omitted it defaults to
net-liq minus collateral already committed to open CSPs, which assumes no
other consumers of cash (i.e. you're keeping this pipeline's view of cash
in sync with reality yourself).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.config import CSPConfig
from options_trader.execution.paper_csp import CSPPaperBroker
from options_trader.journal import Journal
from options_trader.risk.csp_manager import CSPRiskManager
from options_trader.signals.csp import CSPLiquidityRules, build_candidate


def pick_expiration(expirations: list[str], today: date, min_dte: int,
                    max_dte: int, target_dte: int) -> tuple[str, int] | None:
    def dte(e: str) -> int:
        return (date.fromisoformat(e) - today).days
    ok = [e for e in expirations if min_dte <= dte(e) <= max_dte]
    if not ok:
        return None
    exp = min(ok, key=lambda e: abs(dte(e) - target_dte))
    return exp, dte(exp)


def load_earnings_overrides(path: str) -> dict[str, list[str]]:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/csp_paper.json")
    ap.add_argument("--provider", choices=["mcp", "yfinance"], default="yfinance")
    ap.add_argument("--journal", default="journal.db")
    ap.add_argument("--earnings-config", default="configs/csp_earnings.json")
    ap.add_argument("--net-liq", type=float, required=True,
                    help="Total account value (cash + collateral + shares).")
    ap.add_argument("--cash-available", type=float, default=None,
                    help="Cash + SGOV not already committed as CSP collateral. "
                         "Defaults to net-liq minus open CSP collateral.")
    ap.add_argument("--long-dte", action="store_true",
                    help="Also try the 60-180 DTE window for underlyings with "
                         "no standard-window candidate.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print candidates and risk checks without journaling")
    args = ap.parse_args()

    today = date.today()
    cfg = CSPConfig.from_json(args.config)
    if args.provider == "mcp":
        from options_trader.data.mcp_provider import MCPDataProvider
        provider = MCPDataProvider()
    else:
        from options_trader.data.provider import YFinanceProvider
        provider = YFinanceProvider()

    journal = Journal(args.journal)
    earnings_overrides = load_earnings_overrides(args.earnings_config)

    committed = sum(journal.csp_collateral_by_underlying().values())
    cash_available = args.cash_available
    if cash_available is None:
        cash_available = args.net_liq - committed

    risk = CSPRiskManager(cfg, journal, args.net_liq, cash_available,
                          earnings_overrides)
    broker = CSPPaperBroker(cfg, journal, risk)

    print(f"{datetime.now().isoformat(timespec='seconds')} CSP scan — "
          f"net liq {args.net_liq:.0f}, cash available {cash_available:.0f} "
          f"({cash_available / args.net_liq:.1%} of net liq)")

    any_opened = False
    for underlying in cfg.universe:
        delta_min, delta_max, delta_target = cfg.delta_window(underlying)
        rules = CSPLiquidityRules(
            min_open_interest=cfg.min_open_interest, min_volume=cfg.min_volume,
            max_spread_pct=cfg.max_spread_pct, min_premium=cfg.min_premium,
            require_nonzero_bid=cfg.require_nonzero_bid,
        )
        min_otm = cfg.ladder_min_otm_pct if underlying in cfg.ladder_underlyings else 0.0

        try:
            expirations = provider.get_expirations(underlying)
        except Exception as e:
            print(f"{underlying}: ERROR fetching expirations — {e!r} — skipped")
            continue

        picked = pick_expiration(expirations, today, cfg.min_dte, cfg.max_dte,
                                 cfg.target_dte)
        window_note = f"{cfg.min_dte}-{cfg.max_dte} DTE"
        if picked is None and args.long_dte:
            picked = pick_expiration(expirations, today, cfg.long_dte_min,
                                     cfg.long_dte_max, cfg.long_dte_min)
            window_note = f"{cfg.long_dte_min}-{cfg.long_dte_max} DTE (long)"
        if picked is None:
            msg = f"{underlying}: NO QUALIFYING TRADE — no expiration in {window_note}"
            print(msg)
            if not args.dry_run:
                journal.log_no_trade(msg, strategy="csp")
            continue
        expiration, dte = picked

        try:
            snap = provider.get_chain(underlying, expiration)
        except Exception as e:
            print(f"{underlying}: ERROR fetching chain — {e!r} — skipped")
            continue

        cand = build_candidate(
            snap.chain, snap.spot, underlying, expiration, dte,
            delta_min, delta_max, delta_target, rules,
            cfg.slippage_half_spread_frac, min_otm,
        )
        if cand is None:
            msg = (f"{underlying}: NO QUALIFYING TRADE — no strike in delta "
                   f"[{delta_min:.2f},{delta_max:.2f}] passing liquidity/premium "
                   f"({expiration}, {dte} DTE)")
            print(msg)
            if not args.dry_run:
                journal.log_no_trade(msg, strategy="csp")
            continue

        desc = (f"{underlying} {cand.expiration} ({cand.dte} DTE) "
                f"-{cand.strike:g}P delta {cand.delta:.3f}, premium {cand.premium:.2f} "
                f"(collateral {cand.collateral:.0f}, ROC {cand.roc:.2%}, "
                f"ann. {cand.annualized_roc:.1%})")

        if args.dry_run:
            check = risk.check(underlying, cand.strike, cand.premium, today)
            status = f"would allow {check.max_contracts} contract(s)" if check.allowed \
                else f"REFUSED — {'; '.join(check.reasons)}"
            print(f"[dry-run] {desc} — {status}")
            continue

        trade_id, check = broker.open(cand, contracts=1, notes="paper CSP scan entry")
        if trade_id is None:
            msg = f"{underlying}: risk manager refused — {'; '.join(check.reasons)}"
            print(msg)
            journal.log_no_trade(msg, strategy="csp")
        else:
            any_opened = True
            print(f"OPENED #{trade_id} {desc}")

    if not any_opened and not args.dry_run:
        print("\nNO QUALIFYING TRADE TODAY across the whole universe.")

    print(f"\nJournal stats: {journal.stats(strategy='csp')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
