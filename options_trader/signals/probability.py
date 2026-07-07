"""Risk-neutral probability estimates from Black-Scholes.

P(S_T > K) under the risk-neutral measure is N(d2). This is a model, not
truth: it inherits whatever the market's implied volatility says and assumes
lognormal terminal prices. It is, however, an *honest input* to expected
value — unlike asserting "target = 2x premium" and calling that risk/reward.

Risk-neutral probabilities slightly understate upside drift in bull markets
and ignore fat tails; treat EV computed from them as an estimate with error
bars, which is exactly why the pipeline also demands a costs buffer.
"""

from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(spot: float, strike: float, iv: float, dte_years: float,
               rate: float = 0.0) -> float:
    """P(S_T > strike) = N(d2). Returns a degenerate 0/1 answer if the
    option is effectively expired or IV is missing."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if dte_years <= 0 or iv <= 0:
        return 1.0 if spot > strike else 0.0
    sigma_sqrt_t = iv * math.sqrt(dte_years)
    d2 = (math.log(spot / strike) + (rate - 0.5 * iv * iv) * dte_years) / sigma_sqrt_t
    return _norm_cdf(d2)


def prob_below(spot: float, strike: float, iv: float, dte_years: float,
               rate: float = 0.0) -> float:
    return 1.0 - prob_above(spot, strike, iv, dte_years, rate)
