"""Pre-trade volatility-regime gate: level ceiling, spike detection, and
the fail-closed behavior on missing data."""

from unittest import mock

import pandas as pd
import pytest

from options_trader.risk.vol_regime import (
    check_vol_regime, fetch_vix_closes,
)


def _closes(*values: float) -> pd.Series:
    return pd.Series(list(values))


class TestLevelCeiling:
    def test_calm_vix_allows(self):
        closes = _closes(15.0, 15.5, 16.0, 15.8, 16.2, 15.9)
        check = check_vol_regime(30.0, 0.30, 5, vix_closes=closes)
        assert check.allowed
        assert check.vix_level == 15.9

    def test_vix_at_or_above_ceiling_refuses(self):
        closes = _closes(16.0, 17.0, 18.0, 20.0, 25.0, 30.0)
        check = check_vol_regime(30.0, 0.30, 5, vix_closes=closes)
        assert not check.allowed
        assert "level" in check.reason
        assert "30.00" in check.reason

    def test_just_below_ceiling_allows(self):
        # Flat at 29.x so the level check is isolated from the spike check.
        closes = _closes(29.9, 29.9, 29.9, 29.9, 29.9, 29.99)
        check = check_vol_regime(30.0, 0.30, 5, vix_closes=closes)
        assert check.allowed


class TestSpikeDetection:
    def test_slow_rise_within_limit_allows(self):
        # +20% over 5 days, limit is 30%
        closes = _closes(20.0, 21.0, 22.0, 23.0, 23.5, 24.0)
        check = check_vol_regime(30.0, 0.30, 5, vix_closes=closes)
        assert check.allowed

    def test_sharp_spike_refuses_before_hitting_level_ceiling(self):
        # 15 -> 21 over 5 days = +40%, still well under the level ceiling of 30
        closes = _closes(15.0, 16.0, 17.0, 18.0, 19.0, 21.0)
        check = check_vol_regime(30.0, 0.30, 5, vix_closes=closes)
        assert not check.allowed
        assert "spiked" in check.reason
        assert check.vix_pct_change == pytest.approx(0.40, abs=1e-9)

    def test_exact_spike_threshold_refuses(self):
        closes = _closes(10.0, 10.0, 10.0, 10.0, 10.0, 13.0)   # exactly +30%
        check = check_vol_regime(30.0, 0.30, 5, vix_closes=closes)
        assert not check.allowed


class TestFailClosed:
    def test_none_closes_refuses(self):
        with mock.patch("options_trader.risk.vol_regime.fetch_vix_closes",
                        return_value=None):
            check = check_vol_regime(30.0, 0.30, 5)
        assert not check.allowed
        assert "unavailable" in check.reason

    def test_insufficient_history_refuses(self):
        check = check_vol_regime(30.0, 0.30, 5, vix_closes=_closes(20.0))
        assert not check.allowed
        assert "unavailable" in check.reason

    def test_empty_series_refuses(self):
        check = check_vol_regime(30.0, 0.30, 5, vix_closes=_closes())
        assert not check.allowed


class TestFetchVixCloses:
    def test_wraps_yfinance_failure_as_none(self):
        with mock.patch("yfinance.Ticker", side_effect=RuntimeError("network down")):
            assert fetch_vix_closes(5) is None

    def test_returns_close_series_on_success(self):
        fake_hist = pd.DataFrame({"Close": [14.0, 15.0, 16.0]})
        fake_ticker = mock.Mock()
        fake_ticker.history.return_value = fake_hist
        with mock.patch("yfinance.Ticker", return_value=fake_ticker):
            closes = fetch_vix_closes(5)
        assert list(closes) == [14.0, 15.0, 16.0]
