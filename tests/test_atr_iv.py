"""ATR / IV math and playbook classification. No network."""

import math

import pandas as pd
import pytest

from options_trader.signals.atr_iv import (
    atr_lookup_from_ohlc,
    classify_atr_trend,
    classify_bucket,
    classify_iv_context,
    realized_vol,
    snapshot_from_ohlc,
    true_range,
    true_range_series,
    wilder_atr,
)


class TestTrueRange:
    def test_gap_up_uses_high_minus_prev_close(self):
        # H=12, L=11, prev=10 → gap: max(1, 2, 1) = 2
        assert true_range(12.0, 11.0, 10.0) == 2.0

    def test_inside_day(self):
        assert true_range(10.5, 9.5, 10.0) == 1.0


class TestWilderAtr:
    def test_seed_is_sma_then_smooths(self):
        # Constant TR=1 after a NaN first bar.
        close = pd.Series([10.0] + [10.0] * 20)
        high = close + 0.5
        low = close - 0.5
        tr = true_range_series(high, low, close)
        atr = wilder_atr(tr, period=5)
        assert math.isnan(atr.iloc[0])
        # After seed, ATR of a flat 1.0 TR series stays 1.0
        assert atr.dropna().iloc[-1] == pytest.approx(1.0)


class TestRealizedVol:
    def test_zero_on_flat_prices(self):
        close = pd.Series([100.0] * 30)
        # std of zero log-returns is 0 → None
        assert realized_vol(close, period=20) is None

    def test_positive_on_trending_series(self):
        close = pd.Series([100.0 * (1.01 ** i) for i in range(40)])
        hv = realized_vol(close, period=20)
        assert hv is not None and hv > 0


class TestClassify:
    def test_rising_cheap_is_expansion(self):
        trend, _ = classify_atr_trend(2.0, 1.5)
        assert trend == "rising"
        ctx = classify_iv_context(atm_iv=0.12, hv=0.18, atr_vs_implied=1.3)
        assert ctx == "cheap"
        bucket, _ = classify_bucket("rising", "cheap")
        assert bucket == "expansion"

    def test_falling_rich_is_contraction(self):
        trend, _ = classify_atr_trend(1.2, 1.6)
        assert trend == "falling"
        ctx = classify_iv_context(atm_iv=0.28, hv=0.18, atr_vs_implied=0.7)
        assert ctx == "rich"
        bucket, _ = classify_bucket("falling", "rich")
        assert bucket == "contraction"

    def test_rising_rich_is_trending_volatile(self):
        bucket, _ = classify_bucket("rising", "rich")
        assert bucket == "trending_volatile"

    def test_flat_is_mixed(self):
        trend, chg = classify_atr_trend(1.01, 1.00)
        assert trend == "flat"
        assert chg == pytest.approx(0.01)
        bucket, _ = classify_bucket("flat", "fair")
        assert bucket == "mixed"


class TestSnapshot:
    def _ohlc(self, n=40, close0=100.0, daily_range=2.0):
        rows = []
        c = close0
        for _ in range(n):
            h, l = c + daily_range / 2, c - daily_range / 2
            rows.append({"High": h, "Low": l, "Close": c})
            c += 0.1
        return pd.DataFrame(rows)

    def test_2atr_band_and_inside_flag(self):
        ohlc = self._ohlc()
        snap = snapshot_from_ohlc(
            "TEST", ohlc, atm_iv=0.20, iv_dte=30, iv_expiry="2026-10-01",
            put_015_delta_strike=102.0,  # near spot → inside 2×ATR band
        )
        assert snap.spot == pytest.approx(ohlc["Close"].iloc[-1])
        assert snap.atr_14 > 0
        assert snap.band_2atr_down == pytest.approx(snap.spot - 2 * snap.atr_14)
        assert snap.put_015_inside_2atr is True
        assert any("inside" in n for n in snap.notes)

    def test_missing_iv_notes_unknown_context(self):
        snap = snapshot_from_ohlc("TEST", self._ohlc(), atm_iv=None)
        assert snap.iv_context == "unknown"
        assert snap.implied_daily_move is None

    def test_lookup_keys_are_iso_dates(self):
        ohlc = self._ohlc()
        ohlc.index = pd.bdate_range("2026-01-05", periods=len(ohlc))
        lookup = atr_lookup_from_ohlc("SPY", ohlc)
        assert lookup
        (sym, day), atr = next(iter(lookup.items()))
        assert sym == "SPY"
        assert len(day) == 10 and day[4] == "-"
        assert atr > 0
