"""Tests for the live-fills anchor: options_trader/journal/live_calibration.py
and loop/audit_live.py's drift check."""

import pytest

from loop.audit_live import check_drift
from options_trader.journal import Journal
from options_trader.journal.live_calibration import (
    closed_trades_with_predicted_ev, live_gap_report, live_halt_reason,
)


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


def _candidate(p_win=0.35, ev=10.0):
    return {
        "underlying": "TEST", "expiration": "2026-07-11", "kind": "bull_call",
        "long_strike": 100.0, "short_strike": 102.0, "width": 2.0,
        "debit": 0.60, "p_win": p_win, "ev_after_costs": ev,
    }


def _close(journal, entry_debit, exit_value, ev=10.0):
    tid = journal.record_entry(_candidate(ev=ev), 1, entry_debit=entry_debit)
    return journal.record_exit(tid, exit_value=exit_value)


class TestClosedTradesWithPredictedEv:
    def test_filters_to_recorded_predictions_only(self, journal):
        _close(journal, 0.60, 1.20, ev=10.0)  # win, has ev_after_costs
        pos_id = journal.record_credit_entry(
            {"underlying": "TEST", "variant": "put_credit", "entry_date": "2026-07-01",
             "expiration": "2026-07-11", "dte_at_entry": 10, "spot_at_entry": 100.0,
             "legs": [{"type": "put", "strike": 95.0, "side": 1, "entry_bid": 0.1,
                       "entry_ask": 0.2, "entry_iv": 0.3},
                      {"type": "put", "strike": 100.0, "side": -1, "entry_bid": 0.5,
                       "entry_ask": 0.6, "entry_iv": 0.3}],
             "credit_mid": 0.3, "credit": 0.28, "credit_frac": 0.056, "max_loss": 4.72},
            contracts=1,
        )
        journal.record_exit(pos_id, exit_value=0.10)  # credit trade: no ev recorded

        trades = closed_trades_with_predicted_ev(journal.closed_trades())
        assert len(trades) == 1
        assert trades[0]["ev_after_costs_at_entry"] == 10.0
        assert trades[0]["pnl"] == pytest.approx(60.0)


class TestLiveGapReport:
    def test_reports_gap_against_backtest(self, journal):
        for _ in range(3):
            _close(journal, 0.60, 1.20, ev=10.0)  # +$60 realized each
        report = live_gap_report(journal.closed_trades(), n_bins=1,
                                 backtest_expectancy=100.0)
        assert "3 closed trades" in report
        assert "anecdote" in report  # below MIN_TRADES_FOR_VERDICT
        assert "Backtest-vs-live gap" in report
        assert "live underperforms" in report  # 60 realized << 100 predicted

    def test_empty_journal_reports_zero(self, journal):
        report = live_gap_report(journal.closed_trades())
        assert "0 closed trades" in report
        assert "No trades with p_win/ev_after_costs" in report


class TestLiveHaltReason:
    def test_missing_file_means_not_halted(self, tmp_path):
        assert live_halt_reason(tmp_path / "absent.json") is None

    def test_present_file_returns_reasons(self, tmp_path):
        import json
        path = tmp_path / "live_halt.json"
        path.write_text(json.dumps({"reasons": ["mean realized P&L -5.00/trade <= 0"]}))
        reason = live_halt_reason(path)
        assert "mean realized P&L" in reason


class TestCheckDrift:
    def test_no_verdict_below_min_trades(self, journal):
        for _ in range(5):
            _close(journal, 0.60, 1.20, ev=10.0)
        halt, details = check_drift(journal.closed_trades())
        assert not halt
        assert "no verdict yet" in details["verdict"]

    def test_halts_when_slope_and_pnl_collapse(self, journal):
        # Predicted EV always positive; realized P&L always negative and flat
        # (no relationship to the prediction) -> slope 0, mean pnl < 0.
        for i in range(45):
            _close(journal, 1.00, 0.50, ev=10.0 + i)  # -$50 every time
        halt, details = check_drift(journal.closed_trades())
        assert halt
        assert details["n"] == 45
        assert any("slope" in r for r in details["reasons"])
        assert any("mean realized P&L" in r for r in details["reasons"])

    def test_does_not_halt_when_calibrated(self, journal):
        # Realized P&L tracks predicted EV closely and stays positive.
        for i in range(45):
            ev = float(i + 1)
            _close(journal, 0.60, 0.60 + (ev / 100.0), ev=ev)
        halt, details = check_drift(journal.closed_trades())
        assert not halt
        assert details["reasons"] == []
