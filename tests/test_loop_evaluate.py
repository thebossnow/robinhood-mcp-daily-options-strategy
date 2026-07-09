"""Tests for the autoresearch-loop verifier and the intraday snapshot fix."""

import pandas as pd
import pytest

from loop.evaluate import (
    MIN_SCAN_DAYS,
    MIN_SETTLED_TRADES,
    Verdict,
    data_readiness,
    decide,
    dedupe_daily,
    guard_risk_fields,
    required_margin,
    split_walk_forward,
)
from options_trader.config import StrategyConfig
from options_trader.data.provider import ChainSnapshot, SnapshotStore, CHAIN_COLUMNS


def _snap(day, time="10:00:00", und="SPY", exp="2026-08-21"):
    return ChainSnapshot(
        underlying=und, spot=100.0, expiration=exp,
        taken_at=f"{day}T{time}",
        chain=pd.DataFrame(columns=CHAIN_COLUMNS),
    )


class TestRiskFieldGuard:
    def test_signal_field_changes_pass(self):
        base, cand = StrategyConfig(), StrategyConfig(min_p_win=0.30, min_volume=75)
        guard_risk_fields(base, cand)  # no raise

    @pytest.mark.parametrize("f,v", [
        ("daily_loss_limit_pct", 0.5),
        ("max_risk_per_trade_pct", 0.2),
        ("account_equity", 1_000_000.0),
        ("max_open_positions", 50),
        ("max_consecutive_losses", 99),
    ])
    def test_frozen_field_changes_refused(self, f, v):
        base, cand = StrategyConfig(), StrategyConfig(**{f: v})
        with pytest.raises(ValueError, match="frozen risk fields"):
            guard_risk_fields(base, cand)


class TestSnapshotHygiene:
    def test_dedupe_keeps_earliest_per_day(self):
        snaps = [
            _snap("2026-07-09", "09:45:00"),
            _snap("2026-07-09", "10:30:00"),   # same day dup — dropped
            _snap("2026-07-09", "10:30:00", exp="2026-09-18"),  # different exp — kept
            _snap("2026-07-10", "09:45:00"),
        ]
        out = dedupe_daily(snaps)
        assert len(out) == 3
        same_day = [s for s in out if s.taken_at.startswith("2026-07-09")
                    and s.expiration == "2026-08-21"]
        assert same_day[0].taken_at.endswith("09:45:00")

    def test_readiness_gate(self):
        few = [_snap(f"2026-07-{d:02d}") for d in range(1, 6)]
        ready, info = data_readiness(few)
        assert not ready and info["distinct_scan_days"] == 5
        many = [_snap(f"2026-{m:02d}-{d:02d}")
                for m in (5, 6) for d in range(1, MIN_SCAN_DAYS // 2 + 2)]
        assert data_readiness(many)[0]

    def test_walk_forward_splits_on_day_boundary(self):
        snaps = [_snap(f"2026-07-{d:02d}") for d in range(1, 11)]
        train, oos = split_walk_forward(snaps, oos_fraction=0.3)
        train_days = {s.taken_at[:10] for s in train}
        oos_days = {s.taken_at[:10] for s in oos}
        assert not train_days & oos_days
        assert max(train_days) < min(oos_days)  # OOS is strictly the future
        assert len(oos_days) == 3

    def test_intraday_snapshots_do_not_overwrite(self, tmp_path):
        store = SnapshotStore(tmp_path)
        store.save(_snap("2026-07-09", "09:45:00"))
        store.save(_snap("2026-07-09", "10:30:00"))
        assert len(list(tmp_path.rglob("*.csv"))) == 2
        assert len(store.load_all()) == 2


def _metrics(trades=50, exp=10.0, dd=-100.0):
    return {"trades": trades, "expectancy_per_trade": exp, "max_drawdown": dd}


class TestDecision:
    LIMIT = -400.0

    def test_accepts_clear_improvement(self):
        v = decide(_metrics(exp=15.0), _metrics(exp=10.0),
                   _metrics(exp=12.0), _metrics(exp=9.0),
                   attempts=0, max_dd_limit=self.LIMIT)
        assert v.accepted and v.reasons == []

    def test_rejects_insufficient_sample(self):
        v = decide(_metrics(trades=MIN_SETTLED_TRADES - 1, exp=50.0),
                   _metrics(), _metrics(exp=12.0), _metrics(exp=9.0),
                   attempts=0, max_dd_limit=self.LIMIT)
        assert not v.accepted
        assert any("insufficient" in r for r in v.reasons)

    def test_rejects_marginal_improvement_below_bar(self):
        # +0.2 improvement < required margin (max(5% of 10, $1) = $1)
        v = decide(_metrics(exp=10.2), _metrics(exp=10.0),
                   _metrics(exp=12.0), _metrics(exp=9.0),
                   attempts=0, max_dd_limit=self.LIMIT)
        assert not v.accepted

    def test_escalating_margin_with_attempts(self):
        assert required_margin(10.0, attempts=4) == pytest.approx(
            required_margin(10.0, attempts=0) * 3.0
        )
        # exp=11.2 clears attempt 0 (margin 1.0) but not attempt 4 (margin 3.0)
        args = (_metrics(exp=11.2), _metrics(exp=10.0),
                _metrics(exp=12.0), _metrics(exp=9.0))
        assert decide(*args, attempts=0, max_dd_limit=self.LIMIT).accepted
        assert not decide(*args, attempts=4, max_dd_limit=self.LIMIT).accepted

    def test_rejects_absolute_drawdown_breach(self):
        v = decide(_metrics(exp=20.0, dd=-500.0), _metrics(exp=10.0),
                   _metrics(exp=12.0), _metrics(exp=9.0),
                   attempts=0, max_dd_limit=self.LIMIT)
        assert not v.accepted
        assert any("breaches limit" in r for r in v.reasons)

    def test_rejects_drawdown_regression(self):
        v = decide(_metrics(exp=20.0, dd=-200.0), _metrics(exp=10.0, dd=-100.0),
                   _metrics(exp=12.0), _metrics(exp=9.0),
                   attempts=0, max_dd_limit=self.LIMIT)
        assert not v.accepted
        assert any("regressed" in r for r in v.reasons)

    def test_rejects_oos_regression_and_nonpositive_oos(self):
        v = decide(_metrics(exp=20.0), _metrics(exp=10.0),
                   _metrics(exp=-1.0), _metrics(exp=5.0),
                   attempts=0, max_dd_limit=self.LIMIT)
        assert not v.accepted
        assert any("not positive" in r for r in v.reasons)
        assert any("regression" in r for r in v.reasons)

    def test_rejects_when_no_oos_trades(self):
        v = decide(_metrics(exp=20.0), _metrics(exp=10.0),
                   _metrics(trades=0), _metrics(trades=0),
                   attempts=0, max_dd_limit=self.LIMIT)
        assert not v.accepted
        assert any("out-of-sample settled trades" in r for r in v.reasons)

    def test_verdict_carries_details(self):
        v = decide(_metrics(exp=15.0), _metrics(exp=10.0),
                   _metrics(exp=12.0), _metrics(exp=9.0),
                   attempts=0, max_dd_limit=self.LIMIT)
        assert isinstance(v, Verdict)
        assert v.details["required_margin"] == pytest.approx(1.0)
