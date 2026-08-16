"""Pre-trade volatility-regime gate — the pre-emptive half of risk control.

Every check in manager.py's RiskManager is REACTIVE: it looks at realized
P&L after a loss has already happened (daily loss limit, consecutive
losses). A short-premium strategy's classic failure mode is a single large
gap move that blows through those limits in one trade, before any reactive
check ever fires. This module is the pre-emptive half: refuse NEW entries
when the market is already showing elevated or rapidly rising fear.

Uses VIX as a market-wide proxy rather than a per-symbol realized-vol
calculation — the traded universe (SPY/QQQ/IWM) is broad-index, and VIX is
specifically built to capture systemic risk a single name's own vol can
miss.

Two independent triggers, either refuses new entries:
  LEVEL  VIX >= vix_entry_ceiling (default 30.0). Sanity-checked against
         this repo's own put-credit delta/DTE sweep window
         (2026-04-07..08-15): VIX ranged 14.25-25.78 there, so this
         ceiling never would have blocked any of that sweep's uniformly
         positive results — meaning the sweep never traded through a
         regime this gate would have refused, which is exactly why a
         pre-emptive gate is needed rather than trusting that backtest.
  SPIKE  VIX has risen >= vix_spike_pct over the trailing
         vix_spike_lookback_days trading days. Catches a regime change in
         progress before the absolute level crosses the ceiling. The same
         window saw a +40% 5-day VIX move without ever crossing 30,
         confirming this trigger is reachable, not a dead threshold.

Fails CLOSED: if VIX data can't be fetched or is insufficient, entries are
refused rather than allowed. A kill switch that quietly does nothing during
a data outage is worse than one that's occasionally too cautious.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class VolRegimeCheck:
    allowed: bool
    reason: str | None = None
    vix_level: float | None = None
    vix_pct_change: float | None = None


def fetch_vix_closes(lookback_days: int) -> pd.Series | None:
    """Trailing VIX daily closes, oldest first, or None on any failure.
    Real network call — tests should inject a fake series via
    `check_vol_regime`'s `vix_closes` param instead of monkeypatching this."""
    try:
        import yfinance as yf
        hist = yf.Ticker("^VIX").history(
            period=f"{lookback_days + 15}d", auto_adjust=False
        )
        closes = hist["Close"].dropna()
        return closes if not closes.empty else None
    except Exception:
        return None


def check_vol_regime(vix_entry_ceiling: float, vix_spike_pct: float,
                     vix_spike_lookback_days: int,
                     vix_closes: pd.Series | None = None) -> VolRegimeCheck:
    """`vix_closes`: optional pre-fetched Series (oldest first) for testing
    or reuse across calls; defaults to a live fetch."""
    if vix_closes is None:
        vix_closes = fetch_vix_closes(vix_spike_lookback_days)
    if vix_closes is None or len(vix_closes) < 2:
        return VolRegimeCheck(
            False, "vol regime check unavailable (no VIX data) — "
            "refusing new entries defensively"
        )

    level = float(vix_closes.iloc[-1])
    lookback = min(vix_spike_lookback_days, len(vix_closes) - 1)
    baseline = float(vix_closes.iloc[-1 - lookback])
    pct_change = (level - baseline) / baseline if baseline > 0 else 0.0

    if level >= vix_entry_ceiling:
        return VolRegimeCheck(
            False,
            f"VIX level {level:.2f} >= entry ceiling {vix_entry_ceiling:.2f}",
            level, pct_change,
        )
    if pct_change >= vix_spike_pct:
        return VolRegimeCheck(
            False,
            f"VIX spiked {pct_change:+.1%} over {lookback}d "
            f"(limit {vix_spike_pct:+.1%})",
            level, pct_change,
        )
    return VolRegimeCheck(True, None, level, pct_change)
