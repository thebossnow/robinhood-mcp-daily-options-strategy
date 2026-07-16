"""Calibration module tests over deterministic synthetic trade sets."""

import math

import pytest

from options_trader.backtest.calibration import (
    WIN, MID, LOSS,
    brier, calibration_report, classify_outcome, ev_calibration,
    reliability_table, wilson_interval,
)


def make_trade(outcome: str, p_win: float, p_loss: float = 0.2,
               ev: float = 5.0, pnl: float | None = None,
               kind: str = "bull_call") -> dict:
    """A bull-call 100/105 (or bear-put 105/100) settled in the given region."""
    if kind == "bull_call":
        long_k, short_k = 100.0, 105.0
        settle = {WIN: 110.0, MID: 102.5, LOSS: 95.0}[outcome]
    else:
        long_k, short_k = 105.0, 100.0
        settle = {WIN: 95.0, MID: 102.5, LOSS: 110.0}[outcome]
    if pnl is None:
        pnl = {WIN: 300.0, MID: 50.0, LOSS: -200.0}[outcome]
    return {
        "kind": kind,
        "long_strike": long_k,
        "short_strike": short_k,
        "settlement_price": settle,
        "p_win_at_entry": p_win,
        "p_loss_at_entry": p_loss,
        "ev_after_costs_at_entry": ev,
        "pnl": pnl,
    }


def calibrated_set() -> list[dict]:
    """Two probability groups whose realized frequencies match exactly:
    100 trades at p_win=0.3 (30 wins) and 100 at p_win=0.6 (60 wins)."""
    trades = []
    for p, n_win in [(0.3, 30), (0.6, 60)]:
        for i in range(100):
            trades.append(make_trade(WIN if i < n_win else LOSS, p_win=p,
                                     p_loss=round(1 - p, 4)))
    return trades


# --- outcome classification -------------------------------------------------

def test_classify_bull_call_regions():
    assert classify_outcome("bull_call", 100, 105, 110) == WIN
    assert classify_outcome("bull_call", 100, 105, 105) == WIN   # exact short strike = full width
    assert classify_outcome("bull_call", 100, 105, 102) == MID
    assert classify_outcome("bull_call", 100, 105, 100) == LOSS  # exact long strike = worthless
    assert classify_outcome("bull_call", 100, 105, 90) == LOSS


def test_classify_bear_put_regions():
    assert classify_outcome("bear_put", 105, 100, 95) == WIN
    assert classify_outcome("bear_put", 105, 100, 100) == WIN
    assert classify_outcome("bear_put", 105, 100, 103) == MID
    assert classify_outcome("bear_put", 105, 100, 105) == LOSS
    assert classify_outcome("bear_put", 105, 100, 120) == LOSS


def test_classify_unknown_kind_raises():
    with pytest.raises(ValueError):
        classify_outcome("iron_condor", 100, 105, 102)


# --- Wilson interval ---------------------------------------------------------

def test_wilson_contains_point_estimate_and_stays_in_unit_interval():
    lo, hi = wilson_interval(30, 100)
    assert 0.0 <= lo < 0.3 < hi <= 1.0
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lo0, hi0 = wilson_interval(0, 50)
    assert lo0 == 0.0 and hi0 > 0.0


def test_wilson_narrows_with_n():
    lo1, hi1 = wilson_interval(5, 10)
    lo2, hi2 = wilson_interval(500, 1000)
    assert (hi2 - lo2) < (hi1 - lo1)


# --- reliability -------------------------------------------------------------

def test_reliability_calibrated_bins_match():
    rows = reliability_table(calibrated_set(), "p_win_at_entry", WIN, n_bins=2)
    assert len(rows) == 2
    for row, expected in zip(rows, [0.3, 0.6]):
        assert row["n"] == 100
        assert row["predicted"] == pytest.approx(expected)
        assert row["realized"] == pytest.approx(expected)
        assert row["within_ci"]


def test_reliability_detects_overconfidence():
    # model says 90% win, reality delivers 10%
    trades = [make_trade(WIN if i < 10 else LOSS, p_win=0.9)
              for i in range(100)]
    (row,) = reliability_table(trades, "p_win_at_entry", WIN, n_bins=1)
    assert row["gap"] == pytest.approx(-0.8)
    assert not row["within_ci"]


def test_reliability_ties_stay_in_one_bin():
    trades = [make_trade(WIN, p_win=0.5) for _ in range(50)]
    rows = reliability_table(trades, "p_win_at_entry", WIN, n_bins=5)
    assert len(rows) == 1 and rows[0]["n"] == 50


def test_reliability_loss_side_uses_p_loss():
    trades = calibrated_set()
    rows = reliability_table(trades, "p_loss_at_entry", LOSS, n_bins=2)
    # p_loss = 1 - p_win here and every non-win settled as full loss
    for row, expected in zip(rows, [0.4, 0.7]):
        assert row["predicted"] == pytest.approx(expected)
        assert row["realized"] == pytest.approx(expected)


# --- Brier -------------------------------------------------------------------

def test_brier_skill_positive_when_informative():
    b = brier(calibrated_set(), "p_win_at_entry", WIN)
    assert b["n"] == 200
    assert b["base_rate"] == pytest.approx(0.45)
    assert b["skill"] > 0


def test_brier_skill_negative_when_anticalibrated():
    trades = [make_trade(LOSS, p_win=0.9) for _ in range(50)] + \
             [make_trade(WIN, p_win=0.1) for _ in range(50)]
    b = brier(trades, "p_win_at_entry", WIN)
    assert b["skill"] < 0


def test_brier_missing_field_reports_empty():
    trades = [make_trade(WIN, p_win=0.5)]
    del trades[0]["p_loss_at_entry"]
    assert brier(trades, "p_loss_at_entry", LOSS) == {"n": 0}


# --- EV calibration ----------------------------------------------------------

def test_ev_slope_one_when_pnl_equals_prediction():
    trades = [make_trade(WIN, p_win=0.5, ev=float(e), pnl=float(e))
              for e in range(1, 101)]
    ev = ev_calibration(trades, n_bins=4)
    assert ev["ols_slope"] == pytest.approx(1.0)
    assert ev["correlation"] == pytest.approx(1.0)
    assert ev["mean_predicted_ev"] == pytest.approx(ev["mean_realized_pnl"])
    for b in ev["bins"]:
        assert b["predicted_ev"] == pytest.approx(b["realized_pnl"])


def test_ev_slope_zero_when_pnl_uncorrelated():
    trades = [make_trade(WIN, p_win=0.5, ev=float(e), pnl=7.0)
              for e in range(1, 101)]
    ev = ev_calibration(trades, n_bins=4)
    assert ev["ols_slope"] == pytest.approx(0.0)


def test_ev_constant_prediction_has_no_slope():
    trades = [make_trade(WIN, p_win=0.5, ev=5.0, pnl=float(p))
              for p in range(10)]
    ev = ev_calibration(trades, n_bins=3)
    assert ev["ols_slope"] is None


# --- report ------------------------------------------------------------------

def test_report_smoke():
    report = calibration_report(calibrated_set(), n_bins=2)
    assert "200 settled trades" in report
    assert "P(win)" in report and "P(loss)" in report
    assert "Brier" in report
    assert "realized P&L" in report
    # 200 trades: the small-sample warning must NOT appear
    assert "anecdote" not in report


def test_report_small_sample_warning_and_missing_p_loss():
    trades = [make_trade(WIN, p_win=0.5) for _ in range(5)]
    for t in trades:
        del t["p_loss_at_entry"]
    report = calibration_report(trades)
    assert "anecdote" in report
    assert "p_loss_at_entry" in report  # points at the stale trade file


def test_report_empty():
    assert "0 settled trades" in calibration_report([])
