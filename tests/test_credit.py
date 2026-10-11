"""Credit-structure builders: strike selection, credit math, gates."""

import pandas as pd
import pytest

from options_trader.signals.credit import (
    VALIDATED, VARIANTS, CreditVariantConfig, atr_short_bound,
    bs_delta, build_position,
)


def make_chain(spot: float = 100.0) -> pd.DataFrame:
    """Synthetic single-expiration chain around spot=100 with plausible
    deltas and tight quotes. Strikes every 1.0 from 80 to 120."""
    rows = []
    for strike in range(80, 121):
        k = float(strike)
        # crude but monotonic delta ladder around ATM
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


def build(cfg, chain=None, spot=100.0, atr=None):
    return build_position(chain if chain is not None else make_chain(spot),
                          spot, "TEST", "2026-01-05", "2026-02-13", 39, cfg,
                          atr=atr)


class TestPutSpread:
    def test_selects_short_near_target_delta(self):
        pos = build(VARIANTS["put_spread"])
        assert pos is not None
        short = [l for l in pos.legs if l.side == -1][0]
        assert short.type == "put"
        assert abs(abs(short.entry_delta) - 0.30) < 0.05

    def test_wing_is_below_short_and_near_width_target(self):
        pos = build(VARIANTS["put_spread"])
        short = [l for l in pos.legs if l.side == -1][0]
        wing = [l for l in pos.legs if l.side == +1][0]
        assert wing.strike < short.strike
        # 2% of spot=100 -> target width 2.0, strikes every 1.0
        assert abs((short.strike - wing.strike) - 2.0) <= 1.0

    def test_credit_positive_and_below_width(self):
        pos = build(VARIANTS["put_spread"])
        width = max(pos.widths().values())
        assert 0 < pos.credit < width
        assert pos.max_loss == pytest.approx(width - pos.credit, abs=1e-6)

    def test_slippage_reduces_credit(self):
        pos = build(VARIANTS["put_spread"])
        assert pos.credit < pos.credit_mid


class TestCondors:
    def test_symmetric_has_four_legs_two_sides(self):
        pos = build(VARIANTS["condor_sym"])
        assert pos is not None
        assert len(pos.legs) == 4
        assert pos.short_put_strike is not None
        assert pos.short_call_strike is not None
        assert pos.short_put_strike < 100.0 < pos.short_call_strike

    def test_asymmetric_call_side_is_further_out(self):
        sym = build(VARIANTS["condor_sym"])
        asym = build(VARIANTS["condor_asym"])
        assert asym.short_call_strike > sym.short_call_strike
        assert asym.short_put_strike == sym.short_put_strike

    def test_condor_credit_exceeds_put_side_alone(self):
        put_only = CreditVariantConfig(
            name="p", short_put_delta=0.20, short_call_delta=None,
            min_credit_frac=0.0)
        condor = CreditVariantConfig(
            name="c", short_put_delta=0.20, short_call_delta=0.20,
            min_credit_frac=0.0)
        assert build(condor).credit_mid > build(put_only).credit_mid

    def test_intrinsic_close_cost(self):
        pos = build(VARIANTS["condor_sym"])
        # settle inside the range: everything expires worthless
        assert pos.intrinsic_close_cost(100.0) == 0.0
        # settle far below the put wing: put side worth full width
        put_width = pos.widths()["put"]
        assert pos.intrinsic_close_cost(1.0) == pytest.approx(put_width)


class TestGates:
    def test_min_credit_frac_rejects(self):
        strict = CreditVariantConfig(name="s", short_put_delta=0.30,
                                     min_credit_frac=0.99)
        assert build(strict) is None

    def test_min_short_bid_rejects_dead_quotes(self):
        chain = make_chain()
        chain.loc[chain["type"] == "put", "bid"] = 0.0
        cfg = CreditVariantConfig(name="s", short_put_delta=0.30,
                                  min_credit_frac=0.0)
        assert build(cfg, chain=chain) is None

    def test_empty_side_returns_none(self):
        chain = make_chain()
        calls_only = chain[chain["type"] == "call"]
        assert build(VARIANTS["put_spread"], chain=calls_only) is None


class TestBsDeltaFallback:
    def test_bs_delta_signs_and_bounds(self):
        assert 0.4 < bs_delta("call", 100, 100, 0.25, 0.1) < 0.6
        assert -0.6 < bs_delta("put", 100, 100, 0.25, 0.1) < -0.4
        assert bs_delta("call", 100, 150, 0.25, 0.05) < 0.05

    def test_fallback_used_when_no_delta_column(self):
        chain = make_chain().drop(columns=["delta"])
        pos = build(VARIANTS["put_spread"], chain=chain)
        assert pos is not None
        short = [l for l in pos.legs if l.side == -1][0]
        assert short.strike < 100.0   # still an OTM put


class TestAtrDeltaWeekly:
    def test_bound_put_is_spot_minus_mult_atr(self):
        assert atr_short_bound(100.0, 5.0, 2.0, "put") == 90.0
        assert atr_short_bound(100.0, 5.0, 2.0, "call") == 110.0

    def test_delta_inside_band_is_pushed_otm(self):
        # 30Δ put on the synthetic chain is strike 95. 2×ATR=10 → bound 90.
        cfg = CreditVariantConfig(
            name="atr", short_put_delta=0.30, short_call_delta=None,
            min_credit_frac=0.0, atr_mult=2.0,
        )
        pos = build(cfg, atr=5.0)
        assert pos is not None
        short = [l for l in pos.legs if l.side == -1][0]
        wing = [l for l in pos.legs if l.side == +1][0]
        assert short.strike == 90.0
        assert wing.strike < short.strike

    def test_delta_already_outside_band_is_kept(self):
        # Same 30Δ/95 put, ATR=1 → bound 98. 95 is already further OTM.
        cfg = CreditVariantConfig(
            name="atr", short_put_delta=0.30, short_call_delta=None,
            min_credit_frac=0.0, atr_mult=2.0,
        )
        pos = build(cfg, atr=1.0)
        assert pos is not None
        short = [l for l in pos.legs if l.side == -1][0]
        assert abs(short.strike - 95.0) <= 1.0

    def test_missing_atr_refuses_when_mult_set(self):
        cfg = CreditVariantConfig(
            name="atr", short_put_delta=0.30, atr_mult=2.0,
            min_credit_frac=0.0,
        )
        assert build(cfg, atr=None) is None
        assert build(cfg, atr=0.0) is None

    def test_no_listed_strike_beyond_bound_returns_none(self):
        cfg = CreditVariantConfig(
            name="atr", short_put_delta=0.30, atr_mult=2.0,
            min_credit_frac=0.0,
        )
        # bound = 100 - 2*40 = 20, chain only goes to 80
        assert build(cfg, atr=40.0) is None

    def test_weekly_variant_is_configured(self):
        v = VALIDATED["spy_weekly_atr15"]
        assert v.short_put_delta == 0.15
        assert v.short_call_delta is None
        assert v.atr_mult == 2.0
        assert (v.min_dte, v.max_dte, v.target_dte) == (5, 14, 7)
        assert v.exit_on_breach is False
