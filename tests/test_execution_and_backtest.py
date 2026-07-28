from copy import deepcopy
from datetime import datetime, timedelta

import pandas as pd
import pytest

from options_trader.backtest import BacktestEngine
from options_trader.data.provider import ChainSnapshot
from options_trader.execution import PaperBroker, settlement_value
from options_trader.execution.paper import take_profit_target
from options_trader.journal import Journal
from options_trader.signals import generate_candidates


@pytest.fixture
def broker(cfg, tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield PaperBroker(cfg, j)
    j.close()


def _top_candidate(cfg, snapshot):
    cands = generate_candidates(snapshot, cfg)
    assert cands
    return cands[0]


class TestSettlementValue:
    def test_bull_call_regions(self):
        assert settlement_value("bull_call", 100, 102, 99.0) == 0.0     # max loss
        assert settlement_value("bull_call", 100, 102, 101.0) == 1.0    # middle
        assert settlement_value("bull_call", 100, 102, 105.0) == 2.0    # max win

    def test_bear_put_regions(self):
        assert settlement_value("bear_put", 100, 98, 101.0) == 0.0
        assert settlement_value("bear_put", 100, 98, 99.0) == 1.0
        assert settlement_value("bear_put", 100, 98, 95.0) == 2.0


class TestPaperBroker:
    def test_open_applies_slippage(self, cfg, snapshot, broker):
        cand = _top_candidate(cfg, snapshot)
        tid, check = broker.open(cand, contracts=1)
        assert tid is not None and check.allowed
        rec = broker.journal.get(tid)
        assert rec.entry_debit > cand.debit  # never filled at mid

    def test_close_realizes_pnl_with_exit_slippage(self, cfg, snapshot, broker):
        cand = _top_candidate(cfg, snapshot)
        tid, _ = broker.open(cand, contracts=1)
        entry = broker.journal.get(tid).entry_debit
        rec = broker.close(tid, current_mid_value=entry + 0.50)
        # exit mid is +0.50 over entry but slippage is deducted again
        assert 0 < rec.realized_pnl < 50.0

    def test_settle_expired_uses_intrinsic(self, cfg, snapshot, broker):
        cand = _top_candidate(cfg, snapshot)
        tid, _ = broker.open(cand, contracts=1)
        entry = broker.journal.get(tid).entry_debit
        settle_px = 90.0  # deep through one side of every spread in the chain
        expected_value = settlement_value(
            cand.kind, cand.long_strike, cand.short_strike, settle_px
        )
        rec = broker.settle_expired(tid, settlement_price=settle_px)
        assert rec.status == "expired"
        assert rec.realized_pnl == pytest.approx((expected_value - entry) * 100)

    def test_risk_manager_gates_open(self, cfg, snapshot, broker):
        cand = _top_candidate(cfg, snapshot)
        for _ in range(cfg.max_open_positions):
            tid, _ = broker.open(cand, contracts=1)
            assert tid is not None
        tid, check = broker.open(cand, contracts=1)
        assert tid is None
        assert not check.allowed


class TestBacktestEngine:
    def test_settles_wins_and_losses(self, cfg, snapshot):
        engine = BacktestEngine(cfg)
        # Settle far below and far above spot: for any vertical, one side is
        # max profit and the other max loss.
        low = engine.run(
            [snapshot], {("TEST", snapshot.expiration): 90.0},
            per_snapshot_trades=1,
        )
        high = engine.run(
            [snapshot], {("TEST", snapshot.expiration): 110.0},
            per_snapshot_trades=1,
        )
        assert low.summary["trades"] == 1 and high.summary["trades"] == 1
        pnls = sorted([low.trades[0]["pnl"], high.trades[0]["pnl"]])
        assert pnls[0] < 0 < pnls[1]

    def test_unsettled_expiries_are_skipped_not_guessed(self, cfg, snapshot):
        result = BacktestEngine(cfg).run([snapshot], settlements={})
        assert result.summary["trades"] == 0
        assert result.skipped_unsettled >= 1

    def test_expired_trades_have_exit_reason(self, cfg, snapshot):
        result = BacktestEngine(cfg).run(
            [snapshot], {("TEST", snapshot.expiration): 90.0}
        )
        assert result.trades[0]["exit_reason"] == "expired"
        assert result.trades[0]["exit_date"] == snapshot.expiration

    def test_summary_includes_exit_reasons(self, cfg, snapshot):
        result = BacktestEngine(cfg).run(
            [snapshot], {("TEST", snapshot.expiration): 90.0}
        )
        assert "exit_reasons" in result.summary
        assert result.summary["exit_reasons"].get("expired", 0) >= 1


def _later_snapshot(snapshot: ChainSnapshot, minutes: int,
                    value_factor: float) -> ChainSnapshot:
    """Returns a copy of snapshot taken `minutes` later with all mids scaled."""
    later = (datetime.fromisoformat(snapshot.taken_at)
             + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    chain = snapshot.chain.copy()
    mid = (chain["bid"] + chain["ask"]) / 2.0 * value_factor
    half = (chain["ask"] - chain["bid"]) / 2.0
    chain["bid"] = (mid - half).clip(lower=0.0)
    chain["ask"] = mid + half
    return ChainSnapshot(
        underlying=snapshot.underlying,
        spot=snapshot.spot,
        expiration=snapshot.expiration,
        taken_at=later,
        chain=chain,
    )


class TestTakeProfitBacktest:
    def test_take_profit_fires_when_spread_appreciates(self, cfg, snapshot):
        # Scale mids to 4x — the spread will be worth ~width, well above target.
        later = _later_snapshot(snapshot, minutes=60, value_factor=4.0)
        result = BacktestEngine(cfg).run(
            [snapshot, later], settlements={}, per_snapshot_trades=1
        )
        assert result.trades, "expected a take-profit trade"
        t = result.trades[0]
        assert t["exit_reason"] == "take_profit"
        assert t["pnl"] > 0
        assert t["exit_date"] == later.taken_at[:10]

    def test_falls_through_to_settlement_when_not_hit(self, cfg, snapshot):
        # Scale mids to 0.5x — spread depreciates, take-profit never fires.
        later = _later_snapshot(snapshot, minutes=60, value_factor=0.5)
        settlements = {("TEST", snapshot.expiration): 90.0}
        result = BacktestEngine(cfg).run(
            [snapshot, later], settlements=settlements, per_snapshot_trades=1
        )
        # entry snapshot + later snapshot both generate trades, but entries
        # in the later snapshot have no further snapshots to take profit from.
        tp_trades = [t for t in result.trades if t["exit_reason"] == "take_profit"]
        exp_trades = [t for t in result.trades if t["exit_reason"] == "expired"]
        assert not tp_trades
        assert exp_trades

    def test_take_profit_pct_1_disables_early_exit(self, cfg, snapshot):
        cfg_hold = deepcopy(cfg)
        cfg_hold.take_profit_pct = 1.0
        later = _later_snapshot(snapshot, minutes=60, value_factor=4.0)
        settlements = {("TEST", snapshot.expiration): 90.0}
        result = BacktestEngine(cfg_hold).run(
            [snapshot, later], settlements=settlements, per_snapshot_trades=1
        )
        assert all(t["exit_reason"] == "expired" for t in result.trades)

    def test_take_profit_target_helper(self, cfg):
        # entry=1.0, width=5.0, pct=0.75 → target = 1 + 0.75*4 = 4.0
        assert take_profit_target(1.0, 5.0, 0.75) == pytest.approx(4.0)

    def test_paper_broker_take_profit_target(self, cfg, snapshot, broker):
        cand = _top_candidate(cfg, snapshot)
        tid, _ = broker.open(cand)
        rec = broker.journal.get(tid)
        tp = broker.take_profit_target(rec)
        expected = rec.entry_debit + cfg.take_profit_pct * (rec.width - rec.entry_debit)
        assert tp == pytest.approx(expected)
