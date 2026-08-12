#!/usr/bin/env python3
"""Credit-strategy paper entries (validated variants, SPY only).

    python scripts/scan_credit.py --provider mcp

Runs every weekday via cron (credit_entry.sh). Each variant selects its own
target expiration from its DTE window, so short-DTE variants (spy_weekly_put10:
5-14 DTE) and long-DTE variants (spy_condor15, spy_put10: 35-50 DTE) can
coexist. Long-DTE variants are Monday-only (pass --force to override).

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
    VALIDATED, CreditVariantConfig, build_position, leg_passes_live_liquidity,
)

# DTE threshold: variants with max_dte below this are "weekly" and enter any day
WEEKLY_MAX_DTE = 20


def pick_expiration(expirations: list[str], today: date, min_dte: int,
                    max_dte: int, target_dte: int) -> str | None:
    def dte(e: str) -> int:
        return (date.fromisoformat(e) - today).days
    ok = [e for e in expirations if min_dte <= dte(e) <= max_dte]
    if not ok:
        return None
    return min(ok, key=lambda e: abs(dte(e) - target_dte))


def resolve_dte_window(vcfg: CreditVariantConfig, cfg: StrategyConfig,
                       is_weekly: bool) -> tuple[int, int]:
    """Long-DTE variants are further constrained by the account-level window
    in configs/credit_paper.json (e.g. 35-50 DTE), which may be narrower
    than the variant's own backtested range (e.g. 25-50). Weekly variants
    keep their own window untouched — the account-level window targets the
    long-DTE variants and would eliminate a weekly variant's short DTE range
    entirely if applied there too."""
    if is_weekly:
        return vcfg.min_dte, vcfg.max_dte
    return max(vcfg.min_dte, cfg.min_dte), min(vcfg.max_dte, cfg.max_dte)


def _chain_cache(provider, underlying: str) -> dict:
    """Fetch chains once per expiration, reuse across variants."""
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/credit_paper.json")
    ap.add_argument("--provider", choices=["mcp", "yfinance"],
                    default="yfinance")
    ap.add_argument("--journal", default="journal.db")
    ap.add_argument("--force", action="store_true",
                    help="Enter long-DTE variants even if today isn't Monday")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print candidates without journaling anything")
    args = ap.parse_args()

    today = date.today()
    is_monday = today.weekday() == 0

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

    all_expirations = provider.get_expirations(underlying)
    open_kinds = {(r.kind, r.expiration)
                  for r in journal.open_credit_positions()}

    chain_cache: dict[str, object] = {}  # expiration -> snap

    for name, vcfg in VALIDATED.items():
        is_weekly = vcfg.max_dte <= WEEKLY_MAX_DTE

        # Long-DTE variants are Monday-only (weekly cadence matches their DTE)
        if not is_weekly and not is_monday and not args.force:
            print(f"{name}: skipped — long-DTE variant, today ({today}) is not Monday")
            continue

        min_dte, max_dte = resolve_dte_window(vcfg, cfg, is_weekly)

        expiration = pick_expiration(all_expirations, today, min_dte,
                                     max_dte, vcfg.target_dte)
        if expiration is None:
            msg = (f"{name}: NO QUALIFYING TRADE — no expiration in "
                   f"{min_dte}-{max_dte} DTE")
            print(msg)
            if not args.dry_run:
                journal.log_no_trade(msg, strategy="credit")
            continue

        if (name, expiration) in open_kinds:
            print(f"{name}: already open for {expiration} — skipped")
            continue

        if expiration not in chain_cache:
            snap = provider.get_chain(underlying, expiration)
            chain_cache[expiration] = snap
        snap = chain_cache[expiration]
        liquid = snap.chain[snap.chain.apply(leg_passes_live_liquidity, axis=1)]
        print(f"{name}: {underlying} {expiration} ({snap.dte} DTE), spot {snap.spot:.2f}, "
              f"{len(liquid)}/{len(snap.chain)} contracts pass liquidity")

        pos = build_position(liquid, snap.spot, underlying,
                             today.isoformat(), expiration, snap.dte, vcfg,
                             cfg.slippage_half_spread_frac)
        if pos is None:
            msg = f"{name}: NO QUALIFYING TRADE (gates failed on live chain)"
            print(msg)
            if not args.dry_run:
                journal.log_no_trade(msg, strategy="credit")
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
            journal.log_no_trade(msg, strategy="credit")
        else:
            print(f"OPENED #{trade_id} {desc}")

    credit_stats = journal.stats(strategy="credit")
    print(f"\n{datetime.now().isoformat(timespec='seconds')} scan complete. "
          f"Journal stats: {credit_stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
