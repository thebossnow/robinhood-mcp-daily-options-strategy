"""Shared math utilities for options strategy.

Extracted/adapted from PR#3 improvements for reusability across
vertical and single-leg paths. Everything is pure and unit-testable.

Key additions from PR#3:
- expected_move from ATM straddle (useful strike filter)
- prob_touch approximation (~2x ITM, capped)
- LiquidityRules dataclass + helpers with premium band
- Clean mid/spread_pct (inf for bad quotes)
- Trade EV and sizing helpers

These complement (do not replace) the vertical-specific EV in candidates.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import erf, log, sqrt

from ..config import StrategyConfig

TRADING_DAYS_PER_YEAR = 252.0  # trading days for vol scaling


def mid_price(bid: float, ask: float) -> float:
    """Midpoint price. Returns 0.0 when the quote is unusable."""
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return 0.0
    return (bid + ask) / 2.0


def spread_pct(bid: float, ask: float) -> float:
    """Bid/ask spread as fraction of mid. Returns inf for unusable quotes."""
    mid = mid_price(bid, ask)
    if mid <= 0:
        return float("inf")
    return (ask - bid) / mid


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def prob_itm(option_type: str, spot: float, strike: float, iv: float,
             t_years: float, rate: float = 0.0) -> float:
    """Risk-neutral P(ITM at expiry) using N(d2) or N(-d2)."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    v_sqrt_t = iv * sqrt(t_years)
    d2 = (log(spot / strike) + (rate - 0.5 * iv * iv) * t_years) / v_sqrt_t
    if option_type == "call":
        return _norm_cdf(d2)
    if option_type == "put":
        return _norm_cdf(-d2)
    raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")


def prob_touch(option_type: str, spot: float, strike: float, iv: float,
               t_years: float) -> float:
    """Approximate probability the underlying touches the strike before expiry.

    Rough rule of thumb: ~2x the ITM probability, capped at 95%.
    Use as an optimistic proxy when modeling "will we reach target".
    """
    return min(2.0 * prob_itm(option_type, spot, strike, iv, t_years), 0.95)


def expected_move(spot: float, atm_call_mid: float, atm_put_mid: float) -> float:
    """1-sigma expected move implied by ATM straddle (~0.85 * straddle)."""
    straddle = atm_call_mid + atm_put_mid
    if spot <= 0 or straddle <= 0:
        return 0.0
    return 0.85 * straddle


def trade_ev(p_win: float, target_gain: float, max_loss: float) -> float:
    """Simple EV per contract: p_win * gain - (1-p_win) * loss."""
    return p_win * target_gain - (1.0 - p_win) * max_loss


def max_contracts(account_value: float, risk_pct: float,
                  max_loss_per_contract: float) -> int:
    """How many contracts fit inside risk_pct of account equity."""
    if max_loss_per_contract <= 0 or account_value <= 0:
        return 0
    return int((account_value * risk_pct) / max_loss_per_contract)


@dataclass
class LiquidityRules:
    """Configurable liquidity + premium filters.

    These are useful for both vertical legs and single-leg trades.
    """
    min_open_interest: int = 500
    min_volume: int = 100
    max_spread_pct: float = 0.10
    min_premium: float = 0.30      # per share; dust options get eaten by spread
    max_premium: float = 5.00      # per share; keeps position size reasonable


def passes_liquidity(bid: float, ask: float, open_interest: int, volume: int,
                     rules: LiquidityRules = LiquidityRules()) -> bool:
    """True if the contract meets liquidity + premium band rules.

    OI *or* volume is sufficient (one can carry the other), but spread
    and premium bounds are hard.
    """
    mid = mid_price(bid, ask)
    if mid < rules.min_premium or mid > rules.max_premium:
        return False
    if spread_pct(bid, ask) > rules.max_spread_pct:
        return False
    return (open_interest or 0) >= rules.min_open_interest or \
           (volume or 0) >= rules.min_volume


# --- Example: single-leg TradePlan (from PR#3, kept for MCP single-leg path) ---

from dataclasses import dataclass as _dc, field as _field


@_dc
class SingleLegTradePlan:
    ticker: str
    expiration: str
    option_type: str
    strike: float
    entry_mid: float
    debit: float
    profit_target: float
    stop_loss: float
    p_win: float
    ev_per_contract: float
    contracts: int
    open_interest: int
    volume: int
    spread: float


def build_single_leg_plan(
    ticker: str,
    expiration: str,
    option_type: str,
    strike: float,
    bid: float,
    ask: float,
    iv: float,
    spot: float,
    days_to_expiry: float,
    open_interest: int,
    volume: int,
    account_value: float,
    risk_pct: float = 0.02,
    target_gain_pct: float = 1.0,
    stop_loss_pct: float = 0.5,
    rules: LiquidityRules = LiquidityRules(),
) -> SingleLegTradePlan | None:
    """Build a single-leg plan using PR#3-style rules.

    p_win uses prob_touch as optimistic proxy for hitting +100% target.
    Returns None if any filter (liquidity, EV, sizing) fails.
    """
    if not passes_liquidity(bid, ask, open_interest, volume, rules):
        return None

    mid = mid_price(bid, ask)
    t_years = max(days_to_expiry, 0.25) / TRADING_DAYS_PER_YEAR
    p_win = prob_touch(option_type, spot, strike, iv, t_years)

    debit = mid * 100.0
    target_gain = debit * target_gain_pct
    max_loss = debit * stop_loss_pct
    ev = trade_ev(p_win, target_gain, max_loss)
    if ev <= 0:
        return None

    contracts = max_contracts(account_value, risk_pct, max_loss)
    if contracts < 1:
        return None

    return SingleLegTradePlan(
        ticker=ticker,
        expiration=expiration,
        option_type=option_type,
        strike=strike,
        entry_mid=round(mid, 2),
        debit=round(debit, 2),
        profit_target=round(mid * (1 + target_gain_pct), 2),
        stop_loss=round(mid * (1 - stop_loss_pct), 2),
        p_win=round(p_win, 3),
        ev_per_contract=round(ev, 2),
        contracts=contracts,
        open_interest=open_interest,
        volume=volume,
        spread=round(spread_pct(bid, ask), 3),
    )
