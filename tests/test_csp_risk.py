"""CSPRiskManager: collateral cap, cash buffer floor, one-underlying-at-a-
time with the QQQ-ladder exception, kill switch, earnings blackout."""

from datetime import date

import pytest

from options_trader.config import CSPConfig
from options_trader.journal import Journal
from options_trader.risk.csp_manager import CSPRiskManager


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture
def cfg():
    return CSPConfig(account_equity=25_000.0)


def _csp_candidate(underlying="QQQ", strike=400.0, premium=1.0, delta=0.10,
                   expiration="2026-09-15"):
    return {
        "underlying": underlying, "expiration": expiration, "dte": 30,
        "spot_at_entry": strike * 1.2, "strike": strike, "delta": delta,
        "otm_pct": 0.17, "bid": premium - 0.05, "ask": premium + 0.05,
        "premium_mid": premium, "premium": premium, "open_interest": 1000,
        "volume": 200, "collateral": strike * 100, "roc": premium / strike,
        "annualized_roc": premium / strike * 12,
    }


class TestCollateralCap:
    def test_refuses_when_underlying_cap_exceeded(self, cfg, journal):
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0)
        # cap = 20% of 25k = $5,000; a $40 strike collateralizes $4,000/contract
        check = risk.check("QQQ", strike=60.0, premium=1.0)
        assert not check.allowed
        assert any("collateral cap" in r for r in check.reasons)

    def test_allows_within_cap(self, cfg, journal):
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0)
        check = risk.check("QQQ", strike=40.0, premium=1.0)  # $4,000 < $5,000 cap
        assert check.allowed
        assert check.max_contracts == 1

    def test_existing_collateral_counts_against_cap(self, cfg, journal):
        journal.record_csp_entry(_csp_candidate(strike=40.0), contracts=1)
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0)
        # 4,000 already committed; cap is 5,000 -> only 1,000 of room left,
        # not enough for another $4,000 contract.
        check = risk.check("QQQ", strike=40.0, premium=1.0)
        assert not check.allowed


class TestCashBuffer:
    def test_refuses_when_buffer_would_be_breached(self, cfg, journal):
        # floor = 30% of 25k = 7,500; only 8,000 available -> a $4,000
        # contract would leave 4,000, below the floor.
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=8_000.0)
        check = risk.check("QQQ", strike=40.0, premium=1.0)
        assert not check.allowed
        assert any("cash buffer" in r for r in check.reasons)

    def test_allows_when_buffer_holds(self, cfg, journal):
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=15_000.0)
        check = risk.check("QQQ", strike=40.0, premium=1.0)
        assert check.allowed


class TestOneUnderlyingAtATime:
    def test_second_non_ladder_underlying_refused(self, cfg, journal):
        journal.record_csp_entry(_csp_candidate(underlying="HOOD", strike=30.0), 1)
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0)
        check = risk.check("PLTR", strike=30.0, premium=0.5)
        assert not check.allowed
        assert any("one underlying at a time" in r for r in check.reasons)

    def test_adding_to_same_underlying_allowed(self, cfg, journal):
        # Small enough that both stay inside the 20%-of-25k ($5,000) cap.
        journal.record_csp_entry(_csp_candidate(underlying="HOOD", strike=15.0), 1)
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0)
        check = risk.check("HOOD", strike=14.0, premium=0.4)
        assert check.allowed

    def test_qqq_ladder_allowed_alongside_other_open_names(self, cfg, journal):
        journal.record_csp_entry(_csp_candidate(underlying="HOOD", strike=30.0), 1)
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0)
        check = risk.check("QQQ", strike=40.0, premium=1.0)
        assert check.allowed

    def test_qqq_ladder_cap_enforced(self, cfg, journal):
        rungs = [(40.0, "2026-09-15"), (39.0, "2026-09-22"),
                 (38.0, "2026-10-15"), (37.0, "2026-10-22")]
        for strike, expiration in rungs:
            journal.record_csp_entry(
                _csp_candidate(underlying="QQQ", strike=strike, expiration=expiration),
                contracts=1)
        risk = CSPRiskManager(cfg, journal, net_liq=1_000_000.0, cash_available=1_000_000.0)
        check = risk.check("QQQ", strike=36.0, premium=1.0)
        assert not check.allowed
        assert any("ladder full" in r for r in check.reasons)


class TestKillSwitch:
    def test_consecutive_losses_block_new_entries(self, cfg, journal):
        for i in range(cfg.max_consecutive_losses):
            tid = journal.record_csp_entry(
                _csp_candidate(strike=30.0, expiration=f"2026-0{1+i}-15"), 1)
            journal.record_exit(tid, exit_value=2.0)  # loss: bought back above premium
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0)
        check = risk.check("QQQ", strike=40.0, premium=1.0)
        assert not check.allowed
        assert any("kill switch" in r for r in check.reasons)


class TestEarningsBlackout:
    def test_blocks_within_window(self, cfg, journal):
        overrides = {"HOOD": ["2026-09-10"]}
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0,
                              earnings_overrides=overrides)
        check = risk.check("HOOD", strike=30.0, premium=0.5, today=date(2026, 9, 9))
        assert not check.allowed
        assert any("earnings blackout" in r for r in check.reasons)

    def test_allows_outside_window(self, cfg, journal):
        overrides = {"HOOD": ["2026-09-10"]}
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0,
                              earnings_overrides=overrides)
        check = risk.check("HOOD", strike=30.0, premium=0.5, today=date(2026, 9, 20))
        assert check.allowed

    def test_index_etf_not_earnings_sensitive(self, cfg, journal):
        overrides = {"QQQ": ["2026-09-10"]}  # ignored: QQQ isn't in earnings_sensitive
        risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0,
                              earnings_overrides=overrides)
        check = risk.check("QQQ", strike=40.0, premium=1.0, today=date(2026, 9, 10))
        assert check.allowed
