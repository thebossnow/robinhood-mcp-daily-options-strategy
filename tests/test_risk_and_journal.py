import pytest

from options_trader.journal import Journal
from options_trader.risk import RiskManager


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


def _fake_candidate(**kw):
    base = {
        "underlying": "TEST", "expiration": "2026-07-11", "kind": "bull_call",
        "long_strike": 100.0, "short_strike": 102.0, "width": 2.0,
        "debit": 0.60, "p_win": 0.35, "ev_after_costs": 20.0,
    }
    base.update(kw)
    return base


class TestRiskManager:
    def test_allows_and_sizes_normal_trade(self, cfg, journal):
        check = RiskManager(cfg, journal).check(max_loss_per_contract=60.0)
        assert check.allowed
        # 1% of 10k = $100 cap -> 1 contract at $60 risk
        assert check.max_contracts == 1

    def test_refuses_oversized_single_contract(self, cfg, journal):
        check = RiskManager(cfg, journal).check(max_loss_per_contract=150.0)
        assert not check.allowed
        assert any("per-trade cap" in r for r in check.reasons)

    def test_daily_loss_limit(self, cfg, journal):
        # Realize a -$250 day (limit is 2% of 10k = $200)
        tid = journal.record_entry(_fake_candidate(), contracts=1, entry_debit=3.0)
        journal.record_exit(tid, exit_value=0.5)  # -250
        day = journal.get(tid).closed_at[:10]
        check = RiskManager(cfg, journal).check(60.0, today=day)
        assert not check.allowed
        assert any("daily loss limit" in r for r in check.reasons)

    def test_consecutive_loss_kill_switch(self, cfg, journal):
        for _ in range(cfg.max_consecutive_losses):
            tid = journal.record_entry(_fake_candidate(), 1, entry_debit=0.60)
            journal.record_exit(tid, exit_value=0.30)  # loss
        check = RiskManager(cfg, journal).check(60.0, today="1999-01-01")
        assert not check.allowed
        assert any("kill switch" in r for r in check.reasons)

    def test_win_resets_streak(self, cfg, journal):
        for _ in range(2):
            tid = journal.record_entry(_fake_candidate(), 1, entry_debit=0.60)
            journal.record_exit(tid, exit_value=0.30)
        tid = journal.record_entry(_fake_candidate(), 1, entry_debit=0.60)
        journal.record_exit(tid, exit_value=1.20)  # win
        assert journal.consecutive_losses() == 0

    def test_max_open_positions(self, cfg, journal):
        for _ in range(cfg.max_open_positions):
            journal.record_entry(_fake_candidate(), 1, entry_debit=0.30)
        check = RiskManager(cfg, journal).check(30.0, today="1999-01-01")
        assert not check.allowed
        assert any("max open positions" in r for r in check.reasons)


class TestJournal:
    def test_entry_exit_roundtrip_pnl(self, journal):
        tid = journal.record_entry(_fake_candidate(), contracts=2, entry_debit=0.65)
        rec = journal.record_exit(tid, exit_value=1.30)
        assert rec.status == "closed"
        # (1.30 - 0.65) * 100 * 2
        assert rec.realized_pnl == pytest.approx(130.0)

    def test_cannot_close_twice(self, journal):
        tid = journal.record_entry(_fake_candidate(), 1, entry_debit=0.60)
        journal.record_exit(tid, exit_value=1.0)
        with pytest.raises(ValueError):
            journal.record_exit(tid, exit_value=1.0)

    def test_open_risk_sums_open_only(self, journal):
        journal.record_entry(_fake_candidate(), 1, entry_debit=0.60)   # $60
        tid = journal.record_entry(_fake_candidate(), 1, entry_debit=0.40)
        journal.record_exit(tid, exit_value=0.80)
        assert journal.open_risk() == pytest.approx(60.0)

    def test_stats(self, journal):
        for exit_v in (1.20, 0.30):  # one win (+60), one loss (-30)
            tid = journal.record_entry(_fake_candidate(), 1, entry_debit=0.60)
            journal.record_exit(tid, exit_value=exit_v)
        s = journal.stats()
        assert s["closed_trades"] == 2
        assert s["win_rate"] == 0.5
        assert s["total_pnl"] == pytest.approx(30.0)
        assert s["max_drawdown"] == pytest.approx(-30.0)
