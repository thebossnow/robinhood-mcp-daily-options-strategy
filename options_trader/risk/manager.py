"""Hard risk limits, enforced in code.

The agent prompt can *describe* these rules, but this module is what actually
refuses a trade. Nothing downstream (paper broker, future MCP executor) opens
a position without a passing RiskCheck.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import StrategyConfig
from ..journal import Journal


@dataclass
class RiskCheck:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    max_contracts: int = 0


class RiskManager:
    def __init__(self, cfg: StrategyConfig, journal: Journal,
                 live_halt_path=None):
        self.cfg = cfg
        self.journal = journal
        self.live_halt_path = live_halt_path

    def check(self, max_loss_per_contract: float,
              today: str | None = None) -> RiskCheck:
        """Gate a prospective trade. max_loss_per_contract in dollars."""
        today = today or date.today().isoformat()
        reasons: list[str] = []

        if max_loss_per_contract <= 0:
            return RiskCheck(False, ["max loss must be positive"], 0)

        # Imported lazily: journal.live_calibration reaches back into
        # options_trader.backtest (for ev_calibration), which reaches back
        # into execution.paper, which imports RiskManager itself — a
        # module-level import here would be circular.
        from ..journal.live_calibration import LIVE_HALT_PATH, live_halt_reason

        # Audit veto: the slow loop (loop/audit_live.py) halts the fast one
        # when live fills stop confirming the model's predicted edge.
        halt = live_halt_reason(self.live_halt_path or LIVE_HALT_PATH)
        if halt:
            reasons.append(f"live audit halt: {halt}")

        # Kill switch: consecutive losses
        streak = self.journal.consecutive_losses()
        if streak >= self.cfg.max_consecutive_losses:
            reasons.append(
                f"kill switch: {streak} consecutive losses "
                f"(limit {self.cfg.max_consecutive_losses}) — review required"
            )

        # Daily loss limit
        pnl_today = self.journal.realized_pnl_on(today)
        if pnl_today <= -self.cfg.daily_loss_limit:
            reasons.append(
                f"daily loss limit hit: {pnl_today:.2f} <= "
                f"-{self.cfg.daily_loss_limit:.2f}"
            )

        # Concurrency
        n_open = len(self.journal.open_positions())
        if n_open >= self.cfg.max_open_positions:
            reasons.append(
                f"max open positions reached ({n_open}/{self.cfg.max_open_positions})"
            )

        # Per-trade sizing
        max_contracts = int(self.cfg.max_risk_per_trade // max_loss_per_contract)
        if max_contracts < 1:
            reasons.append(
                f"single contract risks {max_loss_per_contract:.2f}, over the "
                f"per-trade cap of {self.cfg.max_risk_per_trade:.2f} "
                f"({self.cfg.max_risk_per_trade_pct:.1%} of equity)"
            )

        # Portfolio heat: open risk + new risk must stay under 2x daily limit
        heat_cap = 2.0 * self.cfg.daily_loss_limit
        if self.journal.open_risk() + max_loss_per_contract > heat_cap:
            reasons.append(
                f"portfolio heat: open risk {self.journal.open_risk():.2f} + "
                f"new {max_loss_per_contract:.2f} exceeds cap {heat_cap:.2f}"
            )

        allowed = not reasons
        return RiskCheck(allowed, reasons, max_contracts if allowed else 0)
