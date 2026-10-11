"""Wheel / covered-call income strategy (S1 in the small-account income
backtest spec): a defined-risk put credit spread that, if assigned,
becomes 100 shares plus a short call against them, and unwinds back to a
fresh spread once called away.

Two phases:
  PHASE A  a put credit spread (short put + long wing below), built with
           signals/credit.py's build_position -- the wing keeps max loss
           defined while the position is open, matching the spec's "buy a
           wing ... so max loss is defined" rule.
  PHASE B  100 shares (from assignment) + a short call against them,
           built here since CreditPosition assumes an option-only
           structure with a hedging wing, and a covered call doesn't have
           one -- the shares are the hedge.

Assignment only converts Phase A into Phase B shares when the SHORT put
finishes ITM and the WING does not (wing_strike < settlement <
short_strike): the short is assigned at its strike, the wing expires
worthless, and share cost basis is short_strike - entry credit. If
settlement falls at or below the wing strike too, both legs are ITM and
the spread instead settles at its already-defined max loss, in cash. This
is the one place the spec's prose ("buy a wing ... so max loss is
defined" + "if assigned ... convert to long shares") is underspecified: a
real put CREDIT SPREAD does not assign as cleanly as a naked
cash-secured put once the long wing is also ITM, so here the wing's
defined-risk guarantee takes precedence over completing the wheel -- a
gap-down never turns into an involuntary share purchase priced well above
the market.

This module owns entry selection and expiry settlement only (mirroring
credit.py's scope). Day-to-day mark-to-market management -- profit
target, time stop, and the spec's 1.5x-credit hard stop -- is a
multi-position backtest-loop concern like backtest/managed.py's
_check_exit, not implemented here. Note that hard stop does not exist
anywhere in this codebase yet for ANY strategy (managed.py only has a
breach-based stop, which the README's own sweep found net-negative and
ships disabled); adding it belongs to whatever orchestrator drives S1/S2/S3
together, so it is applied uniformly rather than once per strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls

import pandas as pd

from .credit import (
    CreditPosition, CreditVariantConfig, build_position,
    _pick_short, _with_deltas,
)


@dataclass
class WheelConfig:
    """Every knob for S1_WHEEL_CORE, matching the small-account spec."""
    name: str = "wheel_core"
    # --- Phase A: put credit spread ---
    put_delta_target: float = 0.25          # spec: 0.20-0.30
    put_wing_width_frac: float = 0.02       # snapped to nearest listed strike
    put_min_dte: int = 7
    put_max_dte: int = 45
    put_min_credit_frac: float = 0.08       # spec: 8% of width (or $0.15 on $1-wide)
    put_min_short_bid: float = 0.05
    # Phase A profit-take, as a fraction of entry credit captured -- mirrors
    # CreditVariantConfig's field of the same name (spec section 11's
    # 40%/50%/70% sensitivity sweep needs this exposed per-phase rather than
    # hardcoded to phase_a_variant()'s CreditVariantConfig default).
    put_profit_take_frac: float = 0.50
    # Deliberately NOT the spec's shared 21-DTE time stop (see
    # phase_a_variant's docstring): ANY positive threshold closes the
    # spread in cash strictly before day == expiration, and
    # managed.py._check_exit only ever routes into settlement (this
    # module's assignment/max_loss split) when day >= expiration -- so a
    # 21-DTE, or even a 1-DTE, time stop makes assignment structurally
    # unreachable, contradicting the wheel's own design ("if assigned...
    # convert to long shares"). 0/0.0 means the threshold is always 0:
    # Phase A holds to actual expiration (or an earlier profit-target /
    # hard-stop) rather than time-exiting early, the way S2/S3 want to.
    put_time_exit_dte: int = 0
    put_time_exit_frac: float = 0.0
    # --- Phase B: covered call ---
    call_delta_target: float = 0.25         # spec: 0.20-0.30
    call_min_short_bid: float = 0.05
    # Stop selling calls (but keep the shares) once underwater this much
    # from cost basis -- spec: "if stock drops 12% from entry."
    drop_from_basis_halt_pct: float = 0.12
    # --- Phase B management (mirrors CreditVariantConfig's fields; a
    # covered call isn't a CreditPosition -- shares are the hedge, not a
    # wing -- so it needs its own copies rather than reusing that class).
    # Checked by the orchestrator (backtest/portfolio.py), not this module.
    call_profit_take_frac: float = 0.50
    # Same reasoning as put_time_exit_dte/frac above: a positive threshold
    # closes the call in cash before expiration and the wheel never learns
    # whether it would have been called away or expired worthless. 0/0.0
    # lets Phase B run to actual settlement by default.
    call_time_exit_dte: int = 0
    call_time_exit_frac: float = 0.0
    # --- Shared hard stop (spec: close at 1.5x credit unrealized loss).
    # Applied to BOTH phases: Phase A via phase_a_variant()'s
    # CreditVariantConfig field of the same name, Phase B directly.
    hard_stop_credit_mult: float | None = 1.5
    # --- Dividend guard (F): skip a call this close to ex-div if its delta
    # is deep enough that early-exercise risk (thin remaining time value)
    # is real.
    ex_div_skip_days: int = 3
    ex_div_skip_call_delta: float = 0.35

    def phase_a_variant(self) -> CreditVariantConfig:
        """Builds Phase A's CreditVariantConfig with put_time_exit_dte/frac
        in place of CreditVariantConfig's own 21-DTE default, and
        exit_on_breach forced off.

        Both overrides exist for the same reason: CreditVariantConfig's
        defaults are written for a strategy that wants to AVOID ever going
        ITM (S2/S3). The wheel's whole mechanism is built on going ITM
        sometimes -- "if assigned... convert to long shares" -- so a
        breach exit (closes for cash the moment spot crosses the short
        strike, well before expiration) or an early time exit would both
        short-circuit assignment before it can ever happen. Confirmed
        empirically: with breach left on, a 5.7-year, 230-trade backtest
        produced zero 'assigned' and zero 'called_away' outcomes -- the
        wheel was silently behaving as a plain put credit spread. The
        hard stop (1.5x credit) still protects against a real gap-down;
        it just doesn't fire on an ordinary test of the strike the way
        breach does.
        """
        return CreditVariantConfig(
            name=f"{self.name}_put", short_put_delta=self.put_delta_target,
            short_call_delta=None, wing_width_frac=self.put_wing_width_frac,
            min_dte=self.put_min_dte, max_dte=self.put_max_dte,
            min_credit_frac=self.put_min_credit_frac,
            min_short_bid=self.put_min_short_bid,
            profit_take_frac=self.put_profit_take_frac,
            hard_stop_credit_mult=self.hard_stop_credit_mult,
            time_exit_dte=self.put_time_exit_dte,
            time_exit_frac=self.put_time_exit_frac,
            exit_on_breach=False,
        )


def build_phase_a(chain: pd.DataFrame, spot: float, underlying: str,
                  entry_date: str, expiration: str, dte: int,
                  cfg: WheelConfig,
                  slippage_half_spread_frac: float = 0.5) -> CreditPosition | None:
    """Phase A entry: delegates entirely to credit.py's generic put-spread
    builder with a WheelConfig-derived variant."""
    return build_position(chain, spot, underlying, entry_date, expiration,
                          dte, cfg.phase_a_variant(), slippage_half_spread_frac)


@dataclass
class ShareLot:
    underlying: str
    shares: int
    cost_basis: float       # per share, net of the assigning spread's credit
    acquired_date: str


def resolve_put_spread_settlement(
    pos: CreditPosition, settlement_price: float, contracts: int = 1,
) -> tuple[str, float, ShareLot | None]:
    """Settle a Phase A spread at expiry.

    `contracts` is how many spreads were open -- assignment is per
    contract, so it sizes the returned lot's share count (100 * contracts).

    Returns (outcome, cash_pnl_per_share, lot):
      'expired'    spot >= short strike -- spread worthless, cash_pnl is
                   the entry credit (already the position's only P&L).
      'assigned'   wing_strike < spot < short_strike -- short is assigned,
                   wing expires worthless. cash_pnl_per_share is 0.0: the
                   trade's economics move into the returned lot's cost
                   basis rather than a cash P&L line (buying the shares is
                   a balance-sheet event, not realized P&L, until sold).
      'max_loss'   spot <= wing_strike -- both legs ITM, the spread
                   settles at its predefined max loss instead of assigning.
    """
    short_strike = pos.short_put_strike
    wing_strike = next(l.strike for l in pos.legs
                       if l.type == "put" and l.side == +1)
    if settlement_price >= short_strike:
        return "expired", pos.credit, None
    if settlement_price > wing_strike:
        lot = ShareLot(
            underlying=pos.underlying, shares=100 * contracts,
            cost_basis=round(short_strike - pos.credit, 4),
            acquired_date=pos.expiration,
        )
        return "assigned", 0.0, lot
    return "max_loss", round(pos.credit - pos.max_loss, 4), None


@dataclass
class CoveredCallLeg:
    strike: float
    entry_bid: float
    entry_ask: float
    entry_delta: float
    entry_iv: float = 0.0

    @property
    def entry_mid(self) -> float:
        return (self.entry_bid + self.entry_ask) / 2.0

    @property
    def entry_half_spread(self) -> float:
        return (self.entry_ask - self.entry_bid) / 2.0


@dataclass
class CoveredCallPosition:
    underlying: str
    entry_date: str
    expiration: str
    dte_at_entry: int
    spot_at_entry: float
    lot: ShareLot
    leg: CoveredCallLeg
    credit_mid: float = 0.0
    credit: float = 0.0


def blocked_by_ex_dividend(call_delta: float, entry_date: str,
                          ex_div_date: str | None, cfg: WheelConfig) -> bool:
    """True if selling this call risks early assignment across an ex-div
    date the spec wants avoided: ex-div falls within ex_div_skip_days of
    entry AND the call is deep enough (delta over threshold) that its
    remaining time value is thin relative to the dividend."""
    if ex_div_date is None or abs(call_delta) <= cfg.ex_div_skip_call_delta:
        return False
    days_to_exdiv = (date_cls.fromisoformat(ex_div_date)
                     - date_cls.fromisoformat(entry_date)).days
    return 0 <= days_to_exdiv <= cfg.ex_div_skip_days


def build_phase_b(chain: pd.DataFrame, spot: float, underlying: str,
                  entry_date: str, expiration: str, dte: int,
                  lot: ShareLot, cfg: WheelConfig,
                  ex_div_date: str | None = None,
                  slippage_half_spread_frac: float = 0.5,
                  ) -> CoveredCallPosition | None:
    """Phase B entry: sell one call per 100-share lot. No wing -- the
    shares are the hedge, unlike credit.py's option-only spreads. Returns
    None (stay in shares, uncovered) if the drop-from-basis halt or the
    ex-dividend guard blocks selling a call this cycle."""
    if lot.cost_basis > 0 and \
            (spot - lot.cost_basis) / lot.cost_basis <= -cfg.drop_from_basis_halt_pct:
        return None
    t_years = max(dte, 1) / 365.0
    side = chain[chain["type"] == "call"]
    if side.empty:
        return None
    side = _with_deltas(side, "call", spot, t_years)
    short = _pick_short(side, cfg.call_delta_target, cfg.call_min_short_bid)
    if short is None:
        return None
    entry_delta = float(short.get("delta", 0.0) or 0.0)
    if blocked_by_ex_dividend(entry_delta, entry_date, ex_div_date, cfg):
        return None
    leg = CoveredCallLeg(
        strike=float(short["strike"]), entry_bid=float(short["bid"]),
        entry_ask=float(short["ask"]), entry_delta=entry_delta,
        entry_iv=float(short.get("iv", 0.0) or 0.0),
    )
    credit_mid = leg.entry_mid
    credit = credit_mid - slippage_half_spread_frac * leg.entry_half_spread
    if credit <= 0:
        return None
    return CoveredCallPosition(
        underlying=underlying, entry_date=entry_date, expiration=expiration,
        dte_at_entry=dte, spot_at_entry=spot, lot=lot, leg=leg,
        credit_mid=round(credit_mid, 4), credit=round(credit, 4),
    )


def resolve_covered_call_settlement(
    pos: CoveredCallPosition, settlement_price: float,
) -> tuple[str, float]:
    """Settle a Phase B call at expiry.

    Returns (outcome, cash_pnl_per_share):
      'called_away'  spot >= strike -- realizes the call credit plus the
                     capital gain/loss on the shares (strike - cost_basis).
      'expired'      spot < strike -- realizes just the call credit; the
                     shares stay held for another Phase B cycle.
    """
    if settlement_price >= pos.leg.strike:
        gain = pos.credit + (pos.leg.strike - pos.lot.cost_basis)
        return "called_away", round(gain, 4)
    return "expired", pos.credit
