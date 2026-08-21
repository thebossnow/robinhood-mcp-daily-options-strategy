"""The two profiles: that as_specified really is verbatim, that
evidence_adjusted really does differ where the repo has measurements, and
that the framework's own numbers behave as the docs claim on a chain."""

import pandas as pd
import pytest

from options_trader.signals.credit import build_position
from options_trader.signals.income import (
    AS_SPECIFIED, DEFAULT_PROFILE, EVIDENCE_ADJUSTED, PROFILES,
    fit_wing_to_grid, get_profile, infer_strike_grid, with_underlying_scale,
)
from options_trader.signals.iv_rank import build_iv_history, read_iv_rank

SPOT = 762.60


def _spy_like_chain():
    """A put/call skew shaped like an index chain 39 DTE out: credit as a
    fraction of a 2%-of-spot wing lands in the 0.10-0.25 band the README
    reports measuring on real SPY chains, nowhere near one third."""
    rows = []
    puts = [(740.0, 7.90, 8.10, 0.17, -0.30), (733.0, 6.80, 7.00, 0.175, -0.25),
            (725.0, 5.90, 6.10, 0.18, -0.22), (718.0, 5.00, 5.20, 0.185, -0.20),
            (710.0, 4.40, 4.60, 0.19, -0.15), (702.0, 3.70, 3.90, 0.195, -0.12),
            (695.0, 3.15, 3.35, 0.20, -0.10), (680.0, 2.30, 2.50, 0.21, -0.07),
            (665.0, 1.60, 1.80, 0.22, -0.05), (650.0, 1.10, 1.30, 0.23, -0.04)]
    for strike, bid, ask, iv, delta in puts:
        rows.append({"type": "put", "strike": strike, "bid": bid, "ask": ask,
                     "iv": iv, "delta": delta, "open_interest": 5000})
    calls = [(775.0, 9.90, 10.10, 0.13, 0.40), (785.0, 6.90, 7.10, 0.128, 0.30),
             (792.0, 5.20, 5.40, 0.126, 0.22), (800.0, 3.90, 4.10, 0.124, 0.20),
             (812.0, 2.60, 2.80, 0.122, 0.15), (825.0, 1.70, 1.90, 0.12, 0.10),
             (840.0, 1.00, 1.20, 0.119, 0.07)]
    for strike, bid, ask, iv, delta in calls:
        rows.append({"type": "call", "strike": strike, "bid": bid, "ask": ask,
                     "iv": iv, "delta": delta, "open_interest": 5000})
    return pd.DataFrame(rows)


def _build(vcfg, chain=None, dte=39):
    return build_position(chain if chain is not None else _spy_like_chain(),
                          SPOT, "SPY", "2026-08-24", "2026-10-02", dte, vcfg)


class TestProfilesRegistered:
    def test_both_profiles_available(self):
        assert set(PROFILES) == {"as_specified", "evidence_adjusted"}

    def test_default_is_the_adjusted_one(self):
        assert DEFAULT_PROFILE == "evidence_adjusted"

    def test_unknown_profile_raises_with_the_options(self):
        with pytest.raises(KeyError, match="evidence_adjusted"):
            get_profile("nope")


class TestAsSpecifiedIsVerbatim:
    def test_short_deltas_are_in_the_frameworks_20_to_30_band(self):
        for v in AS_SPECIFIED.variants.values():
            for d in (v.short_put_delta, v.short_call_delta):
                if d is not None:
                    assert 0.20 <= d <= 0.30, v.name

    def test_monthlies_use_the_30_to_45_dte_window(self):
        v = AS_SPECIFIED.variants["spec_put_spread_25d"]
        assert (v.min_dte, v.max_dte) == (30, 45)

    def test_the_condor_demands_one_third_of_width(self):
        assert AS_SPECIFIED.variants["spec_condor_20d"].min_credit_frac == 0.33

    def test_every_variant_takes_profit_at_fifty_percent(self):
        for p in PROFILES.values():
            for v in p.variants.values():
                assert v.profit_take_frac == 0.50, v.name

    def test_iv_rank_preference_is_thirty_percent(self):
        assert AS_SPECIFIED.min_iv_rank == 0.30

    def test_cash_secured_puts_are_enabled(self):
        assert AS_SPECIFIED.allow_cash_secured_puts
        assert AS_SPECIFIED.csp is not None

    def test_no_book_level_cap(self):
        assert AS_SPECIFIED.portfolio_heat_cap_pct is None


class TestOneThirdOfWidthIsNotAvailable:
    """The single most consequential claim in the docs, exercised."""

    def test_the_spec_condor_finds_no_trade(self):
        assert _build(AS_SPECIFIED.variants["spec_condor_20d"]) is None

    def test_the_same_condor_trades_once_the_floor_is_realistic(self):
        from dataclasses import replace
        relaxed = replace(AS_SPECIFIED.variants["spec_condor_20d"],
                          min_credit_frac=0.10)
        pos = _build(relaxed)
        assert pos is not None

    def test_measured_credit_fraction_sits_far_below_one_third(self):
        from dataclasses import replace
        relaxed = replace(AS_SPECIFIED.variants["spec_condor_20d"],
                          min_credit_frac=0.0)
        pos = _build(relaxed)
        assert 0.05 < pos.credit_frac < 0.33


class TestEvidenceAdjustedDiffers:
    def test_short_deltas_dropped_to_the_validated_band(self):
        for v in EVIDENCE_ADJUSTED.variants.values():
            for d in (v.short_put_delta, v.short_call_delta):
                if d is not None:
                    assert d <= 0.15, v.name

    def test_iv_rank_no_longer_gates(self):
        assert EVIDENCE_ADJUSTED.min_iv_rank is None

    def test_a_book_level_cap_exists(self):
        assert EVIDENCE_ADJUSTED.portfolio_heat_cap_pct == 0.20

    def test_index_cash_secured_puts_are_off_by_default(self):
        assert not EVIDENCE_ADJUSTED.allow_cash_secured_puts
        # ...but the structure is still configured, ready for an underlying
        # whose collateral actually fits.
        assert EVIDENCE_ADJUSTED.csp is not None

    def test_the_weekly_moved_off_the_gamma_cliff(self):
        spec = AS_SPECIFIED.variants["spec_weekly_put_25d"]
        adj = EVIDENCE_ADJUSTED.variants["income_weekly_put10"]
        assert spec.target_dte < adj.min_dte

    def test_neither_profile_uses_a_breach_stop(self):
        for p in PROFILES.values():
            for v in p.variants.values():
                assert not v.exit_on_breach, v.name

    def test_adjusted_variants_actually_trade_on_the_same_chain(self):
        pos = _build(EVIDENCE_ADJUSTED.variants["income_condor15"])
        assert pos is not None and pos.credit > 0


class TestEntryCadence:
    @pytest.mark.parametrize("weekday,ok", [
        (0, True), (1, True), (2, False), (3, False), (4, False)])
    def test_monday_and_tuesday_only(self, weekday, ok):
        for p in PROFILES.values():
            assert p.entry_day_ok(weekday)[0] is ok

    def test_rejection_names_the_day_and_the_allowed_days(self):
        allowed, msg = EVIDENCE_ADJUSTED.entry_day_ok(4)
        assert not allowed
        assert "Friday" in msg and "Monday/Tuesday" in msg


class TestUnderlyingRescale:
    def test_rescaling_touches_every_variant(self):
        scaled = with_underlying_scale(EVIDENCE_ADJUSTED, 0.10)
        assert all(v.wing_width_frac == 0.10 for v in scaled.variants.values())

    def test_the_original_profile_is_untouched(self):
        with_underlying_scale(EVIDENCE_ADJUSTED, 0.10)
        assert EVIDENCE_ADJUSTED.variants["income_condor15"].wing_width_frac == 0.04

    def test_deltas_and_dte_survive_the_rescale(self):
        scaled = with_underlying_scale(EVIDENCE_ADJUSTED, 0.10)
        orig = EVIDENCE_ADJUSTED.variants["income_put10"]
        now = scaled.variants["income_put10"]
        assert (now.short_put_delta, now.min_dte) == (orig.short_put_delta,
                                                      orig.min_dte)


class TestStrikeGridGeometry:
    """Wings are a fraction of spot; strike grids are absolute. The
    geometry does not port down to a low-priced underlying, and these
    pin how the code says so rather than discovering it in a backtest."""

    def test_a_4pct_wing_spans_30_strikes_on_spy(self):
        from options_trader.signals.income import wing_increments
        assert wing_increments(762.60, 1.00, 0.04) == pytest.approx(30.5, abs=0.1)

    def test_the_same_4pct_wing_is_one_strike_on_a_14_dollar_name(self):
        from options_trader.signals.income import wing_increments
        assert wing_increments(13.99, 0.50, 0.04) < 1.2

    def test_the_narrowest_usable_wing_is_tiny_on_spy(self):
        from options_trader.signals.income import min_wing_frac
        assert min_wing_frac(762.60, 1.00) < 0.005

    def test_the_narrowest_usable_wing_is_huge_on_a_cheap_name(self):
        from options_trader.signals.income import min_wing_frac
        # 3 x $0.50 on a $13.99 stock is already >10% of spot — a
        # structurally different trade from anything the sweep validated.
        assert min_wing_frac(13.99, 0.50) > 0.10

    def test_rescaling_widens_every_variant_that_cannot_be_placed(self):
        from options_trader.signals.income import (min_wing_frac,
                                                   scaled_for_underlying)
        scaled = scaled_for_underlying(EVIDENCE_ADJUSTED, 13.99, 0.50)
        floor = min_wing_frac(13.99, 0.50)
        for v in scaled.variants.values():
            assert v.wing_width_frac == pytest.approx(floor)

    def test_rescaling_never_narrows_a_wing(self):
        from options_trader.signals.income import scaled_for_underlying
        scaled = scaled_for_underlying(EVIDENCE_ADJUSTED, 762.60, 1.00)
        for name, v in scaled.variants.items():
            assert v.wing_width_frac == \
                EVIDENCE_ADJUSTED.variants[name].wing_width_frac

    def test_an_untouched_profile_gains_no_warning(self):
        from options_trader.signals.income import scaled_for_underlying
        scaled = scaled_for_underlying(EVIDENCE_ADJUSTED, 762.60, 1.00)
        assert len(scaled.notes) == len(EVIDENCE_ADJUSTED.notes)

    def test_a_rescaled_profile_says_it_is_not_the_validated_geometry(self):
        from options_trader.signals.income import scaled_for_underlying
        scaled = scaled_for_underlying(EVIDENCE_ADJUSTED, 13.99, 0.50)
        note = scaled.notes[-1]
        assert "NOT the validated geometry" in note
        assert "not comparable to the SPY sweep" in note.replace("\n", " ")

    def test_the_original_profile_is_untouched(self):
        from options_trader.signals.income import scaled_for_underlying
        scaled_for_underlying(EVIDENCE_ADJUSTED, 13.99, 0.50)
        assert EVIDENCE_ADJUSTED.variants["income_condor15"].wing_width_frac == 0.04
        assert len(EVIDENCE_ADJUSTED.notes) == 3

    def test_degenerate_inputs_are_rejected(self):
        from options_trader.signals.income import min_wing_frac, wing_increments
        with pytest.raises(ValueError):
            min_wing_frac(0.0, 0.50)
        with pytest.raises(ValueError):
            min_wing_frac(13.99, 0.0)
        with pytest.raises(ValueError):
            wing_increments(13.99, 0.0, 0.04)


class TestVerbatimProfileGatesOnRankNotPercentile:
    """The framework says "IV rank above 30". AS_SPECIFIED exists to be
    that sentence, so it must gate on IV RANK.

    `IncomeProfile.iv_rank_use_percentile` defaults to True because
    percentile is the better statistic — but inheriting that default here
    made the verbatim profile silently gate on something the framework
    never named, in the direction that passes MORE trades.
    """

    def test_as_specified_gates_on_rank(self):
        assert AS_SPECIFIED.iv_rank_use_percentile is False
        assert AS_SPECIFIED.min_iv_rank == 0.30

    def test_adjusted_profile_states_percentile_rather_than_defaulting(self):
        assert EVIDENCE_ADJUSTED.iv_rank_use_percentile is True

    def test_the_two_statistics_actually_disagree_at_this_threshold(self):
        """A year of calm after one spike: rank is crushed, percentile is
        not. The verbatim profile must refuse here; it previously passed."""
        hist = build_iv_history(
            {f"2026-01-{d:02d}": 0.12 for d in range(1, 29)}
            | {"2025-12-01": 0.80}          # the spike that sets the high
        )
        reading = read_iv_rank(0.14, hist)
        assert reading.iv_rank < 0.30          # literal framework reading
        assert reading.iv_percentile > 0.30    # what the default would use

        assert not reading.meets(AS_SPECIFIED.min_iv_rank,
                                 AS_SPECIFIED.iv_rank_use_percentile)
        assert reading.meets(AS_SPECIFIED.min_iv_rank, True)


class TestGridInference:
    """`infer_strike_grid` measures the grid on the rows in hand, because
    TYPICAL_STRIKE_GRID is a table of guesses and the placeable spacing is
    not always the nominal one."""

    @staticmethod
    def _chain(strikes):
        return pd.DataFrame({"strike": strikes,
                             "type": ["put"] * len(strikes)})

    def test_measures_a_dollar_grid_on_an_index(self):
        grid = infer_strike_grid(self._chain([760.0, 761.0, 762.0, 763.0]),
                                 762.0)
        assert grid == pytest.approx(1.00)

    def test_measures_a_half_dollar_grid_on_a_cheap_name(self):
        grid = infer_strike_grid(self._chain([13.0, 13.5, 14.0, 14.5, 15.0]),
                                 14.0)
        assert grid == pytest.approx(0.50)

    def test_only_strikes_near_the_money_count(self):
        # Wide far-OTM spacing must not drag the near-money answer wider.
        grid = infer_strike_grid(
            self._chain([100.0, 300.0, 740.0, 741.0, 742.0, 743.0]), 742.0)
        assert grid == pytest.approx(1.00)

    def test_ties_break_toward_the_wider_spacing(self):
        # One 1.0 gap and one 5.0 gap: assuming the tighter grid would
        # understate how coarse the placeable geometry is.
        grid = infer_strike_grid(self._chain([700.0, 701.0, 706.0]), 703.0)
        assert grid == pytest.approx(5.00)

    def test_a_single_near_money_strike_has_no_grid(self):
        assert infer_strike_grid(self._chain([14.0, 900.0]), 14.0) is None

    def test_an_empty_chain_has_no_grid(self):
        assert infer_strike_grid(pd.DataFrame({"strike": []}), 14.0) is None

    def test_degenerate_spot_is_rejected(self):
        with pytest.raises(ValueError):
            infer_strike_grid(self._chain([1.0, 2.0]), 0.0)


class TestFitWingToGrid:
    """One implementation of the widen-only rule, used by both
    `scaled_for_underlying` and the scanner."""

    def test_a_placeable_wing_is_returned_unchanged_with_no_note(self):
        v = EVIDENCE_ADJUSTED.variants["income_condor15"]
        fitted, note = fit_wing_to_grid(v, 762.60, 1.00)
        assert fitted is v
        assert note is None

    def test_an_unplaceable_wing_is_widened_and_annotated(self):
        v = EVIDENCE_ADJUSTED.variants["income_condor15"]
        fitted, note = fit_wing_to_grid(v, 14.40, 0.50)
        assert fitted.wing_width_frac > v.wing_width_frac
        assert fitted.wing_width_frac == pytest.approx(3 * 0.50 / 14.40)
        assert "NOT the validated geometry" in note
        assert "WING RESCALED" in note

    def test_the_input_config_is_never_mutated(self):
        v = EVIDENCE_ADJUSTED.variants["income_condor15"]
        fit_wing_to_grid(v, 14.40, 0.50)
        assert v.wing_width_frac == pytest.approx(0.04)

    def test_everything_except_the_wing_survives(self):
        v = EVIDENCE_ADJUSTED.variants["income_condor15"]
        fitted, _ = fit_wing_to_grid(v, 14.40, 0.50)
        assert fitted.short_put_delta == v.short_put_delta
        assert fitted.min_dte == v.min_dte
        assert fitted.min_credit_frac == v.min_credit_frac
