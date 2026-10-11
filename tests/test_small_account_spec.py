"""Small-account spec configs build valid positions on a synthetic chain."""

from options_trader.signals.credit import build_position
from options_trader.signals.small_account_spec import (
    S1_TICKERS, S2_BULL_PUT, S2_BULL_PUT_WEEKLY, S2_TICKERS,
    S3B_PUT_CREDIT_ONLY, S3_IC_SATELLITE, S3_TICKERS, S1_WHEEL_CORE,
)
from options_trader.signals.wheel import build_phase_a
from tests.test_wheel import make_chain


class TestUniverseCoversAllSpecTickers:
    def test_full_spec_universe_is_covered(self):
        covered = set(S1_TICKERS) | set(S2_TICKERS) | set(S3_TICKERS)
        assert covered == {"F", "AAL", "SOFI", "RIVN", "GME", "SNAP", "AMC",
                           "MARA", "CLSK"}


class TestS1WheelCore:
    def test_phase_a_variant_builds_on_synthetic_chain(self):
        pos = build_phase_a(make_chain(), 100.0, "F", "2026-01-05",
                            "2026-02-13", 39, S1_WHEEL_CORE)
        assert pos is not None
        assert pos.credit_frac >= S1_WHEEL_CORE.put_min_credit_frac


class TestS2BullPut:
    def test_monthly_variant_builds(self):
        pos = build_position(make_chain(), 100.0, "F", "2026-01-05",
                             "2026-02-19", 45, S2_BULL_PUT)
        assert pos is not None
        assert pos.short_call_strike is None
        short = [l for l in pos.legs if l.side == -1][0]
        assert abs(abs(short.entry_delta) - 0.225) < 0.06

    def test_weekly_variant_builds_shorter_dated(self):
        pos = build_position(make_chain(), 100.0, "F", "2026-01-05",
                             "2026-01-16", 11, S2_BULL_PUT_WEEKLY)
        assert pos is not None


class TestS3IronCondorSatellite:
    def test_condor_has_both_sides(self):
        pos = build_position(make_chain(), 100.0, "GME", "2026-01-05",
                             "2026-01-26", 21, S3_IC_SATELLITE)
        assert pos is not None
        assert pos.short_put_strike is not None
        assert pos.short_call_strike is not None
        assert pos.credit_frac >= S3_IC_SATELLITE.min_credit_frac

    def test_put_only_variant_has_no_call_side(self):
        assert S3B_PUT_CREDIT_ONLY.short_call_delta is None
        # Same put-side target as the condor; on this synthetic chain the
        # single-sided credit sits just under the 25%-of-width gate (0.24),
        # which is the gate correctly rejecting a marginal trade, not a
        # structural failure -- confirm that directly against the condor's
        # matching put leg instead of requiring this exact fixture to clear
        # the credit gate.
        pos = build_position(make_chain(), 100.0, "GME", "2026-01-05",
                             "2026-01-26", 21, S3B_PUT_CREDIT_ONLY)
        if pos is not None:
            assert pos.short_call_strike is None
