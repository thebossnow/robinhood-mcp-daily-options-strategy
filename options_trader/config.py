"""Strategy configuration.

Every numeric knob for the pipeline lives here so the scanner, backtest and
paper broker are guaranteed to use identical logic. The defaults encode a
defined-risk vertical-spread strategy with positive-expectancy filters —
NOT the original "cheap OTM lottery ticket" criteria, which have negative
expected value after spreads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class StrategyConfig:
    # --- Universe ---
    underlyings: list[str] = field(default_factory=lambda: ["SPY", "QQQ", "IWM"])
    min_dte: int = 1          # skip 0DTE by default: gamma risk + spread costs
    max_dte: int = 7

    # --- Liquidity (both legs of a spread must pass ALL of these) ---
    min_open_interest: int = 500
    min_volume: int = 50
    max_spread_pct: float = 0.10   # (ask - bid) / mid per leg
    require_nonzero_bid: bool = True

    # PR#3-inspired premium band (useful for single-leg and vertical legs)
    min_premium: float = 0.30
    max_premium: float = 5.00

<<<<<<< HEAD
=======
    # EM filter multiplier for strike selection (synthesis)
    em_filter_multiplier: float = 1.5

>>>>>>> 01d7ca3 (Add premium band and EM filter multiplier from synthesis to config; integrate EM filter in candidates.)
    # --- Spread construction ---
    spread_widths: list[float] = field(default_factory=lambda: [1.0, 2.0, 5.0])
    # Debit must be <= this fraction of width. 0.45 means max profit is at
    # least ~1.2x max loss BEFORE probability weighting; the EV filter below
    # is what actually decides.
    max_debit_fraction: float = 0.45
    min_debit: float = 0.10        # avoid untradeable sub-dime spreads

    # --- Expectancy filters ---
    min_p_win: float = 0.25        # prob short strike is breached at expiry
    min_ev_after_costs: float = 0.0   # per contract, dollars; must be > 0 to trade
    # Fraction of each leg's half-spread paid as slippage, per side (entry+exit)
    slippage_half_spread_frac: float = 0.5
    risk_free_rate: float = 0.0

    # --- Risk limits (enforced in code; the agent cannot override them) ---
    account_equity: float = 5000.0
    max_risk_per_trade_pct: float = 0.01    # 1% of equity max loss per trade
    daily_loss_limit_pct: float = 0.02      # stop trading for the day at -2%
    max_open_positions: int = 3
    max_consecutive_losses: int = 3         # kill switch

    # --- Output ---
    top_n: int = 5

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def from_json(cls, path: str | Path) -> "StrategyConfig":
        data = json.loads(Path(path).read_text())
        return cls(**data)

    @property
    def max_risk_per_trade(self) -> float:
        return self.account_equity * self.max_risk_per_trade_pct

    @property
    def daily_loss_limit(self) -> float:
        return self.account_equity * self.daily_loss_limit_pct
