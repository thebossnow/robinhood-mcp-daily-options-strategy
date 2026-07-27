"""Backtest by replaying stored chain snapshots.

Free historical option-chain data does not exist, so this engine replays
snapshots collected by the daily scanner (`scan.py --save-snapshot`) — a
forward-collected, point-in-time-correct dataset with no survivorship or
revision bias. The cost is that it takes calendar time to accumulate; the
gate in the README requires a meaningful sample before any live trading.

Settlement prices come from a caller-supplied {(underlying, expiration):
close} mapping, typically filled from yfinance daily history. Trades whose
expiry hasn't happened yet are skipped, not guessed.

Fill model matches the paper broker: entry at mid + slippage. Exit is the
earlier of:
  1. Take-profit: a later snapshot for the same (underlying, expiration)
     shows the spread mid >= entry + take_profit_pct * (width - entry).
     Exit at that mid minus exit slippage.
  2. Expiry settlement: intrinsic value at the underlying's close.
Hold-to-expiry (take_profit_pct=1.0) replicates the original conservative
assumption. The default (0.75) simulates realistic profit-taking and
produces higher trade frequency from the intraday snapshot dataset.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..config import StrategyConfig
from ..data.provider import ChainSnapshot
from ..execution.paper import settlement_value, take_profit_target
from ..signals.candidates import generate_candidates, SpreadCandidate


def _leg_quotes(chain: pd.DataFrame, opt_type: str,
                strike: float) -> tuple[float, float] | None:
    """Returns (mid, half_spread) for a single leg, or None if not found."""
    rows = chain[(chain["type"] == opt_type) & (chain["strike"] == strike)]
    if rows.empty:
        return None
    bid, ask = float(rows.iloc[0]["bid"]), float(rows.iloc[0]["ask"])
    if ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0, (ask - bid) / 2.0


def _spread_value(snap: ChainSnapshot, kind: str,
                  long_strike: float, short_strike: float,
                  ) -> tuple[float, float] | None:
    """(spread_mid, total_half_spread) for the vertical in a later snapshot.

    Returns None when either leg is missing or has an inverted market.
    """
    opt_type = "put" if kind == "bear_put" else "call"
    long_q = _leg_quotes(snap.chain, opt_type, long_strike)
    short_q = _leg_quotes(snap.chain, opt_type, short_strike)
    if long_q is None or short_q is None:
        return None
    return long_q[0] - short_q[0], long_q[1] + short_q[1]


@dataclass
class BacktestResult:
    trades: list[dict] = field(default_factory=list)
    skipped_unsettled: int = 0

    @property
    def summary(self) -> dict:
        pnls = [t["pnl"] for t in self.trades]
        if not pnls:
            return {"trades": 0, "skipped_unsettled": self.skipped_unsettled}
        wins = [p for p in pnls if p > 0]
        running, peak, max_dd = 0.0, 0.0, 0.0
        for p in pnls:
            running += p
            peak = max(peak, running)
            max_dd = min(max_dd, running - peak)
        reasons: dict[str, int] = {}
        for t in self.trades:
            r = t.get("exit_reason", "expired")
            reasons[r] = reasons.get(r, 0) + 1
        return {
            "trades": len(pnls),
            "skipped_unsettled": self.skipped_unsettled,
            "win_rate": round(len(wins) / len(pnls), 4),
            "total_pnl": round(sum(pnls), 2),
            "expectancy_per_trade": round(sum(pnls) / len(pnls), 2),
            "max_drawdown": round(max_dd, 2),
            "exit_reasons": reasons,
        }


class BacktestEngine:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def run(self, snapshots: list[ChainSnapshot],
            settlements: dict[tuple[str, str], float],
            per_snapshot_trades: int = 1) -> BacktestResult:
        result = BacktestResult()

        # Sort chronologically so "subsequent" lookups are always forward in time.
        sorted_snaps = sorted(snapshots, key=lambda s: s.taken_at)

        # Group by (underlying, expiration) for fast take-profit look-ahead.
        by_key: dict[tuple[str, str], list[ChainSnapshot]] = {}
        for snap in sorted_snaps:
            key = (snap.underlying, snap.expiration)
            by_key.setdefault(key, []).append(snap)

        for snap in sorted_snaps:
            candidates = generate_candidates(snap, self.cfg)
            for cand in candidates[:per_snapshot_trades]:
                self._process(cand, snap, by_key, settlements, result)

        return result

    def _process(self, cand: SpreadCandidate, entry_snap: ChainSnapshot,
                 by_key: dict, settlements: dict,
                 result: BacktestResult) -> None:
        key = (cand.underlying, cand.expiration)
        half_spreads = (
            (cand.long_leg["ask"] - cand.long_leg["bid"])
            + (cand.short_leg["ask"] - cand.short_leg["bid"])
        ) / 2.0
        entry = cand.debit + self.cfg.slippage_half_spread_frac * half_spreads
        tp = take_profit_target(entry, cand.width, self.cfg.take_profit_pct)

        # Walk subsequent snapshots for the same contract, look for take-profit.
        exit_value: float | None = None
        exit_date: str = cand.expiration
        exit_reason: str = "expired"

        if self.cfg.take_profit_pct < 1.0:
            for future_snap in by_key.get(key, []):
                if future_snap.taken_at <= entry_snap.taken_at:
                    continue
                q = _spread_value(future_snap, cand.kind,
                                  cand.long_strike, cand.short_strike)
                if q is None:
                    continue
                mid, future_hs = q
                if mid >= tp:
                    slip = self.cfg.slippage_half_spread_frac * future_hs
                    exit_value = min(max(mid - slip, 0.0), cand.width)
                    exit_date = future_snap.taken_at[:10]
                    exit_reason = "take_profit"
                    break

        if exit_value is None:
            if key not in settlements:
                result.skipped_unsettled += 1
                return
            settle_px = settlements[key]
            exit_value = settlement_value(
                cand.kind, cand.long_strike, cand.short_strike, settle_px
            )

        pnl = (exit_value - entry) * 100.0
        result.trades.append(
            {
                "scan_date": entry_snap.taken_at[:10],
                "underlying": cand.underlying,
                "expiration": cand.expiration,
                "kind": cand.kind,
                "long_strike": cand.long_strike,
                "short_strike": cand.short_strike,
                "entry_debit": round(entry, 4),
                "settlement_price": settlements.get(key),
                "exit_value": round(exit_value, 4),
                "exit_date": exit_date,
                "exit_reason": exit_reason,
                "pnl": round(pnl, 2),
                "p_win_at_entry": cand.p_win,
                "p_loss_at_entry": cand.p_loss,
                "ev_after_costs_at_entry": cand.ev_after_costs,
            }
        )
