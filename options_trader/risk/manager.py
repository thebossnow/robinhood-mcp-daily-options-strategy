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
    """`max_contracts` is the number of contracts the book can actually
    absorb — the smaller of what the per-trade cap allows and what is left
    under the portfolio-heat cap. Callers may open up to that many; opening
    more would breach a cap this check is responsible for."""
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    max_contracts: int = 0


class RiskManager:
    def __init__(self, cfg: StrategyConfig, journal: Journal,
                 live_halt_path=None, vix_provider=None):
        """`vix_provider`: zero-arg callable returning a pandas Series of
        VIX closes (oldest first), or None. Defaults to None, which SKIPS
        the volatility-regime gate entirely — tests and ad-hoc scripts
        shouldn't need network access just to construct a RiskManager.
        Production entry points (scan_credit.py) must pass the real
        provider (risk/vol_regime.py's fetch_vix_closes) for this gate to
        actually protect anything; see that module's docstring for why it
        exists alongside the reactive limits below."""
        self.cfg = cfg
        self.journal = journal
        self.live_halt_path = live_halt_path
        self.vix_provider = vix_provider

    def check(self, max_loss_per_contract: float,
              today: str | None = None) -> RiskCheck:
        """Gate a prospective trade. max_loss_per_contract in dollars.

        Returns permission plus the CAPACITY (`RiskCheck.max_contracts`) the
        book has for this structure. Size is decided separately, by
        risk/sizing.py; a position must clear both, and a caller that sizes
        above the returned capacity must be clamped to it before the report
        it shows the operator is rendered.
        """
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

        # Volatility-regime gate: refuse NEW entries pre-emptively when the
        # market is already fearful, before any reactive limit below could
        # fire (see vol_regime.py — this is the pre-emptive half).
        if self.vix_provider is not None:
            fetched = self.vix_provider()
            if fetched is None:
                # Provider tried and failed — fail closed directly rather
                # than passing None into check_vol_regime, which would
                # interpret it as "no data given, fetch live" and mask the
                # provider's own failure behind a real network call.
                reasons.append(
                    "vol regime: check unavailable (provider returned no "
                    "data) — refusing new entries defensively"
                )
            else:
                from .vol_regime import check_vol_regime
                vol_check = check_vol_regime(
                    self.cfg.vix_entry_ceiling, self.cfg.vix_spike_pct,
                    self.cfg.vix_spike_lookback_days, vix_closes=fetched,
                )
                if not vol_check.allowed:
                    reasons.append(f"vol regime: {vol_check.reason}")

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
        max_by_trade = int(self.cfg.max_risk_per_trade // max_loss_per_contract)
        if max_by_trade < 1:
            reasons.append(
                f"single contract risks {max_loss_per_contract:.2f}, over the "
                f"per-trade cap of {self.cfg.max_risk_per_trade:.2f} "
                f"({self.cfg.max_risk_per_trade_pct:.1%} of equity)"
            )

        # Portfolio heat: open risk + new risk must stay under 2x daily limit.
        # `max_contracts` is a CAPACITY, not a per-contract verdict: callers
        # size positions at more than one contract (scan_income.py sizes from
        # the tier schedule), and clamping to a capacity that only ever
        # considered a single contract let an N-contract entry add N times
        # the risk this cap was written to bound.
        heat_cap = 2.0 * self.cfg.daily_loss_limit
        open_risk = self.journal.open_risk()
        max_by_heat = int(max(0.0, heat_cap - open_risk) // max_loss_per_contract)
        if max_by_heat < 1:
            reasons.append(
                f"portfolio heat: open risk {open_risk:.2f} + "
                f"new {max_loss_per_contract:.2f} exceeds cap {heat_cap:.2f}"
            )

        allowed = not reasons
        max_contracts = min(max_by_trade, max_by_heat)
        return RiskCheck(allowed, reasons, max_contracts if allowed else 0)
