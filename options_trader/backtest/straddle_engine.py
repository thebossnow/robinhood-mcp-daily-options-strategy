"""Backtest long straddles/strangles by replaying stored chain snapshots to
expiry settlement. Mirrors engine.py's hold-to-expiry model but for a
mixed-type, both-legs-long structure: no width cap, no take-profit look-
ahead (EOD snapshots only — same honesty tradeoff engine.py documents).

Settlement value at expiry: max(0, S_T - call_strike) + max(0, put_strike
- S_T). Settlement prices come from a caller-supplied
{(underlying, expiration): close} mapping, typically yfinance daily closes
(see scripts/backtest_straddle.py). Trades whose expiry hasn't happened yet
are skipped, not guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..data.provider import ChainSnapshot
from ..signals.straddle import StraddleVariantConfig, generate_straddle_candidate


@dataclass
class StraddleBacktestResult:
    trades: list[dict] = field(default_factory=list)
    skipped_unsettled: int = 0
    skipped_no_candidate: int = 0

    @property
    def summary(self) -> dict:
        pnls = [t["pnl"] for t in self.trades]
        if not pnls:
            return {"trades": 0, "skipped_unsettled": self.skipped_unsettled,
                    "skipped_no_candidate": self.skipped_no_candidate}
        wins = [p for p in pnls if p > 0]
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p < 0)
        running, peak, max_dd = 0.0, 0.0, 0.0
        for p in pnls:
            running += p
            peak = max(peak, running)
            max_dd = min(max_dd, running - peak)
        return {
            "trades": len(pnls),
            "skipped_unsettled": self.skipped_unsettled,
            "skipped_no_candidate": self.skipped_no_candidate,
            "win_rate": round(len(wins) / len(pnls), 4),
            "total_pnl": round(sum(pnls), 2),
            "expectancy_per_trade": round(sum(pnls) / len(pnls), 2),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else float("inf"),
            "max_drawdown": round(max_dd, 2),
            "worst_trade": round(min(pnls), 2),
            "best_trade": round(max(pnls), 2),
        }


class StraddleBacktestEngine:
    def __init__(self, cfg: StraddleVariantConfig):
        self.cfg = cfg

    def run(self, snapshots: list[ChainSnapshot],
            settlements: dict[tuple[str, str], float]) -> StraddleBacktestResult:
        result = StraddleBacktestResult()
        for snap in snapshots:
            cand = generate_straddle_candidate(snap, self.cfg)
            if cand is None:
                result.skipped_no_candidate += 1
                continue
            key = (cand.underlying, cand.expiration)
            if key not in settlements:
                result.skipped_unsettled += 1
                continue
            settle_px = settlements[key]

            half_spreads = ((cand.call_leg["ask"] - cand.call_leg["bid"])
                            + (cand.put_leg["ask"] - cand.put_leg["bid"])) / 2.0
            entry = cand.debit + self.cfg.slippage_half_spread_frac * half_spreads
            settle_value = (max(0.0, settle_px - cand.call_strike)
                            + max(0.0, cand.put_strike - settle_px))
            pnl = (settle_value - entry) * 100.0

            result.trades.append({
                "scan_date": snap.taken_at[:10],
                "underlying": cand.underlying,
                "expiration": cand.expiration,
                "kind": cand.kind,
                "call_strike": cand.call_strike,
                "put_strike": cand.put_strike,
                "entry_debit": round(entry, 4),
                "settlement_price": settle_px,
                "settle_value": round(settle_value, 4),
                "pnl": round(pnl, 2),
            })
        return result
