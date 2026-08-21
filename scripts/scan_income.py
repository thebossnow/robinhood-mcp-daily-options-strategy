#!/usr/bin/env python3
"""Income-framework entries: the full gate chain, one profile at a time.

    python scripts/scan_income.py --profile evidence_adjusted --provider mcp
    python scripts/scan_income.py --profile as_specified --dry-run

Runs the framework's rules in the order that discards work soonest, so a
non-entry day costs no API calls:

    1. entry cadence      Monday/Tuesday only (framework)
    2. vol regime         VIX ceiling / spike, fails CLOSED (repo)
    3. event blackout     no entry beside a known print, fails OPEN (repo)
    4. expiration pick    profile's DTE window
    5. structure build    credit spread / condor / cash-secured put
    6. IV rank            recorded always, gates only if profile says so
    7. event span         how many prints the position must live through
    8. sizing             tier schedule, book heat cap, integer contracts
    9. risk manager       kill switch, daily loss, concurrency
   10. report             entry price / strike / expiration / credit / risk %

Every stop is journaled as NO QUALIFYING TRADE with the gate that stopped
it, because a framework's value is mostly visible in what it refuses, and
that is only auditable if the refusals are written down.

Nothing here places a live order. Entries are journaled to the paper broker
and printed for a human to confirm, per the repo's standing rule.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.config import StrategyConfig
from options_trader.execution.paper import PaperBroker
from options_trader.journal import Journal
from options_trader.reporting import report_credit_position, report_csp_position
from options_trader.risk.events import (
    EventCalendar, check_events, premium_compensates,
)
from options_trader.risk.sizing import (
    csp_capital_at_risk, size_position, spread_capital_at_risk,
)
from options_trader.risk.vol_regime import check_vol_regime, fetch_vix_closes
from options_trader.signals.credit import build_position, leg_passes_live_liquidity
from options_trader.signals.csp import build_csp
from options_trader.signals.income import DEFAULT_PROFILE, PROFILES, get_profile
from options_trader.signals.iv_rank import atm_iv, build_iv_history, read_iv_rank

IV_HISTORY_PATH = "data_iv_history.json"
# Journal book tag. Keeps manage_credit.py from picking up income
# positions and managing them with the wrong variant registry.
INCOME_STRATEGY = "income"


def load_iv_history(path: str) -> dict[str, float]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_iv_observation(path: str, day: str, iv: float) -> None:
    """Append today's ATM IV so tomorrow's IV rank has a longer lookback.

    The series has to be grown by running the scanner; there is no free
    historical ATM-IV feed wired into this repo. `read_iv_rank` reports the
    lookback it actually used, so an eight-day-old series is never dressed
    up as a year of context.
    """
    hist = load_iv_history(path)
    hist[day] = round(iv, 6)
    Path(path).write_text(json.dumps(hist, indent=2, sort_keys=True))


def pick_expiration(expirations: list[str], today: date, min_dte: int,
                    max_dte: int, target_dte: int) -> str | None:
    def dte(e: str) -> int:
        return (date.fromisoformat(e) - today).days
    ok = [e for e in expirations if min_dte <= dte(e) <= max_dte]
    return min(ok, key=lambda e: abs(dte(e) - target_dte)) if ok else None


def _journal_skip(journal, msg: str, dry_run: bool) -> None:
    print(msg)
    if not dry_run:
        journal.log_no_trade(msg, strategy=INCOME_STRATEGY)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/income_framework.json")
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    choices=sorted(PROFILES))
    ap.add_argument("--provider", choices=["mcp", "yfinance"],
                    default="yfinance")
    ap.add_argument("--journal", default="journal.db")
    ap.add_argument("--events", default="configs/events.json")
    ap.add_argument("--iv-history", default=IV_HISTORY_PATH)
    ap.add_argument("--force", action="store_true",
                    help="Ignore the Monday/Tuesday entry-cadence gate")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print candidates and reports, journal nothing")
    args = ap.parse_args()

    today = date.today()
    profile = get_profile(args.profile)
    cfg = StrategyConfig.from_json(args.config)
    equity = cfg.account_equity

    print(f"=== income scan {today} | profile: {profile.name} ===")
    print(f"{profile.description}\n")

    # 1. entry cadence
    day_ok, day_msg = profile.entry_day_ok(today.weekday())
    if not day_ok and not args.force:
        print(f"NO QUALIFYING TRADE — {day_msg}")
        return 0
    print(f"cadence: {day_msg}" + (" (forced)" if not day_ok else ""))

    # 2. vol regime (fails closed)
    vix = fetch_vix_closes(cfg.vix_spike_lookback_days)
    if vix is None:
        print("NO QUALIFYING TRADE — vol regime check unavailable "
              "(no VIX data); refusing new entries defensively")
        return 0
    vol = check_vol_regime(cfg.vix_entry_ceiling, cfg.vix_spike_pct,
                           cfg.vix_spike_lookback_days, vix_closes=vix)
    if not vol.allowed:
        print(f"NO QUALIFYING TRADE — vol regime: {vol.reason}")
        return 0
    print(f"vol regime: VIX {vol.vix_level:.2f} "
          f"({vol.vix_pct_change:+.1%} over {cfg.vix_spike_lookback_days}d) — ok")

    # 3. event blackout (fails open, but says so)
    calendar = EventCalendar.from_json(args.events)
    journal = Journal(args.journal)
    broker = PaperBroker(cfg, journal, vix_provider=lambda: vix)

    if args.provider == "mcp":
        from options_trader.data.mcp_provider import MCPDataProvider
        provider = MCPDataProvider()
    else:
        from options_trader.data.provider import YFinanceProvider
        provider = YFinanceProvider()

    underlying = cfg.underlyings[0]
    expirations = provider.get_expirations(underlying)
    # Scoped to the income book: the credit book is a different script's
    # responsibility and its open positions must not suppress an income
    # entry (nor vice versa). Heat, by contrast, is account-wide on purpose
    # — capital is shared even when management is not.
    open_kinds = {(r.kind, r.expiration)
                  for r in journal.open_credit_positions(INCOME_STRATEGY)}
    open_capital = journal.open_risk()
    chain_cache: dict[str, object] = {}
    opened = 0

    def get_chain(expiration: str):
        if expiration not in chain_cache:
            chain_cache[expiration] = provider.get_chain(underlying, expiration)
        return chain_cache[expiration]

    # --- credit structures ---
    for name, vcfg in profile.variants.items():
        expiration = pick_expiration(expirations, today, vcfg.min_dte,
                                     vcfg.max_dte, vcfg.target_dte)
        if expiration is None:
            _journal_skip(journal, f"{name}: NO QUALIFYING TRADE — no "
                          f"expiration in {vcfg.min_dte}-{vcfg.max_dte} DTE",
                          args.dry_run)
            continue
        if (name, expiration) in open_kinds:
            print(f"{name}: already open for {expiration} — skipped")
            continue

        ev = check_events(today, date.fromisoformat(expiration), calendar,
                          profile.event_blackout_days_before,
                          profile.event_blackout_days_after)
        if not ev.allowed:
            _journal_skip(journal, f"{name}: NO QUALIFYING TRADE — {ev.reason}",
                          args.dry_run)
            continue

        snap = get_chain(expiration)
        liquid = snap.chain[snap.chain.apply(leg_passes_live_liquidity, axis=1)]
        pos = build_position(liquid, snap.spot, underlying, today.isoformat(),
                             expiration, snap.dte, vcfg,
                             cfg.slippage_half_spread_frac)
        if pos is None:
            _journal_skip(journal, f"{name}: NO QUALIFYING TRADE — gates failed "
                          f"on live chain (credit floor {vcfg.min_credit_frac:.2f} "
                          f"of width, {vcfg.short_put_delta or 0:.2f}/"
                          f"{vcfg.short_call_delta or 0:.2f} delta targets)",
                          args.dry_run)
            continue

        # 6. IV rank — recorded always
        observed = atm_iv(snap.chain, snap.spot)
        iv_note = "IV rank unavailable (no ATM IV in chain)"
        reading = None
        if observed is not None:
            if not args.dry_run:
                save_iv_observation(args.iv_history, today.isoformat(), observed)
            history = build_iv_history(load_iv_history(args.iv_history))
            reading = read_iv_rank(observed, history)
            iv_note = reading.describe()
            if not reading.meets(profile.min_iv_rank,
                                 profile.iv_rank_use_percentile):
                _journal_skip(journal, f"{name}: NO QUALIFYING TRADE — IV rank "
                              f"below preference {profile.min_iv_rank:.0%} "
                              f"({iv_note})", args.dry_run)
                continue

        # 7. event span + the "premium compensates" clause
        ok_prem, prem_note = premium_compensates(
            pos.credit, snap.spot, len(ev.spanned),
            profile.typical_event_move_pct)
        event_note = f"{ev.describe_span()} | {prem_note}"

        # 8. sizing
        risk_per = spread_capital_at_risk(max(pos.widths().values()), pos.credit)
        sizing = size_position(equity, risk_per, profile.tiers,
                               open_capital_at_risk=open_capital,
                               portfolio_heat_cap_pct=profile.portfolio_heat_cap_pct)
        if not sizing.feasible:
            _journal_skip(journal, f"{name}: NO QUALIFYING TRADE — sizing: "
                          f"{sizing.summary()}", args.dry_run)
            continue

        structure = ("iron_condor" if vcfg.short_call_delta and vcfg.short_put_delta
                     else "put_credit_spread")
        notes = [] if ok_prem else ["premium does not cover the spanned-event "
                                    "budget — consider reducing size"]
        report = report_credit_position(pos, sizing, equity, structure,
                                        iv_rank_note=iv_note,
                                        event_note=event_note, notes=notes)
        print()
        print(report.render())

        # The documented rule, enforced rather than merely printed: an
        # unconfirmed candidate is one whose delta or IV could not be
        # established from the chain OR the model, so nothing downstream
        # knows what it is selling. INCOME_AGENT_PROMPT.md tells the
        # operator this trade does not happen; before this check, the
        # scanner opened it anyway.
        if not report.confirmation.confirmed:
            _journal_skip(journal, f"{name}: NO QUALIFYING TRADE — IV/delta "
                          f"not confirmed: {report.confirmation.render()}",
                          args.dry_run)
            continue

        if args.dry_run:
            # Consume the heat budget anyway, so a dry run reports the same
            # contract counts a real run would. Without this the second and
            # third variants of a session are sized as though the first had
            # never been taken, and the preview quietly overstates the book.
            open_capital += sizing.total_capital_at_risk
            continue
        trade_id, check = broker.open_credit(pos, contracts=sizing.contracts,
                                             notes=f"income entry {name}",
                                             strategy=INCOME_STRATEGY)
        if trade_id is None:
            _journal_skip(journal, f"{name}: risk manager refused — "
                          f"{'; '.join(check.reasons)}", False)
        else:
            print(f"  OPENED #{trade_id} (paper) — confirm before any live order")
            open_capital += sizing.total_capital_at_risk
            opened += 1

    # --- cash-secured put ---
    if profile.csp is not None and profile.allow_cash_secured_puts:
        opened += _scan_csp(profile, cfg, equity, underlying, expirations,
                            today, get_chain, journal, args, open_capital)
    elif profile.csp is not None:
        print(f"\ncash-secured put: disabled for profile {profile.name} — "
              f"see risk/sizing.py (one index CSP's collateral exceeds the "
              f"tier budget at any equity this schedule targets)")

    print(f"\nscan complete: {opened} entry/entries. "
          f"Journal stats: {journal.stats(strategy=INCOME_STRATEGY)}")
    return 0


def _scan_csp(profile, cfg, equity, underlying, expirations, today,
              get_chain, journal, args, open_capital) -> int:
    ccfg = profile.csp
    expiration = pick_expiration(expirations, today, ccfg.min_dte,
                                 ccfg.max_dte, ccfg.target_dte)
    if expiration is None:
        _journal_skip(journal, f"{ccfg.name}: NO QUALIFYING TRADE — no "
                      f"expiration in {ccfg.min_dte}-{ccfg.max_dte} DTE",
                      args.dry_run)
        return 0
    snap = get_chain(expiration)
    liquid = snap.chain[snap.chain.apply(leg_passes_live_liquidity, axis=1)]
    pos = build_csp(liquid, snap.spot, underlying, today.isoformat(),
                    expiration, snap.dte, ccfg, cfg.slippage_half_spread_frac)
    if pos is None:
        _journal_skip(journal, f"{ccfg.name}: NO QUALIFYING TRADE — no put at "
                      f"{ccfg.short_put_delta:.2f} delta clearing the "
                      f"{ccfg.min_annualized_return:.0%} annualized floor",
                      args.dry_run)
        return 0

    sizing = size_position(equity, csp_capital_at_risk(pos.strike, pos.credit),
                           profile.tiers, open_capital_at_risk=open_capital,
                           portfolio_heat_cap_pct=profile.portfolio_heat_cap_pct)
    if not sizing.feasible:
        _journal_skip(journal, f"{ccfg.name}: NO QUALIFYING TRADE — sizing: "
                      f"{sizing.summary()}", args.dry_run)
        return 0

    report = report_csp_position(pos, sizing, equity)
    print()
    print(report.render())
    if not report.confirmation.confirmed:
        _journal_skip(journal, f"{ccfg.name}: NO QUALIFYING TRADE — IV/delta "
                      f"not confirmed: {report.confirmation.render()}",
                      args.dry_run)
        return 0
    print("  (cash-secured puts are journaled as reports only — the paper "
          "broker models defined-risk structures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
