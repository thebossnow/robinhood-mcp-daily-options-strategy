"""Regime tagging and walk-forward split reporting for the small-account
income backtest spec (options_income_backtest_spec.md section 4's "regime
tags to record" and section 3's walk-forward window).

Deliberately post-hoc: tags are computed by joining the closed-trade list
against VIX/price history AFTER a backtest run, not inside
backtest/portfolio.py's event loop. That keeps the engine's dependency
surface unchanged (it doesn't need to know what a regime tag is) and
keeps this analysis re-runnable against an existing trades.csv without
rerunning the backtest.

Only two of the spec's three tags are implemented: VIX_bucket and
trend_up/down/sideways. IVR_bucket (IV Rank) is deliberately left out --
this repo has no historical per-ticker IV-rank infrastructure (the
IV-rank module that exists on a sibling branch's income-framework PR
never merged here), and building one from scratch for a proxy this
module doesn't otherwise need wasn't worth the added surface for this
pass. Flagged here rather than silently reported as done.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import timedelta


def vix_bucket(vix_close: float) -> str:
    """Spec's four buckets: <15, 15-20, 20-30, >30."""
    if vix_close < 15:
        return "<15"
    if vix_close < 20:
        return "15-20"
    if vix_close < 30:
        return "20-30"
    return ">30"


def trend_bucket(close: float, sma50: float | None,
                 flat_band_pct: float = 0.01) -> str:
    """Up/down/sideways: close within flat_band_pct of its 50-day SMA
    counts as sideways rather than a weak trend in either direction."""
    if sma50 is None or sma50 <= 0:
        return "unknown"
    ratio = close / sma50
    if ratio > 1 + flat_band_pct:
        return "up"
    if ratio < 1 - flat_band_pct:
        return "down"
    return "sideways"


def fetch_vix_closes(start: str, end: str) -> dict[str, float]:
    """Daily VIX closes keyed by YYYY-MM-DD, matching
    dolthub.py's build_spot_lookup's shape but for a single fixed symbol."""
    import yfinance as yf
    end_exclusive = date_cls.fromordinal(
        date_cls.fromisoformat(end).toordinal() + 1
    ).isoformat()
    hist = yf.Ticker("^VIX").history(start=start, end=end_exclusive,
                                     auto_adjust=False)
    return {ts.strftime("%Y-%m-%d"): float(row["Close"])
           for ts, row in hist.iterrows()}


def compute_sma50(closes_by_day: dict[str, float]) -> dict[str, float]:
    """Trailing 50-trading-day SMA per day, keyed by the SAME calendar as
    the input. Days before the 50th available close have no SMA (absent
    from the returned dict) -- callers must fetch price history starting
    well before their backtest window's start for this to cover it."""
    days = sorted(closes_by_day)
    out: dict[str, float] = {}
    window: list[float] = []
    for day in days:
        window.append(closes_by_day[day])
        if len(window) > 50:
            window.pop(0)
        if len(window) == 50:
            out[day] = sum(window) / 50.0
    return out


def tag_trades(trades, vix_by_day: dict[str, float],
              sma50_by_ticker_day: dict[tuple[str, str], float],
              close_by_ticker_day: dict[tuple[str, str], float]) -> list[dict]:
    """Returns one dict per trade: the trade's own fields plus vix_bucket
    and trend_bucket, both 'unknown' if the underlying data wasn't
    available for that (ticker, entry_date)."""
    tagged = []
    for t in trades:
        vix = vix_by_day.get(t.entry_date)
        vb = vix_bucket(vix) if vix is not None else "unknown"
        key = (t.underlying, t.entry_date)
        close = close_by_ticker_day.get(key)
        sma = sma50_by_ticker_day.get(key)
        tb = trend_bucket(close, sma) if close is not None else "unknown"
        tagged.append({
            "sleeve_id": t.sleeve_id, "underlying": t.underlying,
            "kind": t.kind, "entry_date": t.entry_date,
            "exit_date": t.exit_date, "exit_reason": t.exit_reason,
            "contracts": t.contracts, "pnl": t.pnl,
            "vix_bucket": vb, "trend_bucket": tb,
        })
    return tagged


def summarize_pnls(pnls: list[float]) -> dict:
    if not pnls:
        return {"trades": 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": len(pnls),
        "win_rate": round(len(wins) / len(pnls), 4),
        "total_pnl": round(sum(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else float("inf"),
    }


def group_by(tagged_trades: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list[float]] = {}
    for t in tagged_trades:
        groups.setdefault(t[key], []).append(t["pnl"])
    return {k: summarize_pnls(v) for k, v in sorted(groups.items())}


def split_walk_forward(trades, train_end: str) -> tuple[list, list]:
    """Spec's window: train on entries up to and including train_end,
    test on everything after. Split by ENTRY date, not exit date, so a
    trade opened in-sample but closed out-of-sample stays with training
    (its edge was decided under in-sample conditions)."""
    train = [t for t in trades if t.entry_date <= train_end]
    test = [t for t in trades if t.entry_date > train_end]
    return train, test
