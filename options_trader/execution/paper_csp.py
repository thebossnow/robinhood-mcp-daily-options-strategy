"""Paper execution for the cash-secured-put income strategy.

Same pattern as execution/paper.py: pessimistic fills (entries pay
slippage, already netted into CSPCandidate.premium by signals/csp.py),
every open gated by a risk check, every action a Journal write. This is
also the template for a future MCP/live executor — no live Robinhood
trading tool is connected in this pipeline today, so this module only
ever produces paper fills; a human reads its output and places (or
doesn't place) the real order.
"""

from __future__ import annotations

from ..config import CSPConfig
from ..journal import Journal, TradeRecord
from ..risk.csp_manager import CSPRiskManager, CSPRiskCheck
from ..signals.csp import CSPCandidate
from ..signals.covered_call import CoveredCallCandidate


class CSPPaperBroker:
    def __init__(self, cfg: CSPConfig, journal: Journal, risk: CSPRiskManager):
        self.cfg = cfg
        self.journal = journal
        self.risk = risk

    # --- cash-secured puts ---

    def open(self, cand: CSPCandidate, contracts: int = 1,
             notes: str = "") -> tuple[int | None, CSPRiskCheck]:
        check = self.risk.check(cand.underlying, cand.strike, cand.premium)
        if not check.allowed:
            return None, check
        contracts = min(contracts, check.max_contracts)
        trade_id = self.journal.record_csp_entry(
            cand.to_dict(), contracts,
            notes=notes or "paper CSP entry (mid - slippage)",
        )
        return trade_id, check

    def close(self, trade_id: int, cost_to_close_mid: float,
             half_spread: float, status: str = "closed",
             notes: str = "") -> TradeRecord:
        """Buy to close at current mid plus exit slippage. Cost can't be
        negative or exceed the strike (a put's max value)."""
        rec = self.journal.get(trade_id)
        exit_cost = cost_to_close_mid + self.cfg.slippage_half_spread_frac * half_spread
        exit_cost = min(max(exit_cost, 0.0), rec.short_strike)
        return self.journal.record_exit(
            trade_id, round(exit_cost, 4), status=status,
            notes=notes or "paper CSP close (mid + slippage)",
        )

    def expire_worthless(self, trade_id: int, settlement_price: float) -> TradeRecord:
        rec = self.journal.get(trade_id)
        return self.journal.record_exit(
            trade_id, 0.0, status="expired",
            notes=f"expired worthless, underlying settled {settlement_price:.2f}",
        )

    def assign(self, trade_id: int, settlement_price: float) -> tuple[TradeRecord, int]:
        """Settle an ITM-at-expiry CSP as assignment: close the put at
        intrinsic value (the same accounting as buying it back at
        intrinsic), and open a shares position at cost basis =
        strike - premium collected. Returns (closed put record, shares
        trade id)."""
        rec = self.journal.get(trade_id)
        intrinsic = max(0.0, rec.short_strike - settlement_price)
        closed = self.journal.record_exit(
            trade_id, round(intrinsic, 4), status="assigned",
            notes=f"assigned at strike {rec.short_strike:.2f}, "
                  f"underlying settled {settlement_price:.2f}",
        )
        cost_basis = round(rec.short_strike - rec.entry_debit, 4)
        shares_id = self.journal.record_shares_entry(
            rec.underlying, rec.contracts, cost_basis,
            notes=f"assigned from CSP #{trade_id}, {rec.contracts * 100} shares",
        )
        return closed, shares_id

    # --- covered calls (against assigned shares) ---

    def open_cc(self, cand: CoveredCallCandidate, contracts: int = 1,
               notes: str = "") -> int:
        """No CSPRiskManager gate: the collateral is shares already owned,
        not new cash risk — this can never breach the collateral cap or
        cash buffer the way a new put can."""
        return self.journal.record_cc_entry(
            cand.to_dict(), contracts,
            notes=notes or "paper covered call entry (mid - slippage)",
        )

    def close_cc(self, trade_id: int, cost_to_close_mid: float,
                half_spread: float, status: str = "closed",
                notes: str = "") -> TradeRecord:
        rec = self.journal.get(trade_id)
        exit_cost = cost_to_close_mid + self.cfg.slippage_half_spread_frac * half_spread
        exit_cost = max(exit_cost, 0.0)
        return self.journal.record_exit(
            trade_id, round(exit_cost, 4), status=status,
            notes=notes or "paper covered call close (mid + slippage)",
        )

    def call_away(self, cc_trade_id: int, shares_trade_id: int,
                  settlement_price: float) -> tuple[TradeRecord, TradeRecord]:
        """The covered call finished ITM: it's closed at intrinsic value
        (equivalent to being assigned) and the shares are sold at the
        call's strike."""
        cc = self.journal.get(cc_trade_id)
        intrinsic = max(0.0, settlement_price - cc.short_strike)
        cc_closed = self.journal.record_exit(
            cc_trade_id, round(intrinsic, 4), status="assigned",
            notes=f"shares called away at {cc.short_strike:.2f}",
        )
        shares_closed = self.journal.record_exit(
            shares_trade_id, cc.short_strike, status="closed",
            notes=f"called away via covered call #{cc_trade_id}",
        )
        return cc_closed, shares_closed
