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

    # EM filter multiplier for strike selection (synthesis)
    em_filter_multiplier: float = 1.5

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

    # --- Position management ---
    # Close when realized profit >= this fraction of max_profit. 0.75 means
    # exit once we've captured 75% of the spread's max gain. Set to 1.0 to
    # disable (hold to expiry). Applies to paper status alerts and backtest.
    take_profit_pct: float = 0.75

    # --- Risk limits (enforced in code; the agent cannot override them) ---
    account_equity: float = 5000.0
    max_risk_per_trade_pct: float = 0.01    # 1% of equity max loss per trade
    daily_loss_limit_pct: float = 0.02      # stop trading for the day at -2%
    max_open_positions: int = 3
    max_consecutive_losses: int = 3         # kill switch

    # --- Volatility-regime gate (pre-trade, see risk/vol_regime.py) ---
    # These are REACTIVE-limit siblings' opposite number: the limits above
    # respond to realized losses; these refuse new entries pre-emptively
    # when the market is already showing elevated or spiking fear, using
    # VIX as a broad-index proxy (this repo trades SPY/QQQ/IWM).
    vix_entry_ceiling: float = 30.0         # refuse new entries at/above this level
    vix_spike_pct: float = 0.30             # refuse if VIX rose this much...
    vix_spike_lookback_days: int = 5        # ...over this many trading days

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


@dataclass
class CSPConfig:
    """Cash-secured-put income strategy — single-leg only, no naked, no
    spreads. Every knob the user's rules named lives here so scan_csp.py,
    manage_csp.py, and risk/csp_manager.py all read the same numbers.

    This is a DIFFERENT strategy from StrategyConfig's vertical/credit
    spreads above: it collateralizes the full strike (cash-secured), not a
    defined-risk width, so its sizing is collateral-as-%-of-account rather
    than max-loss-as-%-of-account.
    """

    # --- Universe, priority order (first tried first each scan) ---
    universe: list[str] = field(default_factory=lambda: [
        "QQQ", "HOOD", "PLTR", "TSLA", "TSLL", "NVDA", "MU", "SOXL",
    ])
    # QQQ (or any name listed here) is exempt from the one-underlying-at-a-
    # time rule: multiple staggered strikes/expirations can be open at once.
    ladder_underlyings: list[str] = field(default_factory=lambda: ["QQQ"])
    max_ladder_positions: int = 4
    # A laddered rung's strike must be at least this far OTM (first ITM
    # strike >= 10-15% below spot, per the user's rule).
    ladder_min_otm_pct: float = 0.10

    # --- Delta window ---
    delta_min: float = 0.05
    delta_max: float = 0.15
    delta_target: float = 0.10       # preferred strike: closest to this
    # High-vol names get pushed toward the low end of the window.
    high_vol_underlyings: list[str] = field(default_factory=lambda: ["SOXL", "TSLL"])
    high_vol_delta_max: float = 0.10
    high_vol_delta_target: float = 0.07

    # --- DTE window ---
    min_dte: int = 21
    max_dte: int = 45
    target_dte: int = 30
    # Occasional longer-dated entries (60-180+ DTE) only when standard-
    # window premium/ROC is weak and there's spare collateral; never picked
    # automatically — scan_csp.py only considers this window with --long-dte.
    long_dte_min: int = 60
    long_dte_max: int = 180

    # --- Liquidity + premium floor ---
    min_open_interest: int = 500
    min_volume: int = 100
    max_spread_pct: float = 0.10
    min_premium: float = 0.15        # skip penny options after commissions
    require_nonzero_bid: bool = True

    # --- Position sizing (collateral, not max-loss) ---
    # Hard cap: never commit more than this fraction of net liq to one
    # underlying's put collateral. 0.20 is the ceiling from the rules;
    # target_pct_per_underlying is used to prefer smaller size when ranking.
    max_pct_per_underlying: float = 0.20
    target_pct_per_underlying: float = 0.15
    # Hard stop: refuse new entries if the cash+T-bill buffer would fall
    # below this fraction of net liq. target is what SGOV sweeps aim for.
    min_cash_buffer_pct: float = 0.30
    target_cash_buffer_pct: float = 0.40

    # --- Exit / management ---
    # Close when captured profit (premium collected minus current cost to
    # buy back) reaches this fraction of the premium collected — i.e. of
    # max profit, since a short put's max profit is the premium itself.
    # The user's rule allows anywhere in 50-90%; this is the trigger point.
    profit_take_frac: float = 0.50
    delta_alert: float = 0.30         # notify + propose roll/BTC/assignment
    urgent_dte: int = 5               # inside this DTE, a delta alert is urgent

    # --- Risk hard stops ---
    max_consecutive_losses: int = 3   # kill switch, requires human review
    # Earnings-week blackout: tickers here are single names where an
    # upcoming print blocks new entries unless explicitly approved. No
    # earnings-calendar data source is connected in this pipeline today —
    # populate configs/csp_earnings.json (ticker -> ISO earnings dates)
    # manually, or wire a real calendar provider before relying on this.
    earnings_sensitive: list[str] = field(default_factory=lambda: [
        "HOOD", "PLTR", "TSLA", "NVDA", "MU",
    ])
    earnings_blackout_days_before: int = 2
    earnings_blackout_days_after: int = 1

    # --- Account (paper phase: notional, not a claim about real money) ---
    account_equity: float = 25000.0
    slippage_half_spread_frac: float = 0.5

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def from_json(cls, path: str | Path) -> "CSPConfig":
        data = json.loads(Path(path).read_text())
        return cls(**data)

    def delta_window(self, underlying: str) -> tuple[float, float, float]:
        """(delta_min, delta_max, delta_target) for this underlying."""
        if underlying in self.high_vol_underlyings:
            return self.delta_min, self.high_vol_delta_max, self.high_vol_delta_target
        return self.delta_min, self.delta_max, self.delta_target
