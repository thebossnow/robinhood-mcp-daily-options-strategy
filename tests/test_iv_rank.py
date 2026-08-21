"""IV rank vs IV percentile, the fail-open preference, and ATM extraction."""

import pandas as pd
import pytest

from options_trader.signals.iv_rank import (
    atm_iv, build_iv_history, iv_percentile, iv_rank, read_iv_rank,
)


def _hist(*values):
    return pd.Series(list(values), dtype=float)


class TestIVRank:
    def test_midpoint_of_the_range(self):
        assert iv_rank(0.20, _hist(0.10, 0.30)) == pytest.approx(0.5)

    def test_at_the_low_is_zero_and_at_the_high_is_one(self):
        h = _hist(0.10, 0.20, 0.30)
        assert iv_rank(0.10, h) == 0.0
        assert iv_rank(0.30, h) == 1.0

    def test_clamped_outside_the_historical_range(self):
        h = _hist(0.10, 0.30)
        assert iv_rank(0.50, h) == 1.0
        assert iv_rank(0.05, h) == 0.0

    def test_flat_history_has_no_rank(self):
        assert iv_rank(0.20, _hist(0.20, 0.20, 0.20)) is None

    def test_empty_history_has_no_rank(self):
        assert iv_rank(0.20, _hist()) is None


class TestIVPercentile:
    def test_fraction_of_days_below_current(self):
        h = _hist(0.10, 0.12, 0.14, 0.30)
        assert iv_percentile(0.15, h) == pytest.approx(0.75)

    def test_flat_history_still_yields_a_percentile(self):
        # Unlike rank, percentile is well defined on a flat series.
        assert iv_percentile(0.25, _hist(0.20, 0.20)) == 1.0

    def test_empty_history_has_no_percentile(self):
        assert iv_percentile(0.2, _hist()) is None


class TestRankVersusPercentileDisagree:
    """One spike suppresses rank for a year while percentile stays honest —
    the reason the pipeline prefers percentile."""

    def test_single_spike_crushes_rank_but_not_percentile(self):
        calm = [0.12] * 200 + [0.13] * 51
        spike = calm + [0.80]
        reading = read_iv_rank(0.20, pd.Series(spike, dtype=float))
        assert reading.iv_rank < 0.15         # 0.20 looks "low" vs an 0.80 max
        assert reading.iv_percentile > 0.95   # but it is above nearly every day


class TestPreferenceGate:
    def test_no_threshold_always_passes(self):
        r = read_iv_rank(0.10, _hist(0.10, 0.50))
        assert r.meets(None)

    def test_threshold_uses_percentile_by_default(self):
        r = read_iv_rank(0.20, _hist(0.10, 0.12, 0.14, 0.80))
        assert r.iv_percentile == pytest.approx(0.75)
        assert r.meets(0.30)
        assert not r.meets(0.30, use_percentile=False)   # rank is ~0.14

    def test_fails_open_on_unusable_history(self):
        # A missing IV history loses a preference, never a trade.
        r = read_iv_rank(0.20, _hist())
        assert r.meets(0.90)

    def test_below_threshold_is_refused(self):
        r = read_iv_rank(0.11, _hist(0.10, 0.20, 0.30, 0.40))
        assert not r.meets(0.50)


class TestLookbackHonesty:
    def test_reports_the_lookback_it_actually_used(self):
        r = read_iv_rank(0.20, _hist(*[0.1] * 8), lookback_days=252)
        assert r.lookback_days == 8
        assert "over 8d" in r.describe()

    def test_truncates_to_the_requested_window(self):
        long = _hist(*([0.90] * 300 + [0.10] * 20))
        r = read_iv_rank(0.20, long, lookback_days=10)
        assert r.lookback_days == 10
        assert r.high == pytest.approx(0.10)   # the 0.90 era is out of window


class TestATMIV:
    def _chain(self):
        return pd.DataFrame([
            {"type": "put", "strike": 760.0, "bid": 1, "ask": 1, "iv": 0.18},
            {"type": "put", "strike": 700.0, "bid": 1, "ask": 1, "iv": 0.26},
            {"type": "call", "strike": 765.0, "bid": 1, "ask": 1, "iv": 0.14},
            {"type": "call", "strike": 820.0, "bid": 1, "ask": 1, "iv": 0.12},
        ])

    def test_averages_the_nearest_put_and_call(self):
        assert atm_iv(self._chain(), 762.6) == pytest.approx(0.16)

    def test_skips_zero_iv_rows(self):
        chain = self._chain()
        chain.loc[0, "iv"] = 0.0
        # Nearest usable put is now the 700 strike.
        assert atm_iv(chain, 762.6) == pytest.approx((0.26 + 0.14) / 2)

    def test_falls_back_to_one_side(self):
        puts_only = self._chain().query("type == 'put'")
        assert atm_iv(puts_only, 762.6) == pytest.approx(0.18)

    def test_empty_or_iv_less_chain_returns_none(self):
        assert atm_iv(pd.DataFrame(), 762.6) is None
        assert atm_iv(pd.DataFrame([{"type": "put", "strike": 1.0}]), 1.0) is None


class TestBuildHistory:
    def test_sorts_by_date(self):
        s = build_iv_history({"2026-08-20": 0.2, "2026-08-18": 0.1})
        assert list(s.values) == [0.1, 0.2]

    def test_empty_input_is_an_empty_series(self):
        assert build_iv_history({}).empty
