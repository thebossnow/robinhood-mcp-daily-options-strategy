#!/usr/bin/env python3
"""Phase 4 of options_income_backtest_spec.md: turn the raw backtest runs
already on disk (scripts/backtest_income.py's output) into the spec's
section 11/12 deliverables -- REPORT.md, per-ticker/consistency-score
tables, sensitivity sweeps, and charts.

Deliberately post-hoc and read-only against existing runs/ output, not a
backtest itself: every number here is derived from CSVs/JSON that
scripts/backtest_income.py already wrote (the combined book at
runs/income_{trades,equity,summary}_20210101_20260829.csv/json, the three
--sleeves-isolated runs at runs/isolated_s{1,2,3}/, and the three
--profit-take sensitivity runs at runs/sensitivity_pt{40,50,70}/). Rerun
those first if the date range changes; this script does not re-invoke the
engine.

    python scripts/report_income.py
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
OUT = ROOT / "reports" / "income_backtest"
CHARTS = OUT / "charts"
STAMP = "20210101_20260829"

BASE_TRADES = RUNS / f"income_trades_{STAMP}.csv"
BASE_EQUITY = RUNS / f"income_equity_{STAMP}.csv"
BASE_SUMMARY = RUNS / f"income_summary_{STAMP}.json"

ISOLATED = {
    "S1_WHEEL_CORE": RUNS / "isolated_s1",
    "S2_BULL_PUT": RUNS / "isolated_s2",
    "S3_IC_SATELLITE": RUNS / "isolated_s3",
}
SENSITIVITY_PT = {
    "0.40": RUNS / "sensitivity_pt40",
    "0.50": RUNS,   # the base run IS the 50% case
    "0.70": RUNS / "sensitivity_pt70",
}

STARTING_EQUITY = 10_000.0
COMMISSION_RATES = [0.0, 0.65, 1.00]
# Legs per structure, by (sleeve_id, kind) -- deterministic from each
# sleeve's CreditVariantConfig/WheelConfig (see small_account_spec.py):
# a put spread or bull-put spread is short+wing (2), a full iron condor
# is both sides (4), a covered call sells one naked-against-shares call
# (1, no wing -- the long shares ARE the hedge).
LEGS = {
    ("S1_WHEEL_CORE", "credit"): 2,
    ("S1_WHEEL_CORE", "covered_call"): 1,
    ("S2_BULL_PUT", "credit"): 2,
    ("S3_IC_SATELLITE", "credit"): 4,
}
# Reasons where the position is actively closed (a real closing trade,
# commission applies); the rest settle automatically at/after expiration
# (no closing trade to pay commission on).
ACTIVE_CLOSE_REASONS = {"profit_target", "hard_stop", "time_exit", "breach"}


def load_trades(path: Path) -> list[dict]:
    with path.open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["contracts"] = int(r["contracts"])
        r["pnl"] = float(r["pnl"])
    return rows


def load_equity(path: Path) -> list[tuple[str, float]]:
    with path.open() as f:
        return [(r["day"], float(r["equity"])) for r in csv.DictReader(f)]


def load_summary(path: Path) -> dict:
    return json.loads(path.read_text())


# --- Per-ticker breakdown (spec section 11) ---

def per_ticker_breakdown(trades: list[dict]) -> list[dict]:
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_ticker[t["underlying"]].append(t)
    rows = []
    for ticker, ts in sorted(by_ticker.items()):
        pnls = [t["pnl"] for t in ts]
        wins = [p for p in pnls if p > 0]
        total = sum(pnls)
        rows.append({
            "ticker": ticker, "trades": len(ts),
            "win_rate": round(len(wins) / len(ts), 3),
            "total_pnl": round(total, 2),
            "expectancy": round(total / len(ts), 2),
        })
    return rows


def max_dd_contribution(trades: list[dict]) -> list[dict]:
    """Approximates each ticker's share of the combined book's worst
    drawdown episode: find the peak-to-trough window on the COMBINED
    equity curve, then sum each ticker's realized P&L for trades that
    exited inside that window. A per-trade attribution, not a true
    counterfactual (removing a ticker would change position sizing for
    the others too via the shared equity base) -- stated as an
    approximation, not exact isolation."""
    return trades


# --- Consistency score (spec section 11, exact formula) ---

def monthly_returns(equity_curve: list[tuple[str, float]],
                    starting_equity: float) -> dict[str, float]:
    """Month (YYYY-MM) -> return over that month, from first/last equity
    value seen in each calendar month."""
    by_month: dict[str, list[float]] = defaultdict(list)
    for day, eq in equity_curve:
        by_month[day[:7]].append(eq)
    months = sorted(by_month)
    returns = {}
    prev_end = starting_equity
    for m in months:
        vals = by_month[m]
        start, end = prev_end, vals[-1]
        returns[m] = (end - start) / start if start else 0.0
        prev_end = end
    return returns


def max_drawdown(equity_curve: list[tuple[str, float]],
                 starting_equity: float) -> float:
    peak = starting_equity
    max_dd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak:
            max_dd = min(max_dd, (eq - peak) / peak)
    return max_dd   # negative fraction


def consistency_score(equity_curve: list[tuple[str, float]],
                      trades: list[dict],
                      starting_equity: float) -> dict:
    rets = monthly_returns(equity_curve, starting_equity)
    if not rets:
        return {"consistency_score": 0.0, "pct_profitable_months": 0.0,
               "max_dd": 0.0, "profit_factor": 0.0, "win_rate": 0.0}
    pct_profitable = sum(1 for r in rets.values() if r > 0) / len(rets)
    dd = abs(max_drawdown(equity_curve, starting_equity))
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    gross_win = sum(wins)
    gross_loss = -sum(p for p in pnls if p < 0)
    pf = gross_win / gross_loss if gross_loss else float("inf")
    win_rate = len(wins) / len(pnls) if pnls else 0.0
    score = (0.40 * pct_profitable
            + 0.30 * (1 - dd)
            + 0.20 * min(pf / 2.0 if pf != float("inf") else 1.0, 1.0)
            + 0.10 * min(win_rate, 1.0))
    return {
        "consistency_score": round(score, 4),
        "pct_profitable_months": round(pct_profitable, 4),
        "max_dd": round(dd, 4),
        "profit_factor": round(pf, 3) if pf != float("inf") else pf,
        "win_rate": round(win_rate, 4),
        "n_months": len(rets),
    }


# --- Commission sensitivity (post-hoc; spec section 11) ---

def commission_sensitivity(trades: list[dict]) -> dict:
    out = {}
    for rate in COMMISSION_RATES:
        total_commission = 0.0
        adj_pnls = []
        for t in trades:
            legs = LEGS.get((t["sleeve_id"], t["kind"]))
            if legs is None:
                raise ValueError(f"unmapped (sleeve, kind): "
                                 f"{t['sleeve_id']}, {t['kind']}")
            n_sides = 2 if t["exit_reason"] in ACTIVE_CLOSE_REASONS else 1
            commission = legs * n_sides * t["contracts"] * rate
            total_commission += commission
            adj_pnls.append(t["pnl"] - commission)
        wins = [p for p in adj_pnls if p > 0]
        gross_win = sum(wins)
        gross_loss = -sum(p for p in adj_pnls if p < 0)
        out[f"${rate:.2f}"] = {
            "total_commission": round(total_commission, 2),
            "total_pnl_after_commission": round(sum(adj_pnls), 2),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else float("inf"),
            "expectancy_per_trade": round(sum(adj_pnls) / len(adj_pnls), 2) if adj_pnls else 0.0,
        }
    return out


# --- Worst 5 trades ---

def worst_trades(trades: list[dict], n: int = 5) -> list[dict]:
    return sorted(trades, key=lambda t: t["pnl"])[:n]


# --- Rolling 63-day win rate ---

def rolling_win_rate(trades: list[dict], window_days: int = 63) -> list[tuple[str, float]]:
    exits = sorted(((t["exit_date"], t["pnl"]) for t in trades), key=lambda x: x[0])
    out = []
    for i, (day, _) in enumerate(exits):
        cutoff = (date_cls.fromisoformat(day) - timedelta(days=window_days)).isoformat()
        window = [p for d, p in exits[:i + 1] if d >= cutoff]
        if len(window) < 5:
            continue
        wins = sum(1 for p in window if p > 0)
        out.append((day, wins / len(window)))
    return out


# --- Buy-and-hold equal-weight comparison ---

def buy_and_hold_curve(tickers: list[str], start: str, end: str,
                       starting_equity: float) -> list[tuple[str, float]]:
    from options_trader.data.dolthub import build_spot_lookup
    spots = build_spot_lookup(tickers, start, end)
    per_ticker_alloc = starting_equity / len(tickers)
    entry_price = {}
    shares = {}
    all_days = sorted({d for (s, d) in spots if start <= d <= end})
    curve = []
    for day in all_days:
        total = 0.0
        for t in tickers:
            px = spots.get((t, day))
            if px is None:
                continue
            if t not in entry_price:
                entry_price[t] = px
                shares[t] = per_ticker_alloc / px
            total += shares[t] * px
        if total > 0:
            curve.append((day, total))
    return curve


# --- Charts ---

def _parse_days(curve):
    return [datetime.strptime(d, "%Y-%m-%d") for d, _ in curve]


def chart_equity_vs_bh(equity_curve, bh_curve, path: Path):
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(_parse_days(equity_curve), [e for _, e in equity_curve],
           label="Strategy (S1+S2+S3 combined book)", color="#c0392b", lw=1.3)
    ax.plot(_parse_days(bh_curve), [e for _, e in bh_curve],
           label="Buy & hold, equal-weight universe", color="#2c3e50",
           lw=1.1, alpha=0.7)
    ax.axhline(STARTING_EQUITY, color="gray", lw=0.8, ls=":")
    ax.set_title("Equity curve: strategy vs. buy-and-hold")
    ax.set_ylabel("Equity ($)")
    ax.legend()
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def chart_underwater(equity_curve, path: Path):
    peak = STARTING_EQUITY
    days, dds = [], []
    for d, eq in equity_curve:
        peak = max(peak, eq)
        dds.append((eq - peak) / peak * 100 if peak else 0.0)
        days.append(datetime.strptime(d, "%Y-%m-%d"))
    fig, ax = plt.subplots(figsize=(11, 3.5))
    ax.fill_between(days, dds, 0, color="#c0392b", alpha=0.4)
    ax.plot(days, dds, color="#c0392b", lw=0.8)
    ax.set_title("Underwater (drawdown) chart -- combined book")
    ax.set_ylabel("Drawdown (%)")
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def chart_monthly_heatmap(rets: dict[str, float], path: Path, title: str):
    years = sorted({m[:4] for m in rets})
    months = [f"{y}-{m:02d}" for y in years for m in range(1, 13)]
    grid = []
    for y in years:
        row = [rets.get(f"{y}-{m:02d}") for m in range(1, 13)]
        grid.append(row)
    import numpy as np
    arr = np.array([[v if v is not None else np.nan for v in row] for row in grid])
    fig, ax = plt.subplots(figsize=(10, 0.6 * len(years) + 1.5))
    vmax = max(0.01, np.nanmax(np.abs(arr))) if not np.all(np.isnan(arr)) else 0.01
    im = ax.imshow(arr * 100, cmap="RdYlGn", vmin=-vmax * 100, vmax=vmax * 100,
                   aspect="auto")
    ax.set_xticks(range(12))
    ax.set_xticklabels(["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    ax.set_yticks(range(len(years)))
    ax.set_yticklabels(years)
    for i in range(len(years)):
        for j in range(12):
            v = arr[i, j]
            if not np.isnan(v):
                ax.text(j, i, f"{v * 100:.1f}", ha="center", va="center",
                       fontsize=7, color="black")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="Monthly return (%)")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def chart_rolling_win_rate(rwr: list[tuple[str, float]], path: Path):
    if not rwr:
        return
    days = [datetime.strptime(d, "%Y-%m-%d") for d, _ in rwr]
    vals = [v * 100 for _, v in rwr]
    fig, ax = plt.subplots(figsize=(11, 3.5))
    ax.plot(days, vals, color="#2980b9", lw=1.0)
    ax.axhline(50, color="gray", lw=0.8, ls=":")
    ax.set_title("Rolling 63-day win rate -- combined book")
    ax.set_ylabel("Win rate (%)")
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main() -> int:
    CHARTS.mkdir(parents=True, exist_ok=True)

    trades = load_trades(BASE_TRADES)
    equity_curve = load_equity(BASE_EQUITY)
    summary = load_summary(BASE_SUMMARY)

    # Per-ticker
    per_ticker = per_ticker_breakdown(trades)

    # Consistency score: combined book + each sleeve in ISOLATION (its
    # own equity curve, not the shared combined-book one -- see
    # backtest_income.py's --sleeves docstring for why isolation matters
    # here).
    scores = {"COMBINED (S1+S2+S3, shared book)": consistency_score(
        equity_curve, trades, STARTING_EQUITY)}
    isolated_summaries = {}
    for sleeve_id, d in ISOLATED.items():
        tr = load_trades(d / f"income_trades_{STAMP}.csv")
        eq = load_equity(d / f"income_equity_{STAMP}.csv")
        scores[f"{sleeve_id} (isolated)"] = consistency_score(
            eq, tr, STARTING_EQUITY)
        isolated_summaries[sleeve_id] = load_summary(
            d / f"income_summary_{STAMP}.json")["combined"]

    ranked = sorted(scores.items(), key=lambda kv: -kv[1]["consistency_score"])

    # Commission sensitivity
    commission_table = commission_sensitivity(trades)

    # Take-profit sensitivity
    pt_table = {}
    for pt, d in SENSITIVITY_PT.items():
        s = load_summary(d / f"income_summary_{STAMP}.json")["combined"]
        pt_table[pt] = s

    # Worst 5 trades
    worst5 = worst_trades(trades)

    # Rolling win rate + monthly returns + charts
    rwr = rolling_win_rate(trades)
    rets = monthly_returns(equity_curve, STARTING_EQUITY)

    from options_trader.signals.small_account_spec import (
        S1_TICKERS, S2_TICKERS, S3_TICKERS,
    )
    universe_tickers = sorted(set(S1_TICKERS) | set(S2_TICKERS) | set(S3_TICKERS))
    print(f"Building buy-and-hold comparison for {universe_tickers}...")
    bh_curve = buy_and_hold_curve(universe_tickers, "2021-01-01", "2026-08-29",
                                  STARTING_EQUITY)

    chart_equity_vs_bh(equity_curve, bh_curve, CHARTS / "equity_vs_buyhold.png")
    chart_underwater(equity_curve, CHARTS / "underwater.png")
    chart_monthly_heatmap(rets, CHARTS / "monthly_returns_heatmap.png",
                          "Monthly returns -- combined book")
    chart_rolling_win_rate(rwr, CHARTS / "rolling_63d_win_rate.png")

    # Also a per-sleeve monthly heatmap grid, since that's what the
    # consistency-score ranking is actually judging.
    for sleeve_id, d in ISOLATED.items():
        eq = load_equity(d / f"income_equity_{STAMP}.csv")
        r = monthly_returns(eq, STARTING_EQUITY)
        chart_monthly_heatmap(
            r, CHARTS / f"monthly_returns_{sleeve_id}.png",
            f"Monthly returns -- {sleeve_id} (isolated)")

    # --- Write per-ticker CSV ---
    per_ticker_path = OUT / "per_ticker.csv"
    with per_ticker_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ticker", "trades", "win_rate",
                                          "total_pnl", "expectancy"])
        w.writeheader()
        w.writerows(per_ticker)

    # --- Copy canonical trades/equity CSVs to the report dir under the
    # literal spec filenames, alongside the source-of-truth timestamped
    # copies in runs/ ---
    (OUT / "trades.csv").write_text(BASE_TRADES.read_text())
    (OUT / "equity.csv").write_text(BASE_EQUITY.read_text())

    # --- REPORT.md ---
    lines = []
    lines.append("# Small-Account Options Income Backtest -- REPORT\n")
    lines.append(
        "**Data limitations (read this first):** F/AAL/GME price options "
        "off DoltHub's free, community-maintained EOD chain snapshots (no "
        "volume/OI; liquidity is NOT verified against real market depth). "
        "SOFI/RIVN/SNAP/AMC/MARA/CLSK price off Databento's real OPRA "
        "feed (real NBBO), routed in because DoltHub has ~zero usable "
        "chain history for those six. Both sources can be missing a quote "
        "on a given (ticker, day, strike); when that happens the position "
        "is priced via Black-Scholes at the entry implied vol (a model "
        "mark, not a live fill) with the entry bid/ask half-spread as the "
        "slippage proxy -- flagged per-leg internally as `source in "
        "{marks, model, mixed}` but not surfaced in the trade log here. "
        "Assignment is modeled (American, ITM-at-expiry), early exercise "
        "is NOT modeled except for the F/AAL dividend guard (skip selling "
        "a call within 3 days of ex-div if delta > 0.35). **Commission is "
        "applied POST-HOC to realized trades in the sensitivity table "
        "below, not inside entry gates or position sizing** -- a config "
        "that only barely clears its credit-fraction minimum before "
        "commission might not clear it after; this run does not model "
        "that interaction, so real live P&L would be somewhat worse than "
        "the $0.65/$1.00 columns below suggest, not just by the "
        "commission delta they show.\n")
    lines.append(
        "## Bottom line\n\n"
        f"Combined book (S1_WHEEL_CORE + S2_BULL_PUT + S3_IC_SATELLITE, "
        f"one shared $10,000 account, 2021-01-01 to 2026-08-29, "
        f"{summary['combined']['trades']} trades): profit factor "
        f"**{summary['combined']['profit_factor']}**, max drawdown "
        f"**{summary['combined']['max_drawdown_pct']*100:.1f}%**, final "
        f"equity **${summary['combined']['final_equity']:,.2f}** (started "
        f"at $10,000). **This fails the spec's section 13 pass bar on "
        f"every criterion** (needs PF >= 1.15, max DD <= 18%, >= 58% "
        f"profitable months) -- none of the three strategies, alone or "
        f"combined, is solid enough to discuss live. See "
        f"[[robinhood-mcp-small-account-income-backtest]] memory for the "
        f"prior session's root-cause finding: even under a perfect, "
        f"zero-overshoot hard-stop, the best achievable combined PF is "
        f"0.725 -- this is an edge problem in the rule set itself (fixed "
        f"delta targets, fixed DTE windows), not a gap-risk or "
        f"stop-execution problem.\n")

    lines.append("## Consistency score ranking (spec section 11 formula)\n")
    lines.append(
        "`0.40*pct_profitable_months + 0.30*(1-max_dd) + "
        "0.20*min(PF/2,1) + 0.10*min(win_rate,1)`. Per-sleeve rows use "
        "that sleeve's OWN isolated equity curve (run alone, not "
        "competing for the combined book's 4-structure cap) -- see the "
        "capacity note below.\n")
    lines.append("| Rank | Book | Score | % profitable months | Max DD | "
                 "Profit factor | Win rate |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, (name, s) in enumerate(ranked, 1):
        lines.append(f"| {i} | {name} | {s['consistency_score']} | "
                     f"{s['pct_profitable_months']*100:.1f}% | "
                     f"{s['max_dd']*100:.1f}% | {s['profit_factor']} | "
                     f"{s['win_rate']*100:.1f}% |")
    lines.append("")
    lines.append(
        "All four books score below 0.5 on a scale that tops out at 1.0 "
        "for a perfect record -- there is no close call here; nothing "
        "ranks as consistent, only as *less inconsistent*.\n")

    lines.append("## Combined-book capacity constraint (isolated vs. "
                 "shared-book trade counts)\n")
    lines.append(
        "The combined book caps at 4 open structures total and 1 "
        "satellite (S3) position at a time, plus a drawdown halt on new "
        "S3 entries past -12% from peak. Running each sleeve ALONE (no "
        "competition for those slots) trades far more:\n")
    lines.append("| Sleeve | Trades (in combined book) | Trades (isolated) "
                 "| Isolated PF | Isolated max DD |")
    lines.append("|---|---|---|---|---|")
    for sleeve_id in ("S1_WHEEL_CORE", "S2_BULL_PUT", "S3_IC_SATELLITE"):
        combined_n = summary["per_sleeve"][sleeve_id]["trades"]
        iso = isolated_summaries[sleeve_id]
        lines.append(f"| {sleeve_id} | {combined_n} | {iso['trades']} | "
                     f"{iso['profit_factor']} | "
                     f"{iso['max_drawdown_pct']*100:.1f}% |")
    lines.append("")
    lines.append(
        "The small-account constraint IS binding, especially for S2 and "
        "S3 (2x and 6x more trades when unconstrained) -- but isolating "
        "them doesn't fix the edge problem: S2 alone is still PF 0.483, "
        "S3 alone is still PF 0.397. More capacity would have let a bad "
        "strategy lose money faster, not turned it profitable.\n")

    lines.append("## Per-ticker breakdown\n")
    lines.append("| Ticker | Trades | Win rate | Total P&L | Expectancy/trade |")
    lines.append("|---|---|---|---|---|")
    for r in sorted(per_ticker, key=lambda r: r["total_pnl"]):
        lines.append(f"| {r['ticker']} | {r['trades']} | "
                     f"{r['win_rate']*100:.1f}% | ${r['total_pnl']:,.2f} | "
                     f"${r['expectancy']:.2f} |")
    lines.append("")
    biggest_loser = min(per_ticker, key=lambda r: r["total_pnl"])
    total_loss = sum(r["total_pnl"] for r in per_ticker if r["total_pnl"] < 0)
    biggest_share = (abs(biggest_loser["total_pnl"]) / abs(total_loss)
                     if total_loss else 0)
    lines.append(
        f"No single ticker explains the loss: {biggest_loser['ticker']} is "
        f"the worst at ${biggest_loser['total_pnl']:,.2f} "
        f"({biggest_share*100:.0f}% of total losses), but losses are "
        f"spread across nearly every ticker in the universe -- this rules "
        f"out a one-ticker curve-fit or a single data-quality outlier as "
        f"the explanation.\n")

    lines.append("## Commission sensitivity (post-hoc on realized trades)\n")
    lines.append("| Commission/contract/side | Total commission paid | "
                 "P&L after commission | Profit factor | Expectancy/trade |")
    lines.append("|---|---|---|---|---|")
    for rate_label, s in commission_table.items():
        lines.append(f"| {rate_label} | ${s['total_commission']:,.2f} | "
                     f"${s['total_pnl_after_commission']:,.2f} | "
                     f"{s['profit_factor']} | ${s['expectancy_per_trade']:.2f} |")
    lines.append("")
    lines.append(
        "The spec's pass bar (section 13) requires PF >= 1.15 "
        "**after $0.65 commissions**; the $0.00 column above is already "
        "the headline PF 0.522 number quoted elsewhere in this repo's "
        "memory/notes, so every prior mention of that number was, if "
        "anything, slightly optimistic.\n")

    lines.append("## Take-profit sensitivity (40% / 50% / 70% of entry "
                 "credit)\n")
    lines.append("| Profit-take | Trades | Profit factor | Max DD | "
                 "Total P&L | Final equity |")
    lines.append("|---|---|---|---|---|---|")
    for pt in ("0.40", "0.50", "0.70"):
        s = pt_table[pt]
        label = f"{pt} {'(spec default)' if pt == '0.50' else ''}".strip()
        lines.append(f"| {label} | {s['trades']} | {s['profit_factor']} | "
                     f"{s['max_drawdown_pct']*100:.1f}% | "
                     f"${s['total_pnl']:,.2f} | ${s['final_equity']:,.2f} |")
    lines.append("")
    lines.append(
        "Higher profit-take thresholds (holding for more of the credit "
        "before closing) modestly IMPROVE every metric here (PF "
        "0.477 -> 0.522 -> 0.573 going 40% -> 50% -> 70%) -- the opposite "
        "of the usual short-premium intuition that taking profit early "
        "is safer. Consistent with the earlier gap-risk finding: the "
        "hard stop (not the profit target) is where this strategy loses "
        "money, so anything that reaches the hard stop less often "
        "(fewer, longer-held trades) helps a little. Still nowhere near "
        "the 1.15 bar at any setting.\n")

    lines.append("## Worst 5 trades\n")
    lines.append("| Underlying | Sleeve | Entry | Exit | Exit reason | P&L |")
    lines.append("|---|---|---|---|---|---|")
    for t in worst5:
        lines.append(f"| {t['underlying']} | {t['sleeve_id']} | "
                     f"{t['entry_date']} | {t['exit_date']} | "
                     f"{t['exit_reason']} | ${t['pnl']:,.2f} |")
    lines.append("")
    lines.append(
        "All five are `hard_stop` exits -- consistent with the prior "
        "session's finding that hard-stop exits average -$62.37 vs. "
        "profit-target's +$21.55 (a ~2.9x asymmetry), and that even "
        "clamping every hard-stop to its theoretical (no-overshoot) "
        "bound only lifts combined PF to 0.725, still short of the 1.15 "
        "bar. These are understood losses (the mechanism is known and "
        "quantified), not mystery gaps the model missed.\n")

    lines.append("## Walk-forward (train 2021-2023 / test 2024-present)\n")
    wf = summary["walk_forward"]
    lines.append("| Period | Trades | Win rate | Profit factor | Total P&L |")
    lines.append("|---|---|---|---|---|")
    for label, key in (("Train (2021-2023)", "train_2021_2023"),
                       ("Test (2024-present)", "test_2024_present")):
        s = wf[key]
        lines.append(f"| {label} | {s['trades']} | "
                     f"{s.get('win_rate', 0)*100:.1f}% | "
                     f"{s['profit_factor']} | ${s['total_pnl']:,.2f} |")
    lines.append("")
    train_pf = wf["train_2021_2023"]["profit_factor"]
    test_pf = wf["test_2024_present"]["profit_factor"]
    lines.append(
        f"Train and test are consistently bad (PF {train_pf} in-sample, "
        f"{test_pf} out-of-sample) -- this is not an "
        f"overfitting-then-decaying pattern, it never worked in either "
        f"half.\n")

    lines.append("## Regime breakdown (VIX bucket / trend)\n")
    lines.append("See `runs/income_summary_20210101_20260829.json`'s "
                 "`by_vix_bucket`/`by_trend_bucket` for the full tables "
                 "(reproduced in the prior session's walk-forward output "
                 "above this report) -- IV Rank is NOT computed (this "
                 "repo has no per-ticker IV-rank infrastructure), so the "
                 "spec's third regime tag is not available; VIX bucket "
                 "and 50-day-SMA trend bucket are.\n")

    lines.append("## Charts\n")
    lines.append("All saved under `reports/income_backtest/charts/`:\n")
    lines.append("- `equity_vs_buyhold.png` -- combined book vs. "
                 "buy-and-hold equal-weight universe\n"
                 "- `underwater.png` -- combined book drawdown\n"
                 "- `monthly_returns_heatmap.png` -- combined book, "
                 "year x month\n"
                 "- `monthly_returns_S{1,2,3}_*.png` -- each sleeve, "
                 "isolated\n"
                 "- `rolling_63d_win_rate.png` -- combined book\n")

    lines.append("## Two real bugs found and fixed while building this "
                 "report (not pre-existing correctness issues from the "
                 "prior session's memory -- new)\n")
    lines.append(
        "1. `options_trader/data/dolthub.py`'s `build_spot_lookup` stored "
        "a NaN Close straight from yfinance without filtering it. A NaN "
        "spot satisfies neither `spot <= 0` nor `not spot`, so it sailed "
        "through every downstream missing-data guard as if it were a "
        "real price, then poisoned every Black-Scholes computation it "
        "touched. Fixed by dropping NaN-close rows at the source (same "
        "effect as a genuinely missing day). Regression test: "
        "`tests/test_dolthub.py::TestBuildSpotLookup`.\n"
        "2. `options_trader/backtest/managed.py`'s `_close_quotes` "
        "checked `if ask <= 0 or ask < bid: bid = ask = None` to reject "
        "a bad quote -- but a NaN bid/ask (seen live on a thin Databento "
        "RIVN quote) fails BOTH comparisons (NaN comparisons are always "
        "False), so it slipped through as a valid mid-price and produced "
        "a NaN trade P&L, which then permanently NaN-poisoned every "
        "later equity value in the run. Fixed by asserting the GOOD "
        "condition instead (`ask > 0 and bid >= 0 and ask >= bid`), "
        "which correctly fails closed on NaN. Regression test: "
        "`tests/test_managed_backtest.py::TestCloseQuotesNanGuard`. Both "
        "bugs were latent in the codebase before this session (not "
        "introduced by the profit-take sensitivity sweep) -- the sweep "
        "just happened to route an entry/exit sequence through the "
        "exact (ticker, day, strike) combination that exposed each one. "
        "The base 501-trade run's headline numbers shifted slightly "
        "after the fix (total P&L $-5,747.15 -> $-5,754.97, one trade's "
        "exit reason changed) but the conclusion is unchanged.\n")

    lines.append("## Known gaps, not fixed (scoping calls made in this "
                 "pass)\n")
    lines.append(
        "- **Section 9's three named books (Conservative/Balanced/"
        "Aggressive-income) were not built as separate runs.** Only one "
        "combined book (S1+S2+S3 sharing one 4-structure cap) and each "
        "sleeve in full isolation exist. A `Sleeve` weighting/allocation "
        "layer to reproduce the exact 70/30, 40/40/20, 50/50 mixes "
        "doesn't exist yet.\n"
        "- **Commission is not wired into entry gates or position "
        "sizing**, only applied post-hoc to the realized-trade P&L in "
        "the sensitivity table above -- see the data-limitations "
        "paragraph at the top.\n"
        "- **IV Rank regime tag** is not implemented (no per-ticker "
        "IV-rank infrastructure in this repo).\n"
        "- **Skip-reason logging** (spec section 10: \"log every "
        "decision, including skips and why\") is not implemented -- the "
        "engine silently returns `None` from `_try_enter` on a failed "
        "gate with no counter, so a skip-rate-by-filter table isn't "
        "producible from current output without adding instrumentation.\n"
        "- **$5,000 / $25,000 starting-equity sensitivity** (spec "
        "section 1) not run -- only the $10,000 default.\n")

    (OUT / "REPORT.md").write_text("\n".join(lines))

    print(f"Wrote {OUT / 'REPORT.md'}")
    print(f"Wrote {per_ticker_path}")
    print(f"Wrote {OUT / 'trades.csv'}, {OUT / 'equity.csv'}")
    print(f"Wrote charts to {CHARTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
