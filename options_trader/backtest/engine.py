"""Backtest by replaying stored chain snapshots to expiry settlement.

Free historical option-chain data does not exist, so this engine replays
snapshots collected by the daily scanner (`scan.py --save-snapshot`) — a
forward-collected, point-in-time-correct dataset with no survivorship or
revision bias. The cost is that it takes calendar time to accumulate; the
gate in the README requires a meaningful sample before any live trading.

Settlement prices come from a caller-supplied {(underlying, expiration):
close} mapping, typically filled from yfinance daily history. Trades whose
expiry hasn't happened yet are skipped, not guessed.

Fill model matches the paper broker: entry at mid + slippage, hold to
expiry, settle at intrinsic. Hold-to-expiry is the most conservative
management assumption — real management (profit targets, stops) can only
be evaluated with intraday data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import StrategyConfig
from ..data.provider import ChainSnapshot
from ..execution.paper import settlement_value
from ..signals.candidates import generate_candidates


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
        return {
            "trades": len(pnls),
            "skipped_unsettled": self.skipped_unsettled,
            "win_rate": round(len(wins) / len(pnls), 4),
            "total_pnl": round(sum(pnls), 2),
            "expectancy_per_trade": round(sum(pnls) / len(pnls), 2),
            "max_drawdown": round(max_dd, 2),
        }


class BacktestEngine:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def run(self, snapshots: list[ChainSnapshot],
            settlements: dict[tuple[str, str], float],
            per_snapshot_trades: int = 1) -> BacktestResult:
        result = BacktestResult()
        for snap in snapshots:
            candidates = generate_candidates(snap, self.cfg)
            for cand in candidates[:per_snapshot_trades]:
                key = (cand.underlying, cand.expiration)
                if key not in settlements:
                    result.skipped_unsettled += 1
                    continue
                settle_px = settlements[key]
                half_spreads = (
                    (cand.long_leg["ask"] - cand.long_leg["bid"])
                    + (cand.short_leg["ask"] - cand.short_leg["bid"])
                ) / 2.0
                entry = cand.debit + self.cfg.slippage_half_spread_frac * half_spreads
                exit_value = settlement_value(
                    cand.kind, cand.long_strike, cand.short_strike, settle_px
                )
                pnl = (exit_value - entry) * 100.0
                result.trades.append(
                    {
                        "scan_date": snap.taken_at[:10],
                        "underlying": cand.underlying,
                        "expiration": cand.expiration,
                        "kind": cand.kind,
                        "long_strike": cand.long_strike,
                        "short_strike": cand.short_strike,
                        "entry_debit": round(entry, 4),
                        "settlement_price": settle_px,
                        "exit_value": round(exit_value, 4),
                        "pnl": round(pnl, 2),
                        "p_win_at_entry": cand.p_win,
                        "p_loss_at_entry": cand.p_loss,
                        "ev_after_costs_at_entry": cand.ev_after_costs,
                    }
                )
        return result
