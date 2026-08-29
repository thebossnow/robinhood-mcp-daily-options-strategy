"""Wheel state machine: Phase A entry/settlement, Phase B entry/settlement,
the assignment-vs-max-loss split, the drop-from-basis halt, and the
ex-dividend guard."""

import pandas as pd
import pytest

from options_trader.signals.wheel import (
    ShareLot, WheelConfig, blocked_by_ex_dividend, build_phase_a,
    build_phase_b, resolve_covered_call_settlement,
    resolve_put_spread_settlement,
)


def make_chain(spot: float = 100.0) -> pd.DataFrame:
    """Synthetic single-expiration chain around spot, strikes every 1.0
    from 80 to 120, with a monotonic delta ladder and tight quotes."""
    rows = []
    for strike in range(80, 121):
        k = float(strike)
        call_delta = max(0.01, min(0.99, 0.5 + (spot - k) * 0.04))
        put_delta = call_delta - 1.0
        for opt_type, delta in (("call", call_delta), ("put", put_delta)):
            if opt_type == "call":
                mid = max(0.05, (spot - k) + 3.0 if k < spot else 3.0 * call_delta * 2)
            else:
                mid = max(0.05, (k - spot) + 3.0 if k > spot else 3.0 * abs(put_delta) * 2)
            rows.append({
                "type": opt_type, "strike": k,
                "bid": round(mid - 0.05, 2), "ask": round(mid + 0.05, 2),
                "iv": 0.25, "delta": round(delta, 4),
            })
    return pd.DataFrame(rows)


class TestPhaseAVariant:
    def test_breach_exit_is_off(self):
        # Assignment requires riding a tested short strike to expiration;
        # a breach exit would close the spread in cash the moment spot
        # first crosses the short strike, long before that -- see
        # phase_a_variant's docstring for the empirical regression this
        # guards (230 wheel trades, zero assignments, before this fix).
        assert WheelConfig().phase_a_variant().exit_on_breach is False


class TestPhaseAEntry:
    def test_builds_put_credit_spread_near_target_delta(self):
        cfg = WheelConfig(put_delta_target=0.25)
        pos = build_phase_a(make_chain(), 100.0, "F", "2026-01-05",
                            "2026-02-13", 39, cfg)
        assert pos is not None
        short = [l for l in pos.legs if l.side == -1][0]
        wing = [l for l in pos.legs if l.side == +1][0]
        assert short.type == "put"
        assert abs(abs(short.entry_delta) - 0.25) < 0.05
        assert wing.strike < short.strike
        assert pos.max_loss == pytest.approx(
            max(pos.widths().values()) - pos.credit, abs=1e-6)


class TestPutSpreadSettlement:
    def _position(self):
        cfg = WheelConfig(put_delta_target=0.25, put_wing_width_frac=0.02)
        return build_phase_a(make_chain(), 100.0, "F", "2026-01-05",
                             "2026-02-13", 39, cfg)

    def test_expired_otm_keeps_credit_no_lot(self):
        pos = self._position()
        outcome, pnl, lot = resolve_put_spread_settlement(pos, pos.short_put_strike + 1.0)
        assert outcome == "expired"
        assert pnl == pos.credit
        assert lot is None

    def test_assigned_between_wing_and_short_strike(self):
        pos = self._position()
        wing_strike = next(l.strike for l in pos.legs if l.side == +1)
        settlement = (pos.short_put_strike + wing_strike) / 2.0
        outcome, pnl, lot = resolve_put_spread_settlement(pos, settlement)
        assert outcome == "assigned"
        assert pnl == 0.0
        assert lot is not None
        assert lot.shares == 100
        assert lot.underlying == "F"
        assert lot.cost_basis == pytest.approx(
            pos.short_put_strike - pos.credit, abs=1e-6)

    def test_max_loss_when_both_legs_itm(self):
        pos = self._position()
        wing_strike = next(l.strike for l in pos.legs if l.side == +1)
        outcome, pnl, lot = resolve_put_spread_settlement(pos, wing_strike - 5.0)
        assert outcome == "max_loss"
        assert lot is None
        assert pnl == pytest.approx(pos.credit - pos.max_loss, abs=1e-6)
        # the defined-risk guarantee: never worse than max_loss
        assert pnl >= -pos.max_loss - 1e-6


class TestPhaseBEntry:
    def test_selects_call_near_target_delta(self):
        cfg = WheelConfig(call_delta_target=0.25)
        lot = ShareLot(underlying="F", shares=100, cost_basis=95.0,
                       acquired_date="2026-01-05")
        pos = build_phase_b(make_chain(), 100.0, "F", "2026-01-05",
                            "2026-02-13", 39, lot, cfg)
        assert pos is not None
        assert abs(abs(pos.leg.entry_delta) - 0.25) < 0.05
        assert pos.leg.strike > 100.0
        assert pos.credit > 0

    def test_blocked_when_shares_far_underwater(self):
        cfg = WheelConfig(drop_from_basis_halt_pct=0.12)
        lot = ShareLot(underlying="F", shares=100, cost_basis=120.0,
                       acquired_date="2026-01-05")
        # spot=100 is >12% below cost_basis=120
        pos = build_phase_b(make_chain(), 100.0, "F", "2026-01-05",
                            "2026-02-13", 39, lot, cfg)
        assert pos is None

    def test_not_blocked_within_drawdown_tolerance(self):
        cfg = WheelConfig(drop_from_basis_halt_pct=0.12)
        lot = ShareLot(underlying="F", shares=100, cost_basis=105.0,
                       acquired_date="2026-01-05")
        # spot=100 is ~4.8% below cost_basis=105
        pos = build_phase_b(make_chain(), 100.0, "F", "2026-01-05",
                            "2026-02-13", 39, lot, cfg)
        assert pos is not None

    def test_blocked_by_ex_dividend(self):
        cfg = WheelConfig(call_delta_target=0.40, ex_div_skip_call_delta=0.35,
                          ex_div_skip_days=3)
        lot = ShareLot(underlying="F", shares=100, cost_basis=95.0,
                       acquired_date="2026-01-05")
        pos = build_phase_b(make_chain(), 100.0, "F", "2026-01-05",
                            "2026-02-13", 39, lot, cfg,
                            ex_div_date="2026-01-07")
        assert pos is None


class TestExDividendGuard:
    def test_true_when_close_and_deep(self):
        cfg = WheelConfig(ex_div_skip_days=3, ex_div_skip_call_delta=0.35)
        assert blocked_by_ex_dividend(0.40, "2026-01-05", "2026-01-07", cfg)

    def test_false_when_shallow_delta(self):
        cfg = WheelConfig(ex_div_skip_days=3, ex_div_skip_call_delta=0.35)
        assert not blocked_by_ex_dividend(0.20, "2026-01-05", "2026-01-07", cfg)

    def test_false_when_outside_window(self):
        cfg = WheelConfig(ex_div_skip_days=3, ex_div_skip_call_delta=0.35)
        assert not blocked_by_ex_dividend(0.40, "2026-01-05", "2026-01-20", cfg)

    def test_false_when_no_ex_div_date(self):
        cfg = WheelConfig(ex_div_skip_days=3, ex_div_skip_call_delta=0.35)
        assert not blocked_by_ex_dividend(0.40, "2026-01-05", None, cfg)


class TestCoveredCallSettlement:
    def _position(self, strike_delta_target=0.25, cost_basis=95.0):
        cfg = WheelConfig(call_delta_target=strike_delta_target)
        lot = ShareLot(underlying="F", shares=100, cost_basis=cost_basis,
                       acquired_date="2026-01-05")
        return build_phase_b(make_chain(), 100.0, "F", "2026-01-05",
                             "2026-02-13", 39, lot, cfg)

    def test_called_away_realizes_credit_plus_capital_gain(self):
        pos = self._position(cost_basis=95.0)
        outcome, pnl = resolve_covered_call_settlement(pos, pos.leg.strike + 1.0)
        assert outcome == "called_away"
        assert pnl == pytest.approx(
            pos.credit + (pos.leg.strike - 95.0), abs=1e-6)

    def test_expired_keeps_shares_credit_only(self):
        pos = self._position()
        outcome, pnl = resolve_covered_call_settlement(pos, pos.leg.strike - 1.0)
        assert outcome == "expired"
        assert pnl == pos.credit
