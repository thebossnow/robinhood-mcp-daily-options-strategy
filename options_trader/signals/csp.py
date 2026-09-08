"""Cash-secured put candidate selection.

Single-leg, cash-secured only — no naked puts (this pipeline never sizes
past available collateral), no spreads. Selection is mechanical: a delta
window, a DTE window, a liquidity/premium floor. There is no model-EV
filter the way the debit-spread scanner has one; the edge claim is the
same one the credit-spread pipeline makes (variance risk premium, tested
against paper-trading results), not a probability model scoring the
market's own prices.

Reuses signals/credit.py's bs_delta (Black-Scholes fallback for chains
that don't quote a delta column) rather than duplicating it.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import pandas as pd

from .credit import bs_delta


@dataclass
class CSPLiquidityRules:
    min_open_interest: int = 500
    min_volume: int = 100
    max_spread_pct: float = 0.10
    min_premium: float = 0.15
    require_nonzero_bid: bool = True


def _usable_quote(row: pd.Series, rules: CSPLiquidityRules) -> bool:
    bid, ask = float(row["bid"]), float(row["ask"])
    if ask <= 0 or ask < bid:
        return False
    if rules.require_nonzero_bid and bid <= 0:
        return False
    mid = (bid + ask) / 2.0
    if mid < rules.min_premium:
        return False
    if mid <= 0:
        return False
    if (ask - bid) / mid > rules.max_spread_pct:
        return False
    oi = int(row.get("open_interest", 0) or 0)
    vol = int(row.get("volume", 0) or 0)
    return oi >= rules.min_open_interest or vol >= rules.min_volume


def _with_abs_delta(puts: pd.DataFrame, spot: float, t_years: float) -> pd.DataFrame:
    puts = puts.copy()
    if "delta" in puts.columns and puts["delta"].abs().sum() > 0:
        puts["abs_delta"] = puts["delta"].abs()
    else:
        puts["abs_delta"] = puts.apply(
            lambda r: abs(bs_delta("put", spot, float(r["strike"]),
                                   float(r["iv"]), t_years)), axis=1)
    return puts


@dataclass
class CSPCandidate:
    underlying: str
    expiration: str
    dte: int
    spot_at_entry: float
    strike: float
    delta: float
    otm_pct: float          # (spot - strike) / spot
    bid: float
    ask: float
    premium_mid: float      # per share, at mid
    premium: float          # per share, after entry slippage
    open_interest: int
    volume: int
    collateral: float       # per contract, dollars (strike * 100)
    roc: float               # premium / strike, per contract
    annualized_roc: float    # roc * (365 / dte)

    def to_dict(self) -> dict:
        return asdict(self)


def select_put(chain: pd.DataFrame, spot: float, dte: int,
               delta_min: float, delta_max: float, delta_target: float,
               rules: CSPLiquidityRules = CSPLiquidityRules(),
               min_otm_pct: float = 0.0) -> pd.Series | None:
    """The put whose |delta| falls in [delta_min, delta_max], closest to
    delta_target, from the strikes that pass liquidity + premium. Returns
    None if nothing qualifies.

    `chain` columns: type, strike, bid, ask, volume, open_interest, iv, and
    optionally delta. `min_otm_pct` additionally requires the strike be at
    least that fraction below spot (used for QQQ ladder rungs).
    """
    puts = chain[chain["type"] == "put"]
    if puts.empty:
        return None
    t_years = max(dte, 1) / 365.0
    puts = _with_abs_delta(puts, spot, t_years)

    ok = puts[(puts["abs_delta"] >= delta_min) & (puts["abs_delta"] <= delta_max)]
    if min_otm_pct > 0 and spot > 0:
        ok = ok[(spot - ok["strike"]) / spot >= min_otm_pct]
    ok = ok[ok.apply(lambda r: _usable_quote(r, rules), axis=1)]
    if ok.empty:
        return None
    return ok.loc[(ok["abs_delta"] - delta_target).abs().idxmin()]


def build_candidate(chain: pd.DataFrame, spot: float, underlying: str,
                    expiration: str, dte: int,
                    delta_min: float, delta_max: float, delta_target: float,
                    rules: CSPLiquidityRules = CSPLiquidityRules(),
                    slippage_half_spread_frac: float = 0.5,
                    min_otm_pct: float = 0.0) -> CSPCandidate | None:
    """Build one CSP candidate from a single-expiration chain, or None if no
    strike qualifies. Premium pays entry slippage (a fraction of the
    half-spread), matching the rest of the pipeline's fill model."""
    row = select_put(chain, spot, dte, delta_min, delta_max, delta_target,
                     rules, min_otm_pct)
    if row is None:
        return None

    bid, ask = float(row["bid"]), float(row["ask"])
    mid = (bid + ask) / 2.0
    half_spread = (ask - bid) / 2.0
    premium = round(mid - slippage_half_spread_frac * half_spread, 4)
    if premium <= 0:
        return None

    strike = float(row["strike"])
    collateral = strike * 100.0
    roc = premium * 100.0 / collateral if collateral > 0 else 0.0
    annualized_roc = roc * (365.0 / max(dte, 1))

    return CSPCandidate(
        underlying=underlying,
        expiration=expiration,
        dte=dte,
        spot_at_entry=spot,
        strike=strike,
        delta=round(float(row["abs_delta"]), 4),
        otm_pct=round((spot - strike) / spot, 4) if spot > 0 else 0.0,
        bid=bid,
        ask=ask,
        premium_mid=round(mid, 4),
        premium=premium,
        open_interest=int(row.get("open_interest", 0) or 0),
        volume=int(row.get("volume", 0) or 0),
        collateral=round(collateral, 2),
        roc=round(roc, 5),
        annualized_roc=round(annualized_roc, 4),
    )
