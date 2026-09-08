"""CSPPaperBroker: open/close/assign/expire for CSPs, and the assignment ->
shares -> covered-call -> called-away lifecycle."""

import pytest

from options_trader.config import CSPConfig
from options_trader.journal import Journal
from options_trader.risk.csp_manager import CSPRiskManager
from options_trader.execution.paper_csp import CSPPaperBroker
from options_trader.signals.csp import CSPCandidate
from options_trader.signals.covered_call import CoveredCallCandidate


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture
def cfg():
    return CSPConfig(account_equity=25_000.0)


@pytest.fixture
def broker(cfg, journal):
    risk = CSPRiskManager(cfg, journal, net_liq=25_000.0, cash_available=25_000.0)
    return CSPPaperBroker(cfg, journal, risk)


def _cand(underlying="HOOD", strike=30.0, premium=1.0, expiration="2026-09-15"):
    return CSPCandidate(
        underlying=underlying, expiration=expiration, dte=30, spot_at_entry=36.0,
        strike=strike, delta=0.10, otm_pct=0.17, bid=premium - 0.05,
        ask=premium + 0.05, premium_mid=premium, premium=premium,
        open_interest=1000, volume=200, collateral=strike * 100.0,
        roc=premium / strike, annualized_roc=premium / strike * 12,
    )


class TestOpenAndClose:
    def test_open_records_a_trade(self, broker, journal):
        tid, check = broker.open(_cand(), contracts=1)
        assert check.allowed
        assert tid is not None
        rec = journal.get(tid)
        assert rec.strategy == "csp"
        assert rec.status == "open"
        assert rec.short_strike == 30.0
        assert rec.entry_debit == 1.0

    def test_risk_refusal_returns_none(self, broker, journal):
        # exceeds the 20% collateral cap on a 25k account ($5,000): a $200
        # strike collateralizes $20,000/contract.
        tid, check = broker.open(_cand(strike=200.0), contracts=1)
        assert tid is None
        assert not check.allowed

    def test_close_captures_profit(self, broker, journal):
        tid, _ = broker.open(_cand(premium=1.0), contracts=1)
        closed = broker.close(tid, cost_to_close_mid=0.30, half_spread=0.05)
        # exit_cost = 0.30 + 0.5*0.05 = 0.325; pnl = (1.0 - 0.325)*100 = 67.5
        assert closed.status == "closed"
        assert closed.realized_pnl == pytest.approx(67.5)


class TestAssignment:
    def test_expire_worthless_keeps_full_premium(self, broker, journal):
        tid, _ = broker.open(_cand(strike=30.0, premium=1.0), contracts=1)
        closed = broker.expire_worthless(tid, settlement_price=35.0)
        assert closed.status == "expired"
        assert closed.realized_pnl == pytest.approx(100.0)

    def test_assignment_opens_shares_at_cost_basis(self, cfg, journal):
        # A bigger account so 2 contracts (6,000 collateral) fit the 20% cap.
        risk = CSPRiskManager(cfg, journal, net_liq=100_000.0, cash_available=100_000.0)
        broker = CSPPaperBroker(cfg, journal, risk)
        tid, check = broker.open(_cand(strike=30.0, premium=1.0), contracts=2)
        assert check.allowed
        assert journal.get(tid).contracts == 2
        closed, shares_id = broker.assign(tid, settlement_price=27.0)
        # intrinsic = 30 - 27 = 3; pnl = (premium 1.0 - intrinsic 3.0)*100*2 = -400
        assert closed.status == "assigned"
        assert closed.realized_pnl == pytest.approx(-400.0)

        shares = journal.get(shares_id)
        assert shares.strategy == "shares"
        assert shares.contracts == 2          # 200 shares, tracked as 2 lots
        assert shares.short_strike == pytest.approx(29.0)  # 30 - 1 premium collected

    def test_full_lifecycle_called_away(self, broker, journal):
        tid, _ = broker.open(_cand(underlying="HOOD", strike=30.0, premium=1.0), 1)
        _, shares_id = broker.assign(tid, settlement_price=27.0)

        cc = CoveredCallCandidate(
            underlying="HOOD", expiration="2026-10-15", dte=25, spot_at_entry=29.0,
            strike=31.0, delta=0.10, at_or_above_cost_basis=True, cost_basis=29.0,
            bid=0.45, ask=0.55, premium_mid=0.5, premium=0.5,
            open_interest=500, volume=100, roc=0.5 / 29.0,
        )
        cc_id = broker.open_cc(cc, contracts=1)
        assert journal.get(cc_id).strategy == "covered_call"

        cc_closed, shares_closed = broker.call_away(cc_id, shares_id, settlement_price=32.0)
        # call intrinsic = 32 - 31 = 1; pnl = (0.5 - 1.0)*100 = -50
        assert cc_closed.realized_pnl == pytest.approx(-50.0)
        # shares sold at strike 31 vs cost basis 29: pnl = (31-29)*100 = 200
        assert shares_closed.realized_pnl == pytest.approx(200.0)
        assert journal.open_shares_positions() == []
        assert journal.open_cc_positions() == []
