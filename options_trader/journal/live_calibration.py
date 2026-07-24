"""Live/paper-trading calibration — the graph's real-fills anchor.

options_trader/backtest/calibration.py asks whether predicted p_win/EV match
settled BACKTEST outcomes. This module asks the harder question over REAL
closed trades in the journal: does the edge the scanner claimed at entry
actually show up in money that settled in the account? That gap — backtest
score vs. live result — is the number in the whole pipeline that can't be
argued with, because nothing downstream of it can be re-tuned to look better.

Only vertical-spread entries currently record `p_win`/`ev_after_costs` at
entry (see Journal.record_entry); credit-strategy entries don't carry those
fields yet, so this report is scoped to what was actually recorded — no
placeholders, no guessing at credit-trade predictions that were never made.

`LIVE_HALT_PATH` is the other end of the audit loop: loop/audit_live.py
writes it when live calibration has broken down over a large-enough sample,
and RiskManager refuses new entries while it exists — a slower loop vetoing
the fast one, anchored to this same data.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..backtest.calibration import MIN_TRADES_FOR_VERDICT, ev_calibration
from .journal import TradeRecord

LIVE_HALT_PATH = Path("loop/live_halt.json")


def closed_trades_with_predicted_ev(records: list[TradeRecord]) -> list[dict]:
    """TradeRecord list -> ev_calibration()-shaped dicts, entries that
    actually recorded a prediction only."""
    return [
        {"ev_after_costs_at_entry": r.ev_after_costs, "pnl": r.realized_pnl}
        for r in records
        if r.ev_after_costs is not None and r.realized_pnl is not None
    ]


def live_gap_report(records: list[TradeRecord], n_bins: int = 5,
                    backtest_expectancy: float | None = None) -> str:
    """Human-readable report: predicted EV vs. realized live P&L, and
    optionally the gap against a backtest's predicted expectancy_per_trade
    (from scripts/backtest.py --save-trades summary)."""
    trades = closed_trades_with_predicted_ev(records)
    n = len(trades)
    lines = [f"Live calibration over {n} closed trades with a recorded prediction"]
    if n < MIN_TRADES_FOR_VERDICT:
        lines.append(f"  !! Only {n} trades (< {MIN_TRADES_FOR_VERDICT}): "
                     "treat everything below as anecdote, not evidence.")
    if n == 0:
        lines.append("  No trades with p_win/ev_after_costs recorded yet — "
                     "credit-strategy entries don't record predictions; "
                     "vertical entries do (options_trader/journal/journal.py).")
        return "\n".join(lines)

    ev = ev_calibration(trades, n_bins=n_bins)
    lines.append("")
    lines.append("Predicted EV after costs -> REALIZED live P&L (quantile bins):")
    lines.append(f"  {'n':>5}  {'mean predicted EV':>18}  {'mean realized P&L':>18}")
    for b in ev["bins"]:
        lines.append(f"  {b['n']:>5}  {b['predicted_ev']:>17.2f}$  "
                     f"{b['realized_pnl']:>17.2f}$")
    lines.append(
        f"  Overall: predicted ${ev['mean_predicted_ev']:.2f}/trade vs "
        f"realized ${ev['mean_realized_pnl']:.2f}/trade; "
        f"OLS slope {ev['ols_slope']}, correlation {ev['correlation']}"
    )
    lines.append("  slope ~1: live fills confirm the model's edge. slope <= 0")
    lines.append("  or a negative realized mean: the backtest's edge is not")
    lines.append("  showing up in real fills — investigate before sizing up.")

    if backtest_expectancy is not None:
        live_expectancy = ev["mean_realized_pnl"]
        gap = live_expectancy - backtest_expectancy
        lines.append("")
        lines.append(
            f"Backtest-vs-live gap: backtest predicted "
            f"${backtest_expectancy:.2f}/trade expectancy, live realized "
            f"${live_expectancy:.2f}/trade -> gap ${gap:+.2f}/trade "
            f"({'live underperforms the backtest' if gap < 0 else 'live matches or beats the backtest'})"
        )
    return "\n".join(lines)


def live_halt_reason(path: Path = LIVE_HALT_PATH) -> str | None:
    """None if live trading is not halted; otherwise a human-readable reason
    taken from the last loop/audit_live.py run."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return f"{path} exists but is unreadable — treat as halted"
    reasons = data.get("reasons") or [f"live trading halted (see {path})"]
    return "; ".join(reasons)
