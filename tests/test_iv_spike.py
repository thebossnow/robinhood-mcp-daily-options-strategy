"""Graded IV-spike response: the framework's "roll or close" split into
WATCH / DEFEND / CLOSE, keyed on delta as well as vol."""

import pytest

from options_trader.risk.iv_spike import (
    CLOSE, DEFEND, NONE, WATCH, IVSpikeConfig, assess,
)


class TestQuietMarket:
    def test_small_iv_move_is_nothing(self):
        s = assess(0.15, 0.18, 0.22)
        assert s.action == NONE and not s.needs_attention

    def test_falling_iv_is_nothing(self):
        assert assess(0.20, 0.12, 0.18).action == NONE


class TestWatch:
    def test_the_frameworks_15_to_40_example_with_a_safe_strike(self):
        """A 2.67x IV move alone does not close a position whose short
        strike the market still prices at 22 delta."""
        s = assess(0.15, 0.40, 0.22)
        assert s.action == WATCH
        assert s.iv_ratio == pytest.approx(0.40 / 0.15)
        assert "peak IV" in s.reason

    def test_watch_threshold_boundary(self):
        # Just above and just below 1.5x. The exact boundary is not tested
        # because 0.30/0.20 evaluates to 1.4999999999999998 in binary
        # floating point — a threshold that sits on a ratio of decimals has
        # no exact case, and nothing here should depend on which side of
        # the last bit a comparison lands.
        assert assess(0.20, 0.31, 0.20).action == WATCH
        assert assess(0.20, 0.29, 0.20).action == NONE

    def test_missing_delta_can_watch_but_never_defend(self):
        s = assess(0.15, 0.45, None)
        assert s.action == WATCH and s.short_delta is None


class TestDefend:
    def test_spike_plus_threatened_strike(self):
        s = assess(0.15, 0.40, 0.45)
        assert s.action == DEFEND and s.needs_attention
        assert "roll out/down" in s.reason

    def test_threatened_strike_without_a_spike_is_not_a_defend(self):
        # Delta drifted up on a slow grind, not a vol event.
        s = assess(0.15, 0.18, 0.45)
        assert s.action == NONE

    def test_defend_requires_both_thresholds(self):
        assert assess(0.15, 0.40, 0.35).action == WATCH      # delta too low
        assert assess(0.15, 0.25, 0.45).action == WATCH      # ratio too low


class TestClose:
    def test_delta_through_breach_closes_regardless_of_vol(self):
        s = assess(0.15, 0.15, 0.55)
        assert s.action == CLOSE
        assert "the strike, not the vol" in s.reason

    def test_breach_outranks_everything(self):
        assert assess(0.15, 0.90, 0.60).action == CLOSE

    def test_roll_budget_exhaustion_converts_defend_to_close(self):
        assert assess(0.15, 0.40, 0.45, rolls_used=1).action == DEFEND
        s = assess(0.15, 0.40, 0.45, rolls_used=2)
        assert s.action == CLOSE and "defers loss without reducing it" in s.reason

    def test_roll_cap_is_configurable(self):
        cfg = IVSpikeConfig(max_rolls=4)
        assert assess(0.15, 0.40, 0.45, cfg, rolls_used=3).action == DEFEND


class TestDegenerateInputs:
    def test_zero_entry_iv_cannot_be_assessed(self):
        s = assess(0.0, 0.40, 0.22)
        assert s.action == NONE and "no entry IV" in s.reason

    def test_zero_entry_iv_still_honours_a_breach(self):
        assert assess(0.0, 0.40, 0.60).action == CLOSE

    def test_negative_delta_is_read_as_magnitude(self):
        assert assess(0.15, 0.40, -0.45).action == DEFEND


class TestDescription:
    def test_describe_carries_action_ratio_and_delta(self):
        text = assess(0.15, 0.40, 0.45).describe()
        assert text.startswith("DEFEND")
        assert "IV x2.67" in text and "short delta 0.45" in text
