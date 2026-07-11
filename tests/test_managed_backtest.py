"""Managed backtest engine: exit rules, settlement, weekly cadence.

Uses a FakeHistory serving synthetic day-chains so no network is touched.
"""

import pandas as pd
import pytest

from options_trader.backtest.managed import (
    ManagedBacktestEngine, weekly_entry_days,
)
from options_trader.signals.credit import CreditVariantConfig

# 2026-01-05 is a Monday; expiration 2026-02-13 is 39 DTE.
ENTRY = "2026-01-05"
EXPIRATION = "2026-02-13"
SYMBOL = "TEST"


def trading_days(start="2026-01-05", end="2026-02-13"):
    days = pd.bdate_range(start=start, end=end)
    return [d.strftime("%Y-%m-%d") for d in days]


def base_chain() -> pd.DataFrame:
    rows = []
    for strike in range(80, 121):
        k = float(strike)
        call_delta = max(0.01, min(0.99, 0.5 + (100.0 - k) * 0.04))
        for opt_type, delta in (("call", call_delta), ("put", call_delta - 1.0)):
            mid = max(0.10, 2.0 * min(call_delta, 1 - call_delta) + 1.0)
            rows.append({"expiration": EXPIRATION, "type": opt_type,
                         "strike": k, "bid": mid - 0.05, "ask": mid + 0.05,
                         "iv": 0.25, "delta": round(delta, 4)})
    return pd.DataFrame(rows)


EMPTY = base_chain().iloc[0:0]


class FakeHistory:
    """Serves the synthetic chain scaled by a per-day value factor: each
    leg's mid = entry mid * factor, so factor < 1 means the position has
    decayed toward profit. Days without a factor return an empty frame
    (dataset gap)."""

    def __init__(self, value_factors: dict[str, float]):
        self.factors = value_factors
        self._chain = base_chain()

    def day_chains(self, symbol, day, max_dte=50):
        factor = self.factors.get(day)
        if factor is None:
            return EMPTY.copy()
        chain = self._chain.copy()
        mid = (chain["bid"] + chain["ask"]) / 2.0 * factor
        chain["bid"] = (mid - 0.05).clip(lower=0.0)
        chain["ask"] = mid + 0.05
        return chain


def spot_lookup(spot_by_day: dict[str, float] | None = None,
                default: float = 100.0) -> dict:
    spots = {(SYMBOL, d): default for d in trading_days()}
    for day, s in (spot_by_day or {}).items():
        spots[(SYMBOL, day)] = s
    return spots


def checkpoints():
    return weekly_entry_days(trading_days())


PUT_SPREAD = CreditVariantConfig(name="put_spread", short_put_delta=0.30,
                                 min_credit_frac=0.0)
CONDOR = CreditVariantConfig(name="condor", short_put_delta=0.20,
                             short_call_delta=0.20, min_credit_frac=0.0)


def run_one(history, spots, cfg=PUT_SPREAD):
    engine = ManagedBacktestEngine(history, spots)
    return engine.run([SYMBOL], ENTRY, ENTRY, [cfg])


class TestExitRules:
    def test_profit_target_fires_at_next_checkpoint(self):
        # Value collapses right after entry: target hit at checkpoint 2.
        history = FakeHistory({d: (1.0 if d == ENTRY else 0.01)
                               for d in trading_days()})
        result = run_one(history, spot_lookup())
        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.exit_reason == "profit_target"
        assert t.exit_date == checkpoints()[1]
        assert t.pnl > 0

    def test_breach_exit_on_spot_below_short_put(self):
        # Marks never improve (factor 1.0) so profit target can't fire.
        history = FakeHistory({d: 1.0 for d in trading_days()})
        breach_week = checkpoints()[2]
        result = run_one(history, spot_lookup({breach_week: 50.0}))
        t = result.trades[0]
        assert t.exit_reason == "breach"
        assert t.exit_date == breach_week

    def test_condor_breach_on_upside(self):
        history = FakeHistory({d: 1.0 for d in trading_days()})
        breach_week = checkpoints()[2]
        result = run_one(history, spot_lookup({breach_week: 150.0}), cfg=CONDOR)
        assert result.trades[0].exit_reason == "breach"

    def test_time_exit_at_21_dte(self):
        history = FakeHistory({d: 1.0 for d in trading_days()})
        result = run_one(history, spot_lookup())
        t = result.trades[0]
        assert t.exit_reason == "time_exit"
        # weekly cadence: fires at the FIRST checkpoint at/inside 21 DTE,
        # which can be a few days past the boundary
        exit_dte = (pd.Timestamp(EXPIRATION) - pd.Timestamp(t.exit_date)).days
        assert exit_dte <= 21
        earlier = [c for c in checkpoints() if ENTRY < c < t.exit_date]
        assert all((pd.Timestamp(EXPIRATION) - pd.Timestamp(c)).days > 21
                   for c in earlier)

    def test_dataset_gap_marks_exit_with_model(self):
        # 21-DTE boundary checkpoint has no quotes; the exit still fires
        # there, priced by Black-Scholes at entry IV (mark_source 'model').
        cps = checkpoints()
        boundary = next(
            c for c in cps
            if (pd.Timestamp(EXPIRATION) - pd.Timestamp(c)).days <= 21)
        factors = {d: 1.0 for d in trading_days() if d != boundary}
        result = run_one(FakeHistory(factors), spot_lookup())
        t = result.trades[0]
        assert t.exit_reason == "time_exit"
        assert t.exit_date == boundary
        assert t.mark_source == "model"

    def test_quoted_exit_reports_marks_source(self):
        history = FakeHistory({d: 1.0 for d in trading_days()})
        result = run_one(history, spot_lookup())
        t = result.trades[0]
        assert t.exit_reason == "time_exit"
        assert t.mark_source == "marks"

    def test_no_marks_no_iv_settles_at_intrinsic(self):
        # Gap after entry AND legs without entry IV: model can't price,
        # so the position drifts to expiry settlement.
        factors = {ENTRY: 1.0}
        history = FakeHistory(factors)
        history._chain = history._chain.assign(iv=0.0)
        result = run_one(history, spot_lookup())
        t = result.trades[0]
        assert t.exit_reason == "expired"
        assert t.mark_source == "settlement"
        # OTM at spot 100 -> full credit kept
        assert t.exit_cost == 0.0
        assert t.pnl == pytest.approx(t.credit * 100.0)

    def test_exit_cost_capped_at_width(self):
        # Marks blow out to absurd values; exit cost must cap at width.
        history = FakeHistory({d: (1.0 if d == ENTRY else 100.0)
                               for d in trading_days()})
        breach_week = checkpoints()[2]
        result = run_one(history, spot_lookup({breach_week: 50.0}))
        t = result.trades[0]
        width = t.max_loss / 100.0 + t.credit
        assert t.exit_cost <= width + 1e-9
        assert t.pnl >= -t.max_loss - 1e-6


class TestCadence:
    def test_weekly_entry_days_one_per_iso_week(self):
        days = trading_days("2026-01-05", "2026-01-30")
        entries = weekly_entry_days(days)
        assert entries == ["2026-01-05", "2026-01-12",
                           "2026-01-19", "2026-01-26"]

    def test_no_data_at_entry_counted(self):
        result = run_one(FakeHistory({}), spot_lookup())
        assert result.trades == []
        assert result.skipped_no_data >= 1

    def test_multi_variant_shares_entry(self):
        history = FakeHistory({d: (1.0 if d == ENTRY else 0.01)
                               for d in trading_days()})
        engine = ManagedBacktestEngine(history, spot_lookup())
        result = engine.run([SYMBOL], ENTRY, ENTRY, [PUT_SPREAD, CONDOR])
        assert {t.variant for t in result.trades} == {"put_spread", "condor"}

    def test_weekly_entries_stack_positions(self):
        # Entries allowed for 3 weeks; the third week is only 25 DTE from
        # the sole expiration, below min_dte=30, so 2 positions stack and
        # both time-exit.
        history = FakeHistory({d: 1.0 for d in trading_days()})
        engine = ManagedBacktestEngine(history, spot_lookup())
        result = engine.run([SYMBOL], ENTRY, checkpoints()[2], [PUT_SPREAD])
        assert len(result.trades) == 2
        assert {t.entry_date for t in result.trades} == set(checkpoints()[:2])
        assert result.skipped_no_expiration == 1


class TestSummary:
    def test_summary_aggregates(self):
        history = FakeHistory({d: (1.0 if d == ENTRY else 0.01)
                               for d in trading_days()})
        result = run_one(history, spot_lookup())
        s = result.summary(variant="put_spread")
        assert s["trades"] == 1
        assert s["win_rate"] == 1.0
        assert s["exit_reasons"] == {"profit_target": 1}
        assert result.summary(variant="nope") == {"trades": 0}
