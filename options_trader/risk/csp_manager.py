"""Hard risk limits for the cash-secured-put income strategy.

Separate from risk/manager.py's RiskManager (vertical/credit spreads,
sized as max-loss-% of equity). Cash-secured puts collateralize the full
strike, so sizing here is collateral-as-%-of-net-liq, plus rules the
spread strategies don't need: a cash buffer floor, a one-underlying-at-a-
time gate (with a named laddering exception), and an earnings blackout.

Nothing downstream (execution/paper_csp.py, any future live executor)
opens a CSP without a passing check from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..config import CSPConfig
from ..journal import Journal


@dataclass
class CSPRiskCheck:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    max_contracts: int = 0


class CSPRiskManager:
    def __init__(self, cfg: CSPConfig, journal: Journal,
                 net_liq: float, cash_available: float,
                 earnings_overrides: dict[str, list[str]] | None = None):
        """`net_liq`: total account value (cash + collateral + T-bill/SGOV
        holdings + any equity from assigned shares). `cash_available`: cash
        + T-bill/SGOV NOT already committed as CSP collateral — the pool a
        new position's collateral would be drawn from. Callers own pulling
        these from the account (this repo has no live Robinhood account
        tool connected, so scripts/scan_csp.py takes them as CLI args or a
        --account-snapshot file today).
        `earnings_overrides`: ticker -> list of ISO earnings dates. No
        earnings-calendar data source is wired in; populate
        configs/csp_earnings.json manually or pass a real provider's output
        here."""
        self.cfg = cfg
        self.journal = journal
        self.net_liq = net_liq
        self.cash_available = cash_available
        self.earnings_overrides = earnings_overrides or {}

    def earnings_blackout(self, underlying: str, today: date | None = None) -> str | None:
        """Reason string if `underlying` is in an earnings blackout window,
        else None. Only applies to cfg.earnings_sensitive tickers."""
        if underlying not in self.cfg.earnings_sensitive:
            return None
        dates = self.earnings_overrides.get(underlying, [])
        today = today or date.today()
        for d_str in dates:
            d = date.fromisoformat(d_str)
            start = d - timedelta(days=self.cfg.earnings_blackout_days_before)
            end = d + timedelta(days=self.cfg.earnings_blackout_days_after)
            if start <= today <= end:
                return f"earnings blackout: {underlying} reports {d_str}"
        return None

    def _is_ladder(self, underlying: str) -> bool:
        return underlying in self.cfg.ladder_underlyings

    def check(self, underlying: str, strike: float, premium: float,
             today: date | None = None) -> CSPRiskCheck:
        """Gate a prospective new CSP. Returns how many contracts (0 if
        refused) fit every rule simultaneously."""
        today = today or date.today()
        reasons: list[str] = []

        collateral_per_contract = strike * 100.0
        if collateral_per_contract <= 0:
            return CSPRiskCheck(False, ["strike must be positive"], 0)

        # Kill switch: consecutive CSP losses
        streak = self.journal.consecutive_losses()
        if streak >= self.cfg.max_consecutive_losses:
            reasons.append(
                f"kill switch: {streak} consecutive losses "
                f"(limit {self.cfg.max_consecutive_losses}) — review required"
            )

        # Earnings blackout
        blackout = self.earnings_blackout(underlying, today)
        if blackout:
            reasons.append(blackout)

        # One-underlying-at-a-time for new risk, except laddered names
        # (QQQ by default) which may add staggered rungs freely.
        if not self._is_ladder(underlying):
            open_csps = self.journal.open_csp_positions()
            other_names = {r.underlying for r in open_csps
                           if r.underlying != underlying
                           and r.underlying not in self.cfg.ladder_underlyings}
            if other_names:
                reasons.append(
                    f"one underlying at a time: already have new risk open in "
                    f"{sorted(other_names)}"
                )
        else:
            n_rungs = sum(1 for r in self.journal.open_csp_positions()
                         if r.underlying == underlying)
            if n_rungs >= self.cfg.max_ladder_positions:
                reasons.append(
                    f"ladder full: {n_rungs}/{self.cfg.max_ladder_positions} "
                    f"{underlying} rungs already open"
                )

        # Per-underlying collateral cap
        existing = self.journal.csp_collateral_by_underlying().get(underlying, 0.0)
        cap = self.cfg.max_pct_per_underlying * self.net_liq
        room = cap - existing
        if room < collateral_per_contract:
            reasons.append(
                f"{underlying} collateral cap: {existing:.0f} already committed, "
                f"cap {cap:.0f} ({self.cfg.max_pct_per_underlying:.0%} of net liq) "
                f"— no room for another contract at {collateral_per_contract:.0f}"
            )
            room = 0.0

        # Cash buffer floor: cash_available after new collateral must stay
        # >= min_cash_buffer_pct of net liq.
        buffer_floor = self.cfg.min_cash_buffer_pct * self.net_liq
        spendable = self.cash_available - buffer_floor
        if spendable < collateral_per_contract:
            reasons.append(
                f"cash buffer: opening this would drop the buffer below "
                f"{self.cfg.min_cash_buffer_pct:.0%} of net liq "
                f"(cash available {self.cash_available:.0f}, floor {buffer_floor:.0f})"
            )
            spendable = 0.0

        by_room = int(room // collateral_per_contract) if room > 0 else 0
        by_cash = int(spendable // collateral_per_contract) if spendable > 0 else 0
        max_contracts = max(0, min(by_room, by_cash)) if not reasons else 0
        if not reasons and max_contracts < 1:
            reasons.append("no contracts fit within collateral cap and cash buffer")

        allowed = not reasons
        return CSPRiskCheck(allowed, reasons, max_contracts if allowed else 0)
