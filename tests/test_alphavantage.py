"""Tests for the Alpha Vantage settlement-price fallback. All HTTP mocked —
see options_trader/data/alphavantage.py for why this exists (yfinance
substitute for get_settlement_close only, free tier has no options chains)."""

from unittest import mock

import pytest

from options_trader.data import get_settlement_provider
from options_trader.data.alphavantage import AlphaVantageProvider


def _resp(json_body, status_code=200):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = json_body
    r.raise_for_status = mock.Mock()
    if status_code >= 400:
        import requests
        r.raise_for_status.side_effect = requests.HTTPError(str(status_code))
    return r


DAILY_BODY = {
    "Meta Data": {"2. Symbol": "SPY"},
    "Time Series (Daily)": {
        "2026-08-10": {"1. open": "772.60", "4. close": "773.03"},
        "2026-08-07": {"1. open": "771.02", "4. close": "773.26"},
    },
}


class TestAlphaVantageProvider:
    def test_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="API key"):
            AlphaVantageProvider()

    def test_get_settlement_close_known_date(self):
        provider = AlphaVantageProvider(api_key="demo", pause_s=0.0)
        with mock.patch("options_trader.data.alphavantage.requests.get",
                        return_value=_resp(DAILY_BODY)) as get:
            px = provider.get_settlement_close("SPY", "2026-08-10")
        assert px == pytest.approx(773.03)
        assert get.call_count == 1

    def test_unknown_date_returns_none(self):
        provider = AlphaVantageProvider(api_key="demo", pause_s=0.0)
        with mock.patch("options_trader.data.alphavantage.requests.get",
                        return_value=_resp(DAILY_BODY)):
            assert provider.get_settlement_close("SPY", "2019-01-01") is None

    def test_daily_series_cached_per_underlying(self):
        provider = AlphaVantageProvider(api_key="demo", pause_s=0.0)
        with mock.patch("options_trader.data.alphavantage.requests.get",
                        return_value=_resp(DAILY_BODY)) as get:
            provider.get_settlement_close("SPY", "2026-08-10")
            provider.get_settlement_close("SPY", "2026-08-07")
        assert get.call_count == 1  # second lookup served from cache

    def test_error_message_raises(self):
        provider = AlphaVantageProvider(api_key="demo", pause_s=0.0)
        body = {"Error Message": "Invalid API call"}
        with mock.patch("options_trader.data.alphavantage.requests.get",
                        return_value=_resp(body)):
            with pytest.raises(RuntimeError, match="Invalid API call"):
                provider.get_settlement_close("BOGUS", "2026-08-10")

    def test_premium_or_rate_limit_raises(self):
        provider = AlphaVantageProvider(api_key="demo", pause_s=0.0)
        body = {"Information": "This is a premium endpoint"}
        with mock.patch("options_trader.data.alphavantage.requests.get",
                        return_value=_resp(body)):
            with pytest.raises(RuntimeError, match="premium endpoint"):
                provider.get_settlement_close("SPY", "2026-08-10")


class TestSettlementProviderFactory:
    def test_yfinance_default(self):
        provider = get_settlement_provider()
        assert provider.__class__.__name__ == "YFinanceProvider"

    def test_alphavantage_uses_env_key(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "demo")
        provider = get_settlement_provider("alphavantage")
        assert isinstance(provider, AlphaVantageProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown price provider"):
            get_settlement_provider("carrier_pigeon")
