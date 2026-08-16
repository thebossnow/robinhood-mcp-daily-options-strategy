"""Candidate generation: long straddles and strangles (volatility bets).

Structure: buy 1 call + 1 put, same expiration, no short legs. Payoff at
expiry: max(0, S_T - call_strike) + max(0, put_strike - S_T) - debit. Unlike
the defined-risk verticals in candidates.py, max loss is the debit paid but
there is no width cap on the winning side.

`strangle_width_frac=0.0` (the default) picks the strike nearest spot on
each side — an ATM straddle. A positive value pushes each leg out-of-the-
money by that fraction of spot (a strangle): cheaper debit, needs a bigger
move to profit.

Deliberately mechanical, no BS-EV filter (unlike candidates.py): pricing
both legs off their own quoted IV and comparing to a BS-model fair value
would be close to tautological here (there's no long/short IV mismatch to
exploit, unlike a vertical spread's skew). The real question this strategy
answers is empirical — does realized volatility clear the debit paid often
enough to profit — which is exactly what the historical backtest measures.
Selling premium (credit.py) is documented there as benefiting from the
volatility risk premium (implied usually running above realized); buying
premium here is the mirror bet, fighting that same premium.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import pandas as pd

from ..data.provider import ChainSnapshot


@dataclass
class StraddleVariantConfig:
    """One tradeable variant."""
    name: str
    strangle_width_frac: float = 0.0   # 0 = straddle; >0 = OTM strangle
    min_dte: int = 5
    max_dte: int = 14
    target_dte: int = 7
    min_open_interest: int = 500
    min_volume: int = 50
    max_spread_pct: float = 0.15
    min_debit: float = 0.10
    max_debit: float = 50.0
    slippage_half_spread_frac: float = 0.5

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StraddleCandidate:
    underlying: str
    expiration: str
    dte: int
    kind: str                  # 'straddle' | 'strangle'
    call_strike: float
    put_strike: float
    debit: float                # net mid debit per share (both legs, before slippage)
    max_loss: float              # per contract, dollars (= debit * 100)
    breakeven_low: float
    breakeven_high: float
    spot_at_entry: float
    call_leg: dict
    put_leg: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _mid(row: pd.Series) -> float:
    return (row["bid"] + row["ask"]) / 2.0


def _leg_ok(row: pd.Series, cfg: StraddleVariantConfig) -> bool:
    if row["bid"] <= 0 or row["ask"] <= 0 or row["ask"] < row["bid"]:
        return False
    if row["open_interest"] < cfg.min_open_interest:
        return False
    if row["volume"] < cfg.min_volume:
        return False
    mid = _mid(row)
    if mid <= 0:
        return False
    if (row["ask"] - row["bid"]) / mid > cfg.max_spread_pct:
        return False
    return True


def _leg_info(row: pd.Series) -> dict:
    return {
        "strike": float(row["strike"]),
        "bid": float(row["bid"]),
        "ask": float(row["ask"]),
        "mid": round(_mid(row), 4),
        "volume": int(row["volume"]),
        "open_interest": int(row["open_interest"]),
        "iv": float(row["iv"]),
    }


def _nearest_strike(side: pd.DataFrame, target: float) -> pd.Series | None:
    if side.empty:
        return None
    idx = (side["strike"] - target).abs().idxmin()
    return side.loc[idx]


def generate_straddle_candidate(snap: ChainSnapshot,
                                cfg: StraddleVariantConfig) -> StraddleCandidate | None:
    """At most one candidate per snapshot: a ChainSnapshot is already scoped
    to a single (underlying, expiration), so there's one ATM/strangle choice
    per side, not a combinatorial search like the vertical-spread scanner."""
    if not (cfg.min_dte <= snap.dte <= cfg.max_dte):
        return None

    calls = snap.chain[snap.chain["type"] == "call"]
    puts = snap.chain[snap.chain["type"] == "put"]
    if calls.empty or puts.empty:
        return None

    call_target = snap.spot * (1.0 + cfg.strangle_width_frac)
    put_target = snap.spot * (1.0 - cfg.strangle_width_frac)
    call_row = _nearest_strike(calls, call_target)
    put_row = _nearest_strike(puts, put_target)
    if call_row is None or put_row is None:
        return None
    if not _leg_ok(call_row, cfg) or not _leg_ok(put_row, cfg):
        return None

    debit = _mid(call_row) + _mid(put_row)
    if debit < cfg.min_debit or debit > cfg.max_debit:
        return None

    kind = "straddle" if cfg.strangle_width_frac == 0.0 else "strangle"
    call_strike, put_strike = float(call_row["strike"]), float(put_row["strike"])
    return StraddleCandidate(
        underlying=snap.underlying,
        expiration=snap.expiration,
        dte=snap.dte,
        kind=kind,
        call_strike=call_strike,
        put_strike=put_strike,
        debit=round(debit, 4),
        max_loss=round(debit * 100.0, 2),
        breakeven_low=round(put_strike - debit, 4),
        breakeven_high=round(call_strike + debit, 4),
        spot_at_entry=snap.spot,
        call_leg=_leg_info(call_row),
        put_leg=_leg_info(put_row),
    )
