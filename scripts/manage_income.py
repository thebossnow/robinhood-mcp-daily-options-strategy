#!/usr/bin/env python3
"""Daily management of open income positions, with IV-spike grading.

    python scripts/manage_income.py --provider mcp

Everything scripts/manage_credit.py does — settle expiries, take profit at
50% of the entry credit, time-exit at min(21 DTE, half the entry DTE) — plus
the one rule the income framework adds and that pipeline lacks: a graded
response to a post-entry volatility spike.

The grading is REPORT-ONLY. It never closes or rolls a position. That split
is deliberate:

  The profit target and the time exit are mechanical rules with a
  backtested basis, so the script executes them. The IV-spike response is a
  rule this repository has NOT measured, and whose closest measured
  analogue (the short-strike breach stop) was the single largest loss
  driver in the 2022-26 sweep. Wiring an unmeasured exit into an automatic
  path would be adopting on faith exactly the kind of rule the sweep
  disproved. So it prints DEFEND / CLOSE with a reason, and a human
  decides.

Positions are graded by comparing the short leg's entry IV against its IV
in today's chain, alongside today's short-leg delta. Both come from the
live chain; when it quotes neither, the grade degrades to WATCH at most,
never to an exit (see risk/iv_spike.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.config import StrategyConfig
from options_trader.execution.paper import PaperBroker
from options_trader.journal import Journal
from options_trader.risk.iv_spike import IVSpikeConfig, assess
from options_trader.risk.vol_regime import fetch_vix_closes
from options_trader.signals.credit import CreditVariantConfig
from options_trader.signals.income import DEFAULT_PROFILE, PROFILES, get_profile

# Reuse the marking and exit logic rather than forking it: a second copy
# that drifts from manage_credit.py would be worse than a shared one.
# Imported through the `scripts` namespace package (there is no
# __init__.py) rather than as a bare `manage_credit`, so this resolves the
# same way whether the file is run as a script — the sys.path insert above
# puts the repo root first — or imported as `scripts.manage_income` by the
# tests, where scripts/ itself is not on the path.
from scripts.manage_credit import manage_position, mark_position   # noqa: E402,F401


def short_leg_quote(legs: list[dict], chain) -> tuple[float | None, float | None]:
    """(iv, |delta|) for the tested short leg from today's chain.

    The put side is the tested side: an index that rallies through a short
    call is a loss too, but it is the selloff that produces the fast,
    gapping version this grading exists to catch. Falls back to the short
    call when the structure has no put side.
    """
    for opt_type in ("put", "call"):
        short = next((l for l in legs
                      if l["side"] == -1 and l["type"] == opt_type), None)
        if short is None:
            continue
        row = chain[(chain["type"] == short["type"])
                    & (chain["strike"] == short["strike"])]
        if row.empty:
            return None, None
        r = row.iloc[0]
        iv = float(r.get("iv", 0.0) or 0.0) or None
        raw = r.get("delta", 0.0)
        delta = abs(float(raw)) if raw else None
        return iv, delta
    return None, None


def entry_short_iv(entry: dict) -> float | None:
    """Entry IV of the tested short leg, from the journaled candidate."""
    for opt_type in ("put", "call"):
        for leg in entry.get("legs", []):
            if leg.get("side") == -1 and leg.get("type") == opt_type:
                return float(leg.get("entry_iv") or 0.0) or None
    return None


def grade_position(rec, journal: Journal, chain,
                   cfg: IVSpikeConfig) -> str | None:
    """One line describing the IV-spike grade, or None when there is
    nothing worth saying (a NONE grade on a quiet position)."""
    entry = journal.candidate(rec.id) or {}
    legs = json.loads(rec.legs_json or "[]")
    e_iv = entry_short_iv(entry)
    if e_iv is None:
        return None
    now_iv, now_delta = short_leg_quote(legs, chain)
    if now_iv is None:
        return None
    rolls = int(entry.get("rolls_used", 0))
    signal = assess(e_iv, now_iv, now_delta, cfg, rolls_used=rolls)
    if signal.action == "NONE":
        return None
    return f"  vol grade: {signal.describe()}"


def variant_config(kind: str, profile) -> CreditVariantConfig:
    """Management parameters for a journaled position, looked up across
    every profile so a position entered under one profile is still managed
    by its own rules after the operator switches default. The fallback is a
    bare CreditVariantConfig, which is the right answer for a variant that
    was renamed after entry and the wrong one for a variant that simply
    lives elsewhere — hence the search across PROFILES first."""
    if kind in profile.variants:
        return profile.variants[kind]
    for p in PROFILES.values():
        if kind in p.variants:
            return p.variants[kind]
    return CreditVariantConfig(name=kind)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/income_framework.json")
    ap.add_argument("--profile", default=DEFAULT_PROFILE, choices=sorted(PROFILES))
    ap.add_argument("--provider", choices=["mcp", "yfinance"], default="yfinance")
    ap.add_argument("--journal", default="journal.db")
    args = ap.parse_args()

    cfg = StrategyConfig.from_json(args.config)
    profile = get_profile(args.profile)
    spike_cfg = IVSpikeConfig()

    if args.provider == "mcp":
        from options_trader.data.mcp_provider import MCPDataProvider
        provider = MCPDataProvider()
        from options_trader.data.provider import YFinanceProvider
        settlement_provider = YFinanceProvider()
    else:
        from options_trader.data.provider import YFinanceProvider
        provider = YFinanceProvider()
        settlement_provider = provider

    journal = Journal(args.journal)
    broker = PaperBroker(
        cfg, journal,
        vix_provider=lambda: fetch_vix_closes(cfg.vix_spike_lookback_days),
    )
    today = date.today()

    open_positions = journal.open_credit_positions()
    print(f"{datetime.now().isoformat(timespec='seconds')}: "
          f"{len(open_positions)} open position(s), profile {profile.name}")

    attention = 0
    for rec in open_positions:
        try:
            # Grade BEFORE managing: once manage_position closes a trade the
            # position is gone, and the grade that would have explained why
            # the market moved under it goes unrecorded.
            grade = None
            dte = (date.fromisoformat(rec.expiration) - today).days
            if dte > 0:
                snap = provider.get_chain(rec.underlying, rec.expiration)
                grade = grade_position(rec, journal, snap.chain, spike_cfg)
            print(manage_position(rec, cfg, journal, broker, provider,
                                  settlement_provider, today,
                                  vcfg=variant_config(rec.kind, profile)))
            if grade:
                print(grade)
                if "DEFEND" in grade or "CLOSE" in grade:
                    attention += 1
        except Exception as e:
            print(f"#{rec.id} {rec.kind} exp {rec.expiration}: ERROR during "
                  f"management — {e!r} — skipped, will retry next run")

    if attention:
        print(f"\n{attention} position(s) need a human decision — nothing was "
              f"rolled or closed on the vol grade alone.")
    print(f"Journal stats: {journal.stats(strategy='income')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
