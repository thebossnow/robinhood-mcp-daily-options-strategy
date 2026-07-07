"""Paper execution with pessimistic fills.

Entries pay slippage (a fraction of the combined half-spread) on top of mid;
exits give it back up. If the strategy is profitable under these fills it has
a chance live; if it only works at mid, it doesn't work.

This module is also the template for a future MCP live executor: same
RiskManager gate, same Journal writes — only the fill source changes, plus a
mandatory human confirmation step before any live order.
"""

from __future__ import annotations

from ..config import StrategyConfig
from ..journal import Journal, TradeRecord
from ..risk import RiskManager, RiskCheck
from ..signals.candidates import SpreadCandidate


def settlement_value(kind: str, long_strike: float, short_strike: float,
                     settlement_price: float) -> float:
    """Per-share value of a vertical at expiry given the settlement price."""
    if kind == "bull_call":
        long_iv = max(0.0, settlement_price - long_strike)
        short_iv = max(0.0, settlement_price - short_strike)
    elif kind == "bear_put":
        long_iv = max(0.0, long_strike - settlement_price)
        short_iv = max(0.0, short_strike - settlement_price)
    else:
        raise ValueError(f"Unknown spread kind: {kind}")
    return long_iv - short_iv


class PaperBroker:
    def __init__(self, cfg: StrategyConfig, journal: Journal):
        self.cfg = cfg
        self.journal = journal
        self.risk = RiskManager(cfg, journal)

    def _entry_slippage(self, cand: SpreadCandidate) -> float:
        half_spreads = (
            (cand.long_leg["ask"] - cand.long_leg["bid"])
            + (cand.short_leg["ask"] - cand.short_leg["bid"])
        ) / 2.0
        return self.cfg.slippage_half_spread_frac * half_spreads

    def open(self, cand: SpreadCandidate, contracts: int = 1,
             notes: str = "") -> tuple[int | None, RiskCheck]:
        """Returns (trade_id, risk_check). trade_id is None if refused."""
        check = self.risk.check(cand.max_loss)
        if not check.allowed:
            return None, check
        contracts = min(contracts, check.max_contracts)
        entry_debit = round(cand.debit + self._entry_slippage(cand), 4)
        trade_id = self.journal.record_entry(
            cand.to_dict(), contracts, entry_debit,
            notes=notes or "paper entry (mid + slippage)",
        )
        return trade_id, check

    def close(self, trade_id: int, current_mid_value: float,
              notes: str = "") -> TradeRecord:
        """Close at current spread mid, minus exit slippage estimated from
        the entry-time spreads recorded in the journal."""
        rec = self.journal.get(trade_id)
        cand = self._entry_candidate(trade_id)
        slip = self._entry_slippage(cand) if cand else 0.0
        exit_value = max(0.0, round(current_mid_value - slip, 4))
        exit_value = min(exit_value, rec.width)  # spread can't exceed width
        return self.journal.record_exit(
            trade_id, exit_value, status="closed",
            notes=notes or "paper close (mid - slippage)",
        )

    def settle_expired(self, trade_id: int, settlement_price: float) -> TradeRecord:
        rec = self.journal.get(trade_id)
        value = settlement_value(
            rec.kind, rec.long_strike, rec.short_strike, settlement_price
        )
        return self.journal.record_exit(
            trade_id, round(value, 4), status="expired",
            notes=f"settled at underlying {settlement_price:.2f}",
        )

    def _entry_candidate(self, trade_id: int) -> SpreadCandidate | None:
        import json
        row = self.journal._get_row(trade_id)
        if row is None or not row["candidate_json"]:
            return None
        return SpreadCandidate(**json.loads(row["candidate_json"]))
