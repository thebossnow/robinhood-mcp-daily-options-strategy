"""Covered-call candidate selection, for shares acquired via CSP assignment.

Same delta/liquidity discipline as the CSP scanner (0.05-0.15 delta), with
one extra constraint the user's rules require: strike at or above cost
basis when possible, so an assignment doesn't lock in a loss on top of the
premium already collected. When no strike at/above cost basis clears the
delta window, the nearest-to-cost-basis qualifying strike below it is
returned instead (still inside the delta window) — a covered call is
better than idle shares even if it caps below cost basis; scan_csp.py/
manage_csp.py surface which case happened so a human can veto it.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import pandas as pd

from .credit import bs_delta
from .csp import CSPLiquidityRules, _usable_quote


@dataclass
class CoveredCallCandidate:
    underlying: str
    expiration: str
    dte: int
    spot_at_entry: float
    strike: float
    delta: float
    at_or_above_cost_basis: bool
    cost_basis: float
    bid: float
    ask: float
    premium_mid: float
    premium: float
    open_interest: int
    volume: int
    roc: float               # premium / cost_basis, per contract

    def to_dict(self) -> dict:
        return asdict(self)


def _with_abs_delta(calls: pd.DataFrame, spot: float, t_years: float) -> pd.DataFrame:
    calls = calls.copy()
    if "delta" in calls.columns and calls["delta"].abs().sum() > 0:
        calls["abs_delta"] = calls["delta"].abs()
    else:
        calls["abs_delta"] = calls.apply(
            lambda r: abs(bs_delta("call", spot, float(r["strike"]),
                                   float(r["iv"]), t_years)), axis=1)
    return calls


def build_candidate(chain: pd.DataFrame, spot: float, underlying: str,
                    expiration: str, dte: int, cost_basis: float,
                    delta_min: float, delta_max: float, delta_target: float,
                    rules: CSPLiquidityRules = CSPLiquidityRules(),
                    slippage_half_spread_frac: float = 0.5,
                    ) -> CoveredCallCandidate | None:
    calls = chain[chain["type"] == "call"]
    if calls.empty:
        return None
    t_years = max(dte, 1) / 365.0
    calls = _with_abs_delta(calls, spot, t_years)

    ok = calls[(calls["abs_delta"] >= delta_min) & (calls["abs_delta"] <= delta_max)]
    ok = ok[ok.apply(lambda r: _usable_quote(r, rules), axis=1)]
    if ok.empty:
        return None

    above = ok[ok["strike"] >= cost_basis]
    pool = above if not above.empty else ok
    at_or_above = not above.empty
    row = pool.loc[(pool["abs_delta"] - delta_target).abs().idxmin()]

    bid, ask = float(row["bid"]), float(row["ask"])
    mid = (bid + ask) / 2.0
    half_spread = (ask - bid) / 2.0
    premium = round(mid - slippage_half_spread_frac * half_spread, 4)
    if premium <= 0:
        return None

    roc = premium * 100.0 / (cost_basis * 100.0) if cost_basis > 0 else 0.0

    return CoveredCallCandidate(
        underlying=underlying, expiration=expiration, dte=dte,
        spot_at_entry=spot, strike=float(row["strike"]),
        delta=round(float(row["abs_delta"]), 4),
        at_or_above_cost_basis=at_or_above, cost_basis=cost_basis,
        bid=bid, ask=ask, premium_mid=round(mid, 4), premium=premium,
        open_interest=int(row.get("open_interest", 0) or 0),
        volume=int(row.get("volume", 0) or 0), roc=round(roc, 5),
    )
