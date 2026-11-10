"""Risk limits are scoped per book.

The credit and income books share one journal table. Before this, the max
open positions limit and the consecutive-loss kill switch counted every
book's rows, so the credit book's positions and losing streak refused
income entries the income book had done nothing to earn (and vice versa).
These tests pin the scoping down, and pin that the account-wide limits
(daily loss, portfolio heat) stay account-wide.
"""

import pytest

from options_trader.config import StrategyConfig
from options_trader.execution.paper import PaperBroker
from options_trader.journal import Journal
from options_trader.risk import RiskManager

from tests.test_credit_paper import make_condor

CONDOR_RISK = make_condor(credit=3.0).max_loss * 100.0   # $2,200 / contract


def scoped_cfg(**over) -> StrategyConfig:
    # Heat cap 2 * 12% * 50k = $12,000, so three condors ($6,600) fit and
    # only the limits under test can refuse.
    base = dict(account_equity=50_000.0, max_risk_per_trade_pct=0.05,
                daily_loss_limit_pct=0.12, max_open_positions=3,
                max_consecutive_losses=3)
    base.update(over)
    return StrategyConfig(**base)


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


def _open(j, strategy):
    return j.record_credit_entry(make_condor(credit=3.0).to_dict(), 1,
                                 strategy=strategy)


def _loss(j, strategy):
    tid = _open(j, strategy)
    j.record_exit(tid, 3.5)          # bought back above the credit: -$50


def _win(j, strategy):
    tid = _open(j, strategy)
    j.record_exit(tid, 1.0)          # +$200


def _reasons(check, text):
    return [r for r in check.reasons if text in r]


class TestJournalScoping:
    def test_open_positions_default_is_every_book(self, journal):
        _open(journal, "credit")
        _open(journal, "income")
        assert len(journal.open_positions()) == 2
        assert len(journal.open_positions("credit")) == 1
        assert len(journal.open_positions("income")) == 1
        assert journal.open_positions("vertical") == []

    def test_consecutive_losses_default_is_every_book(self, journal):
        _loss(journal, "credit")
        _loss(journal, "income")
        assert journal.consecutive_losses() == 2
        assert journal.consecutive_losses("credit") == 1
        assert journal.consecutive_losses("income") == 1
        assert journal.consecutive_losses("vertical") == 0

    def test_other_books_win_does_not_reset_streak(self, journal):
        _loss(journal, "income")
        _win(journal, "credit")
        _loss(journal, "income")
        _loss(journal, "income")
        assert journal.consecutive_losses("income") == 3
        assert journal.consecutive_losses("credit") == 0
        assert journal.consecutive_losses() == 2   # unscoped: as before


class TestKillSwitchScoping:
    def test_credit_losses_do_not_block_income(self, journal):
        cfg = scoped_cfg()
        for _ in range(cfg.max_consecutive_losses):
            _loss(journal, "credit")
        rm = RiskManager(cfg, journal)
        income = rm.check(CONDOR_RISK, today="1999-01-01", strategy="income")
        assert income.allowed, income.reasons
        credit = rm.check(CONDOR_RISK, today="1999-01-01", strategy="credit")
        assert not credit.allowed
        assert _reasons(credit, "kill switch")
        assert _reasons(credit, "credit book")

    def test_income_losses_do_not_block_credit(self, journal):
        cfg = scoped_cfg()
        for _ in range(cfg.max_consecutive_losses):
            _loss(journal, "income")
        rm = RiskManager(cfg, journal)
        assert rm.check(CONDOR_RISK, today="1999-01-01",
                        strategy="credit").allowed
        income = rm.check(CONDOR_RISK, today="1999-01-01", strategy="income")
        assert _reasons(income, "kill switch")

    def test_unscoped_check_still_counts_every_book(self, journal):
        cfg = scoped_cfg()
        _loss(journal, "credit")
        _loss(journal, "income")
        _loss(journal, "credit")
        check = RiskManager(cfg, journal).check(CONDOR_RISK, today="1999-01-01")
        assert not check.allowed
        assert _reasons(check, "kill switch")


class TestOpenPositionScoping:
    def test_credit_positions_do_not_block_income(self, journal):
        cfg = scoped_cfg()
        for _ in range(cfg.max_open_positions):
            _open(journal, "credit")
        rm = RiskManager(cfg, journal)
        income = rm.check(CONDOR_RISK, today="1999-01-01", strategy="income")
        assert income.allowed, income.reasons
        credit = rm.check(CONDOR_RISK, today="1999-01-01", strategy="credit")
        assert _reasons(credit, "max open positions")
        assert not rm.check(CONDOR_RISK, today="1999-01-01").allowed

    def test_income_positions_do_not_block_credit(self, journal):
        cfg = scoped_cfg()
        for _ in range(cfg.max_open_positions):
            _open(journal, "income")
        rm = RiskManager(cfg, journal)
        assert rm.check(CONDOR_RISK, today="1999-01-01",
                        strategy="credit").allowed
        assert _reasons(rm.check(CONDOR_RISK, today="1999-01-01",
                                 strategy="income"), "max open positions")


class TestAccountWideLimitsStayAccountWide:
    def test_portfolio_heat_counts_every_book(self, journal):
        # Heat cap $12,000; five credit condors carry $11,000 of open risk,
        # so one more $2,200 condor in ANY book would breach it.
        cfg = scoped_cfg(max_open_positions=10)
        for _ in range(5):
            _open(journal, "credit")
        check = RiskManager(cfg, journal).check(
            CONDOR_RISK, today="1999-01-01", strategy="income")
        assert not check.allowed
        assert _reasons(check, "portfolio heat")

    def test_daily_loss_limit_counts_every_book(self, journal):
        cfg = scoped_cfg(daily_loss_limit_pct=0.001)   # $50 limit
        tid = _open(journal, "credit")
        journal.record_exit(tid, 3.5)                  # -$50 today
        day = journal.get(tid).closed_at[:10]
        check = RiskManager(cfg, journal).check(
            CONDOR_RISK, today=day, strategy="income")
        assert not check.allowed
        assert _reasons(check, "daily loss limit")


class TestBrokerPassesItsBook:
    def test_open_credit_scopes_to_the_book_it_tags(self, journal):
        cfg = scoped_cfg()
        broker = PaperBroker(cfg, journal)
        for _ in range(cfg.max_consecutive_losses):
            _loss(journal, "credit")
        tid, check = broker.open_credit(make_condor(credit=3.0), 1,
                                        strategy="income")
        assert tid is not None, check.reasons
        assert journal.get(tid).strategy == "income"
        tid, check = broker.open_credit(make_condor(credit=3.0), 1)
        assert tid is None
        assert _reasons(check, "kill switch")
