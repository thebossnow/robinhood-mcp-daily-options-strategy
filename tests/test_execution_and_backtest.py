import pytest

from options_trader.backtest import BacktestEngine
from options_trader.execution import PaperBroker, settlement_value
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
