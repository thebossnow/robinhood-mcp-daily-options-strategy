#!/usr/bin/env python3
"""Verifier for the autoresearch loop. Real metrics only — no placeholders.

Decides whether a candidate StrategyConfig beats the baseline on collected
snapshots, under the statistical discipline described in loop/program.md:

- frozen risk fields are inviolable (guard_risk_fields),
- no verdict without enough data (data_readiness),
- intraday snapshots deduplicated to one per day per underlying/expiration,
- walk-forward split: most recent scan days held out,
- in-sample improvement with an escalating margin per prior attempt,
- out-of-sample must be positive and must not regress,
- out-of-sample EV-calibration slope must stay positive when there's enough
  OOS sample to judge it (pairing check: a candidate's predicted edge must
  itself track its own held-out P&L, not just beat baseline on average).

Run standalone for a dry-run report on the current config and dataset:

    python loop/evaluate.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.backtest import BacktestEngine
from options_trader.backtest.calibration import MIN_TRADES_FOR_VERDICT, ev_calibration
from options_trader.config import StrategyConfig
from options_trader.data import SnapshotStore
from options_trader.data.provider import ChainSnapshot

# Risk limits the loop may never change. program.md documents why.
FROZEN_RISK_FIELDS = (
    "account_equity",
    "max_risk_per_trade_pct",
    "daily_loss_limit_pct",
    "max_open_positions",
    "max_consecutive_losses",
)

MIN_SCAN_DAYS = 30        # distinct days of snapshots before any verdict
MIN_SETTLED_TRADES = 40   # in-sample settled trades before any verdict
OOS_FRACTION = 0.3        # most recent scan days held out
DD_REGRESSION_TOLERANCE = 1.25   # candidate dd may be at most 25% worse
BASE_MARGIN_FLOOR = 1.0   # dollars of required improvement, minimum
BASE_MARGIN_FRAC = 0.05   # ...or 5% of |baseline expectancy|, whichever is larger
ATTEMPT_ESCALATION = 0.5  # each prior attempt raises the margin by 50%


def guard_risk_fields(baseline: StrategyConfig, candidate: StrategyConfig) -> None:
    """Reject any variant that touches frozen risk limits."""
    changed = [
        f for f in FROZEN_RISK_FIELDS
        if getattr(baseline, f) != getattr(candidate, f)
    ]
    if changed:
        raise ValueError(
            f"Variant changes frozen risk fields {changed} — refused. "
            "Risk limits are outside the loop's edit scope (loop/program.md)."
        )


def dedupe_daily(snapshots: list[ChainSnapshot]) -> list[ChainSnapshot]:
    """One snapshot per (scan day, underlying, expiration) — the earliest.
    Intraday snapshots are highly correlated; counting each one as an
    independent trade would inflate every statistic."""
    picked: dict[tuple, ChainSnapshot] = {}
    for snap in sorted(snapshots, key=lambda s: s.taken_at):
        key = (snap.taken_at[:10], snap.underlying, snap.expiration)
        picked.setdefault(key, snap)
    return list(picked.values())


def scan_days(snapshots: list[ChainSnapshot]) -> list[str]:
    return sorted({s.taken_at[:10] for s in snapshots})


def data_readiness(snapshots: list[ChainSnapshot]) -> tuple[bool, dict]:
    days = scan_days(snapshots)
    info = {
        "distinct_scan_days": len(days),
        "required_scan_days": MIN_SCAN_DAYS,
        "snapshots_total": len(snapshots),
        "snapshots_after_daily_dedupe": len(dedupe_daily(snapshots)),
    }
    return len(days) >= MIN_SCAN_DAYS, info


def split_walk_forward(
    snapshots: list[ChainSnapshot], oos_fraction: float = OOS_FRACTION
) -> tuple[list[ChainSnapshot], list[ChainSnapshot]]:
    """Split on a scan-day boundary: the most recent days are out-of-sample."""
    days = scan_days(snapshots)
    n_oos = max(1, int(len(days) * oos_fraction)) if days else 0
    oos_days = set(days[len(days) - n_oos:])
    train = [s for s in snapshots if s.taken_at[:10] not in oos_days]
    oos = [s for s in snapshots if s.taken_at[:10] in oos_days]
    return train, oos


def collect_metrics(cfg: StrategyConfig, snapshots: list[ChainSnapshot],
                    settlements: dict[tuple[str, str], float]) -> dict:
    result = BacktestEngine(cfg).run(dedupe_daily(snapshots), settlements)
    return result.summary


def required_margin(baseline_expectancy: float, attempts: int) -> float:
    base = max(BASE_MARGIN_FRAC * abs(baseline_expectancy), BASE_MARGIN_FLOOR)
    return base * (1.0 + ATTEMPT_ESCALATION * attempts)


@dataclass
class Verdict:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def decide(candidate_train: dict, baseline_train: dict,
           candidate_oos: dict, baseline_oos: dict,
           attempts: int, max_dd_limit: float,
           candidate_oos_trades: list[dict] | None = None) -> Verdict:
    """Pure decision logic over backtest summaries (BacktestResult.summary
    dicts). max_dd_limit is a negative dollar amount.

    candidate_oos_trades, if given the candidate's OOS BacktestResult.trades,
    adds a pairing check: does the candidate's predicted ev_after_costs
    actually track its own OOS realized P&L (options_trader/backtest/
    calibration.py's ev_calibration)? An expectancy "improvement" that
    doesn't cash on held-out data is a model artifact, not real edge — this
    catches it even when the raw expectancy numbers above look fine."""
    reasons: list[str] = []

    n = candidate_train.get("trades", 0)
    if n < MIN_SETTLED_TRADES:
        reasons.append(
            f"insufficient in-sample settled trades: {n} < {MIN_SETTLED_TRADES}"
        )

    c_exp = candidate_train.get("expectancy_per_trade", 0.0)
    b_exp = baseline_train.get("expectancy_per_trade", 0.0)
    margin = required_margin(b_exp, attempts)
    if c_exp < b_exp + margin:
        reasons.append(
            f"in-sample expectancy {c_exp:.2f} does not beat baseline "
            f"{b_exp:.2f} by required margin {margin:.2f} "
            f"(attempt #{attempts + 1})"
        )

    c_dd = candidate_train.get("max_drawdown", 0.0)
    b_dd = baseline_train.get("max_drawdown", 0.0)
    if c_dd < max_dd_limit:
        reasons.append(
            f"in-sample max drawdown {c_dd:.2f} breaches limit {max_dd_limit:.2f}"
        )
    if b_dd < 0 and c_dd < b_dd * DD_REGRESSION_TOLERANCE:
        reasons.append(
            f"max drawdown regressed >{(DD_REGRESSION_TOLERANCE - 1):.0%}: "
            f"{c_dd:.2f} vs baseline {b_dd:.2f}"
        )

    if candidate_oos.get("trades", 0) == 0:
        reasons.append("no out-of-sample settled trades")
    else:
        c_oos = candidate_oos.get("expectancy_per_trade", 0.0)
        b_oos = baseline_oos.get("expectancy_per_trade", 0.0)
        if c_oos <= 0:
            reasons.append(f"out-of-sample expectancy not positive: {c_oos:.2f}")
        if c_oos < b_oos:
            reasons.append(
                f"out-of-sample regression: {c_oos:.2f} < baseline {b_oos:.2f}"
            )

    oos_calibration = None
    if candidate_oos_trades and len(candidate_oos_trades) >= MIN_TRADES_FOR_VERDICT:
        oos_calibration = ev_calibration(candidate_oos_trades)
        slope = oos_calibration["ols_slope"]
        if slope is not None and slope <= 0:
            reasons.append(
                f"OOS EV-calibration slope {slope} <= 0 — predicted "
                "ev_after_costs does not track this candidate's own "
                "out-of-sample P&L (pairing check: improvement looks like a "
                "model artifact, not real edge)"
            )

    return Verdict(
        accepted=not reasons,
        reasons=reasons,
        details={
            "in_sample": {"candidate": candidate_train, "baseline": baseline_train},
            "out_of_sample": {"candidate": candidate_oos, "baseline": baseline_oos},
            "required_margin": margin,
            "max_dd_limit": max_dd_limit,
            "oos_calibration": oos_calibration,
        },
    )


def evaluate(candidate_cfg: StrategyConfig, baseline_cfg: StrategyConfig,
             snapshots: list[ChainSnapshot],
             settlements: dict[tuple[str, str], float],
             attempts: int = 0) -> Verdict:
    """Full verifier: guards, data gates, walk-forward, decision."""
    guard_risk_fields(baseline_cfg, candidate_cfg)

    ready, info = data_readiness(snapshots)
    if not ready:
        return Verdict(False, [
            f"data gate: only {info['distinct_scan_days']} distinct scan days "
            f"(need {info['required_scan_days']}) — collect more snapshots "
            "before evaluating variants"
        ], details=info)

    train, oos = split_walk_forward(snapshots)
    # Drawdown limit is anchored to the FROZEN baseline risk config —
    # 4 daily-loss-limits of pain in a backtest is where we stop listening.
    max_dd_limit = -4.0 * baseline_cfg.daily_loss_limit
    dd_oos = dedupe_daily(oos)
    candidate_oos_result = BacktestEngine(candidate_cfg).run(dd_oos, settlements)
    return decide(
        candidate_train=collect_metrics(candidate_cfg, train, settlements),
        baseline_train=collect_metrics(baseline_cfg, train, settlements),
        candidate_oos=candidate_oos_result.summary,
        baseline_oos=collect_metrics(baseline_cfg, oos, settlements),
        attempts=attempts,
        max_dd_limit=max_dd_limit,
        candidate_oos_trades=candidate_oos_result.trades,
    )


def _fetch_settlements(snapshots: list[ChainSnapshot]) -> dict:
    from options_trader.data import YFinanceProvider
    provider = YFinanceProvider()
    settlements: dict[tuple[str, str], float] = {}
    for snap in snapshots:
        key = (snap.underlying, snap.expiration)
        if key in settlements:
            continue
        try:
            px = provider.get_settlement_close(snap.underlying, snap.expiration)
        except Exception as e:
            print(f"  settlement fetch failed for {key}: {e}")
            continue
        if px is not None:
            settlements[key] = px
    return settlements


def main() -> int:
    """Dry run: report data readiness and baseline metrics for the current
    config. This is a report, not a verdict on any variant."""
    snapshots = SnapshotStore().load_all()
    ready, info = data_readiness(snapshots)
    print("Data readiness:")
    for k, v in info.items():
        print(f"  {k}: {v}")
    if not snapshots:
        print("No snapshots collected yet — run scan.py --save-snapshot daily.")
        return 1
    if not ready:
        print("NOT READY: keep collecting snapshots before any loop evaluation.")

    cfg = StrategyConfig()
    settlements = _fetch_settlements(snapshots)
    train, oos = split_walk_forward(snapshots)
    print(f"\nWalk-forward split: {len(scan_days(train))} train days / "
          f"{len(scan_days(oos))} OOS days")
    print("\nBaseline in-sample:", collect_metrics(cfg, train, settlements))
    print("Baseline out-of-sample:", collect_metrics(cfg, oos, settlements))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
