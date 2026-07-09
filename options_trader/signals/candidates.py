"""Candidate generation: vertical debit spreads with expected-value scoring.

Structures: bull call spreads (buy K1 call, sell K2 call, K1 < K2) and
bear put spreads (buy K2 put, sell K1 put). Defined risk on both sides —
max loss is the debit paid, full stop.

EV model (per contract, x100 multiplier applied at the end):
    win region   — underlying beyond the short strike: payoff = width - debit
    loss region  — underlying not past the long strike: payoff = -debit
    middle       — between strikes: approximated as width/2 - debit
    EV = p_win*(width - debit) + p_mid*(width/2 - debit) - p_loss*debit

Probabilities come from Black-Scholes N(d2) using each leg's implied vol
(see signals/probability.py for the caveats). The middle-region payoff
approximation is crude but conservative-ish for OTM debit spreads, where
the true conditional payoff is below width/2.

Costs: entry and exit each pay a configurable fraction of each leg's
half-spread. A candidate must have EV > 0 *after* these costs to surface.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from itertools import combinations

import pandas as pd

from ..config import StrategyConfig
from ..data.provider import ChainSnapshot
from .probability import prob_above, prob_below
from .math import expected_move, mid_price  # PR#3 utilities for EM filter + better mids

TRADING_DAYS_PER_YEAR = 365.0  # calendar-day convention to match yfinance IV


@dataclass
class SpreadCandidate:
    underlying: str
    expiration: str
    dte: int
    kind: str                  # 'bull_call' | 'bear_put'
    long_strike: float
    short_strike: float
    width: float
    debit: float               # net mid debit per share
    max_loss: float            # per contract, dollars (= debit * 100)
    max_profit: float          # per contract, dollars
    breakeven: float
    p_win: float               # P(full max profit at expiry)
    p_loss: float              # P(full max loss at expiry)
    ev: float                  # per contract, dollars, before costs
    est_costs: float           # per contract, dollars (slippage, round trip)
    ev_after_costs: float
    long_leg: dict
    short_leg: dict

    def to_dict(self) -> dict:
        return asdict(self)

    def order_description(self, contracts: int = 1) -> str:
        opt = "Call" if self.kind == "bull_call" else "Put"
        return (
            f"{self.kind.replace('_', ' ').title()} — "
            f"Buy to open {contracts} {self.underlying} {self.expiration} "
            f"{self.long_strike:g} {opt} / "
            f"Sell to open {contracts} {self.underlying} {self.expiration} "
            f"{self.short_strike:g} {opt} @ net debit {self.debit:.2f} LMT"
        )


def _mid(row: pd.Series) -> float:
    return (row["bid"] + row["ask"]) / 2.0


def leg_passes_liquidity(row: pd.Series, cfg: StrategyConfig) -> bool:
    """Both minimums are required (the old scanner's `and` bug let zero-OI
    contracts through whenever volume was decent)."""
    if cfg.require_nonzero_bid and row["bid"] <= 0:
        return False
    if row["ask"] <= 0 or row["ask"] < row["bid"]:
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
    # PR#3-inspired premium band
    if mid < cfg.min_premium or mid > cfg.max_premium:
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


def _build_spread(long_row: pd.Series, short_row: pd.Series, kind: str,
                  snap: ChainSnapshot, cfg: StrategyConfig) -> SpreadCandidate | None:
    width = abs(float(short_row["strike"]) - float(long_row["strike"]))
    debit = _mid(long_row) - _mid(short_row)
    if debit < cfg.min_debit or debit > width * cfg.max_debit_fraction:
        return None

    dte_years = max(snap.dte, 0.5) / TRADING_DAYS_PER_YEAR  # same-day floor: half a day
    long_iv, short_iv = float(long_row["iv"]), float(short_row["iv"])
    if long_iv <= 0 or short_iv <= 0:
        return None

    k_long, k_short = float(long_row["strike"]), float(short_row["strike"])
    if kind == "bull_call":
        p_win = prob_above(snap.spot, k_short, short_iv, dte_years, cfg.risk_free_rate)
        p_loss = prob_below(snap.spot, k_long, long_iv, dte_years, cfg.risk_free_rate)
        breakeven = k_long + debit
    else:  # bear_put
        p_win = prob_below(snap.spot, k_short, short_iv, dte_years, cfg.risk_free_rate)
        p_loss = prob_above(snap.spot, k_long, long_iv, dte_years, cfg.risk_free_rate)
        breakeven = k_long - debit
    p_mid = max(0.0, 1.0 - p_win - p_loss)

    ev_per_share = (
        p_win * (width - debit)
        + p_mid * (width / 2.0 - debit)
        - p_loss * debit
    )

    half_spreads = (long_row["ask"] - long_row["bid"]) / 2.0 + \
                   (short_row["ask"] - short_row["bid"]) / 2.0
    # entry + exit, each paying a fraction of the combined half-spread
    est_costs = 2.0 * cfg.slippage_half_spread_frac * half_spreads * 100.0

    ev = ev_per_share * 100.0
    return SpreadCandidate(
        underlying=snap.underlying,
        expiration=snap.expiration,
        dte=snap.dte,
        kind=kind,
        long_strike=k_long,
        short_strike=k_short,
        width=width,
        debit=round(debit, 4),
        max_loss=round(debit * 100.0, 2),
        max_profit=round((width - debit) * 100.0, 2),
        breakeven=round(breakeven, 4),
        p_win=round(p_win, 4),
        p_loss=round(p_loss, 4),
        ev=round(ev, 2),
        est_costs=round(est_costs, 2),
        ev_after_costs=round(ev - est_costs, 2),
        long_leg=_leg_info(long_row),
        short_leg=_leg_info(short_row),
    )


def generate_candidates(snap: ChainSnapshot, cfg: StrategyConfig) -> list[SpreadCandidate]:
    """All vertical debit spreads passing liquidity, structure, probability
    and after-cost EV filters, best EV first."""
    if not (cfg.min_dte <= snap.dte <= cfg.max_dte):
        return []

    out: list[SpreadCandidate] = []

    # Synthesis: expected move filter (PR#3-inspired)
    em = 0.0
    try:
        calls = snap.chain[snap.chain["type"] == "call"]
        puts = snap.chain[snap.chain["type"] == "put"]
        if not calls.empty and not puts.empty:
            call_idx = (calls["strike"] - snap.spot).abs().idxmin()
            put_idx = (puts["strike"] - snap.spot).abs().idxmin()
            atm_call_mid = mid_price(float(calls.loc[call_idx, "bid"]), float(calls.loc[call_idx, "ask"]))
            atm_put_mid = mid_price(float(puts.loc[put_idx, "bid"]), float(puts.loc[put_idx, "ask"]))
            em = expected_move(snap.spot, atm_call_mid, atm_put_mid)
    except Exception:
        em = 0.0

    for opt_type, kind in [("call", "bull_call"), ("put", "bear_put")]:
        side = snap.chain[snap.chain["type"] == opt_type]
        if side.empty:
            continue
        liquid = side[side.apply(lambda r: leg_passes_liquidity(r, cfg), axis=1)]
        if len(liquid) < 2:
            continue

        # EM filter using configurable multiplier
        if em > 0:
            liquid = liquid[liquid["strike"].abs().sub(snap.spot).abs() <= (em * cfg.em_filter_multiplier)]

        rows = list(liquid.sort_values("strike").iterrows())
        for (_, a), (_, b) in combinations(rows, 2):
            width = abs(b["strike"] - a["strike"])
            if not any(abs(width - w) < 1e-9 for w in cfg.spread_widths):
                continue
            if kind == "bull_call":
                long_row, short_row = a, b   # buy lower, sell higher
            else:
                long_row, short_row = b, a   # buy higher, sell lower
            cand = _build_spread(long_row, short_row, kind, snap, cfg)
            if cand is None:
                continue
            if cand.p_win < cfg.min_p_win:
                continue
            if cand.ev_after_costs <= cfg.min_ev_after_costs:
                continue
            out.append(cand)

    out.sort(key=lambda c: c.ev_after_costs, reverse=True)
    return out
