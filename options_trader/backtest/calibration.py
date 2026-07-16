"""Calibration study: do the model's probabilities and EV predict reality?

Every allocator upgrade under discussion (edge-proportional Kelly sizing,
portfolio construction) consumes `p_win` and `ev_after_costs` as inputs. If
those numbers are miscalibrated, an optimizer just formalizes the noise —
so this module answers the prerequisite question over settled backtest
trades:

1. **P(win) reliability** — bucket trades by predicted `p_win` (N(d2) at the
   short leg's implied vol) and compare against the realized full-win
   frequency, with Wilson intervals on the realized rate.
2. **P(loss) reliability** — same for the full-loss tail, which the EV
   formula weights just as heavily.
3. **Brier skill** — Brier score of the predictions vs. a constant predictor
   at the realized base rate. Skill <= 0 means the model's probabilities
   carry no information beyond the average.
4. **EV → realized P&L** — bucket by predicted `ev_after_costs` and compare
   mean predicted vs. mean realized dollars, plus the OLS slope of realized
   on predicted. A well-calibrated EV has slope near 1; slope near 0 means
   the surfaced "edge" does not cash.

Statistical honesty caveats, printed with every report:

- Trades struck on the same day across SPY/QQQ/IWM are highly correlated
  samples; intervals here treat them as independent and are therefore
  optimistic (too narrow).
- Probabilities come from market implied vol, so under the risk-neutral
  measure true edge is ~zero minus costs. Positive predicted EV can be real
  (skew harvested between legs) or artifact (the width/2 middle-region
  approximation). This study is how you tell which.
- Over DoltHub-imported snapshots, liquidity is optimistic (no volume/OI in
  that dataset) — see README.

Input is the trade-dict list produced by `BacktestEngine.run` (persisted by
`scripts/backtest.py --save-trades`). Only fields present in those dicts are
used; `p_loss_at_entry` is optional so older trade files still work.
"""

from __future__ import annotations

import math

WIN, MID, LOSS = "win", "mid", "loss"

# Below ~40 settled trades (the repo's paper-trading significance gate) any
# calibration verdict is noise; the report says so instead of pretending.
MIN_TRADES_FOR_VERDICT = 40


def classify_outcome(kind: str, long_strike: float, short_strike: float,
                     settlement_price: float) -> str:
    """Which payoff region did the spread settle in?

    Mirrors execution.paper.settlement_value: at exactly the short strike the
    spread is worth its full width (win); at exactly the long strike it is
    worthless (loss).
    """
    if kind == "bull_call":
        if settlement_price >= short_strike:
            return WIN
        if settlement_price <= long_strike:
            return LOSS
    elif kind == "bear_put":
        if settlement_price <= short_strike:
            return WIN
        if settlement_price >= long_strike:
            return LOSS
    else:
        raise ValueError(f"Unknown spread kind: {kind}")
    return MID


def _outcome(trade: dict) -> str:
    return classify_outcome(trade["kind"], trade["long_strike"],
                            trade["short_strike"], trade["settlement_price"])


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def _quantile_bins(values: list[float], n_bins: int) -> list[list[int]]:
    """Indices grouped into up to n_bins equal-count bins by value.

    Equal-count rather than fixed-width: the scanner concentrates p_win in a
    narrow band (min_p_win..~0.6), so fixed-width bins would leave most of
    the range empty. Ties stay in one bin (bins can merge, never split a
    predicted value across bins).
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    n = len(order)
    if n == 0:
        return []
    n_bins = max(1, min(n_bins, n))
    bins: list[list[int]] = []
    start = 0
    for b in range(n_bins):
        end = round((b + 1) * n / n_bins)
        if end <= start:
            continue
        # never split identical predicted values across a boundary
        while end < n and values[order[end]] == values[order[end - 1]]:
            end += 1
        bins.append(order[start:end])
        start = end
        if start >= n:
            break
    return bins


def reliability_table(trades: list[dict], prob_field: str, target: str,
                      n_bins: int = 5) -> list[dict]:
    """Predicted probability vs. realized frequency, in quantile bins.

    prob_field: 'p_win_at_entry' or 'p_loss_at_entry'.
    target: WIN or LOSS — the outcome the probability predicts.
    """
    usable = [t for t in trades if t.get(prob_field) is not None]
    probs = [float(t[prob_field]) for t in usable]
    rows = []
    for idx in _quantile_bins(probs, n_bins):
        n = len(idx)
        hits = sum(1 for i in idx if _outcome(usable[i]) == target)
        predicted = sum(probs[i] for i in idx) / n
        realized = hits / n
        lo, hi = wilson_interval(hits, n)
        rows.append({
            "n": n,
            "predicted": round(predicted, 4),
            "realized": round(realized, 4),
            "ci_low": round(lo, 4),
            "ci_high": round(hi, 4),
            "gap": round(realized - predicted, 4),
            # a well-calibrated bin has its prediction inside the CI
            "within_ci": lo <= predicted <= hi,
        })
    return rows


def brier(trades: list[dict], prob_field: str, target: str) -> dict:
    """Brier score and skill vs. a constant base-rate predictor."""
    usable = [t for t in trades if t.get(prob_field) is not None]
    if not usable:
        return {"n": 0}
    outcomes = [1.0 if _outcome(t) == target else 0.0 for t in usable]
    probs = [float(t[prob_field]) for t in usable]
    n = len(usable)
    score = sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / n
    base_rate = sum(outcomes) / n
    ref = base_rate * (1.0 - base_rate)  # Brier of always predicting base rate
    skill = 1.0 - score / ref if ref > 0 else 0.0
    return {
        "n": n,
        "brier": round(score, 4),
        "base_rate": round(base_rate, 4),
        "brier_base": round(ref, 4),
        "skill": round(skill, 4),
    }


def ev_calibration(trades: list[dict], n_bins: int = 5) -> dict:
    """Does predicted ev_after_costs predict realized P&L, dollar for dollar?"""
    evs = [float(t["ev_after_costs_at_entry"]) for t in trades]
    pnls = [float(t["pnl"]) for t in trades]
    n = len(trades)
    if n == 0:
        return {"n": 0, "bins": []}

    bins = []
    for idx in _quantile_bins(evs, n_bins):
        bins.append({
            "n": len(idx),
            "predicted_ev": round(sum(evs[i] for i in idx) / len(idx), 2),
            "realized_pnl": round(sum(pnls[i] for i in idx) / len(idx), 2),
        })

    mean_ev = sum(evs) / n
    mean_pnl = sum(pnls) / n
    var_ev = sum((e - mean_ev) ** 2 for e in evs)
    cov = sum((e - mean_ev) * (p - mean_pnl) for e, p in zip(evs, pnls))
    var_pnl = sum((p - mean_pnl) ** 2 for p in pnls)
    slope = cov / var_ev if var_ev > 0 else float("nan")
    corr = (cov / math.sqrt(var_ev * var_pnl)
            if var_ev > 0 and var_pnl > 0 else float("nan"))
    return {
        "n": n,
        "bins": bins,
        "mean_predicted_ev": round(mean_ev, 2),
        "mean_realized_pnl": round(mean_pnl, 2),
        "ols_slope": round(slope, 3) if not math.isnan(slope) else None,
        "correlation": round(corr, 3) if not math.isnan(corr) else None,
    }


def _fmt_reliability(rows: list[dict]) -> list[str]:
    out = [f"  {'n':>5}  {'predicted':>9}  {'realized':>8}  "
           f"{'95% CI':>16}  {'gap':>7}"]
    for r in rows:
        flag = "" if r["within_ci"] else "  <-- outside CI"
        out.append(
            f"  {r['n']:>5}  {r['predicted']:>9.3f}  {r['realized']:>8.3f}  "
            f"[{r['ci_low']:.3f}, {r['ci_high']:.3f}]  {r['gap']:>+7.3f}{flag}"
        )
    return out


def calibration_report(trades: list[dict], n_bins: int = 5) -> str:
    """Human-readable calibration report over settled trades."""
    lines: list[str] = []
    n = len(trades)
    lines.append(f"Calibration study over {n} settled trades")
    if n < MIN_TRADES_FOR_VERDICT:
        lines.append(f"  !! Only {n} trades (< {MIN_TRADES_FOR_VERDICT}): "
                     "treat everything below as anecdote, not evidence.")
    lines.append("  Caveat: same-day trades across correlated underlyings are")
    lines.append("  not independent samples — intervals are optimistic.")
    if n == 0:
        return "\n".join(lines)

    outcomes = [_outcome(t) for t in trades]
    lines.append("")
    lines.append(f"Outcomes: {outcomes.count(WIN)} win / "
                 f"{outcomes.count(MID)} mid / {outcomes.count(LOSS)} loss")

    for prob_field, target, label in [
        ("p_win_at_entry", WIN, "P(win) — full max profit"),
        ("p_loss_at_entry", LOSS, "P(loss) — full max loss"),
    ]:
        b = brier(trades, prob_field, target)
        if b.get("n", 0) == 0:
            lines.append("")
            lines.append(f"{label}: field {prob_field!r} not in trade file — "
                         "re-run backtest.py --save-trades on current code.")
            continue
        lines.append("")
        lines.append(f"{label} reliability ({b['n']} trades, quantile bins):")
        lines.extend(_fmt_reliability(
            reliability_table(trades, prob_field, target, n_bins)))
        lines.append(
            f"  Brier {b['brier']:.4f} vs base-rate {b['brier_base']:.4f} "
            f"(base rate {b['base_rate']:.3f}) -> skill {b['skill']:+.1%}"
        )
        lines.append("  skill > 0: probabilities rank outcomes better than "
                     "the average; <= 0: no information.")

    ev = ev_calibration(trades, n_bins)
    lines.append("")
    lines.append("Predicted EV after costs -> realized P&L (quantile bins):")
    lines.append(f"  {'n':>5}  {'mean predicted EV':>18}  {'mean realized P&L':>18}")
    for b in ev["bins"]:
        lines.append(f"  {b['n']:>5}  {b['predicted_ev']:>17.2f}$  "
                     f"{b['realized_pnl']:>17.2f}$")
    lines.append(
        f"  Overall: predicted ${ev['mean_predicted_ev']:.2f}/trade vs "
        f"realized ${ev['mean_realized_pnl']:.2f}/trade; "
        f"OLS slope {ev['ols_slope']}, correlation {ev['correlation']}"
    )
    lines.append("  slope ~1 with positive realized mean: EV is predictive —")
    lines.append("  edge-proportional sizing (fractional Kelly under the 1% cap)")
    lines.append("  has real inputs. slope ~0: the surfaced edge is model")
    lines.append("  artifact — fix signals before building any allocator.")
    return "\n".join(lines)
