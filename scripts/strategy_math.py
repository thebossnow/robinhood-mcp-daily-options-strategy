"""Pure math for the daily options strategy: pricing sanity, probability
estimates, expectancy, and position sizing.

No network or broker dependencies — everything here is unit-testable.
The probability model is Black-Scholes risk-neutral (N(d2) for P(ITM at
expiry), doubled and capped for P(touch)). These are estimates, not
guarantees; their job is to reject trades whose implied odds can't
support the target, not to predict winners.
"""

from dataclasses import dataclass
from math import erf, log, sqrt

TRADING_DAYS_PER_YEAR = 252.0


def mid_price(bid: float, ask: float) -> float:
    """Midpoint price. Returns 0.0 when the quote is unusable."""
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return 0.0
    return (bid + ask) / 2.0


def spread_pct(bid: float, ask: float) -> float:
    """Bid/ask spread as a fraction of mid. Returns inf for unusable quotes
    so callers filtering on 'spread <= cap' reject them naturally."""
    mid = mid_price(bid, ask)
    if mid <= 0:
        return float("inf")
    return (ask - bid) / mid


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _d1_d2(spot: float, strike: float, iv: float, t_years: float,
           rate: float = 0.0) -> tuple:
    v_sqrt_t = iv * sqrt(t_years)
    d1 = (log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / v_sqrt_t
    return d1, d1 - v_sqrt_t


def prob_itm(option_type: str, spot: float, strike: float, iv: float,
             t_years: float, rate: float = 0.0) -> float:
    """Risk-neutral probability the option expires in the money (N(d2)
    for calls, N(-d2) for puts)."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    _, d2 = _d1_d2(spot, strike, iv, t_years, rate)
    if option_type == "call":
        return _norm_cdf(d2)
    if option_type == "put":
        return _norm_cdf(-d2)
    raise ValueError(f"option_type must be 'call' or 'put', got {option_type!r}")


def prob_touch(option_type: str, spot: float, strike: float, iv: float,
               t_years: float) -> float:
    """Probability the underlying touches the strike before expiry.
    Standard approximation: ~2x the probability of expiring ITM, capped."""
    return min(2.0 * prob_itm(option_type, spot, strike, iv, t_years), 0.95)


def expected_move(spot: float, atm_call_mid: float, atm_put_mid: float) -> float:
    """Expected move (in underlying dollars) implied by the ATM straddle
    price. ~85% of the straddle is the conventional 1-sigma estimate."""
    straddle = atm_call_mid + atm_put_mid
    if spot <= 0 or straddle <= 0:
        return 0.0
    return 0.85 * straddle


def trade_ev(p_win: float, target_gain: float, max_loss: float) -> float:
    """Expected value per contract given win probability, the dollar gain
    at target, and the dollar loss at stop."""
    return p_win * target_gain - (1.0 - p_win) * max_loss


def max_contracts(account_value: float, risk_pct: float,
                  max_loss_per_contract: float) -> int:
    """Contracts allowed so total risk stays within risk_pct of account."""
    if max_loss_per_contract <= 0 or account_value <= 0:
        return 0
    return int((account_value * risk_pct) / max_loss_per_contract)


@dataclass
class LiquidityRules:
    min_open_interest: int = 500
    min_volume: int = 100
    max_spread_pct: float = 0.10   # spread must be <= 10% of mid
    min_premium: float = 0.30      # per share; below this, spread drag dominates
    max_premium: float = 5.00      # per share; keeps defined risk small


def passes_liquidity(bid: float, ask: float, open_interest: int, volume: int,
                     rules: LiquidityRules = LiquidityRules()) -> bool:
    """True when the quote is tradable under the liquidity rules. OI *or*
    volume may carry the liquidity test, but the spread and premium
    bounds are hard requirements."""
    mid = mid_price(bid, ask)
    if mid < rules.min_premium or mid > rules.max_premium:
        return False
    if spread_pct(bid, ask) > rules.max_spread_pct:
        return False
    return (open_interest or 0) >= rules.min_open_interest or \
           (volume or 0) >= rules.min_volume


@dataclass
class TradePlan:
    """A fully-specified candidate: entry, exits, sizing, and expectancy."""
    ticker: str
    expiration: str
    option_type: str
    strike: float
    entry_mid: float
    debit: float            # dollars per contract (mid * 100)
    profit_target: float    # close at this option price
    stop_loss: float        # close at this option price
    p_win: float
    ev_per_contract: float
    contracts: int
    open_interest: int
    volume: int
    spread: float


def build_trade_plan(ticker: str, expiration: str, option_type: str,
                     strike: float, bid: float, ask: float, iv: float,
                     spot: float, days_to_expiry: float,
                     open_interest: int, volume: int,
                     account_value: float,
                     risk_pct: float = 0.02,
                     target_gain_pct: float = 1.0,
                     stop_loss_pct: float = 0.5,
                     rules: LiquidityRules = LiquidityRules()):
    """Evaluate one contract against every rule. Returns a TradePlan when
    the trade passes liquidity, has positive expectancy, and the account
    can size at least one contract within risk_pct — otherwise None.

    Default management: take profit at +100% of debit, stop at -50%.
    p_win uses probability-of-touch on the strike as the (optimistic)
    proxy for reaching the profit target, so a trade that fails EV here
    fails under generous assumptions.
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

    return TradePlan(
        ticker=ticker, expiration=expiration, option_type=option_type,
        strike=strike, entry_mid=round(mid, 2), debit=round(debit, 2),
        profit_target=round(mid * (1 + target_gain_pct), 2),
        stop_loss=round(mid * (1 - stop_loss_pct), 2),
        p_win=round(p_win, 3), ev_per_contract=round(ev, 2),
        contracts=contracts, open_interest=open_interest, volume=volume,
        spread=round(spread_pct(bid, ask), 3),
    )
