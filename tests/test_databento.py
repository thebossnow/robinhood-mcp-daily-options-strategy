"""Tests for the Databento (OPRA) EOD options history source. All API I/O
mocked — no test in this file hits Databento or spends real money."""

from unittest import mock

import pandas as pd
import pytest

from options_trader.data.databento import (
    DAY_CHAIN_COLUMNS,
    DatabentoClient,
    DatabentoHistory,
    parse_occ_symbol,
    _close_window_utc,
)


def _quote_row(symbol="SOFI  230616P00010000", bid=0.53, ask=0.54,
               ts_event="2023-06-15T19:59:30Z"):
    return {"symbol": symbol, "bid_px_00": bid, "ask_px_00": ask, "ts_event": ts_event}


class TestParseOccSymbol:
    def test_put(self):
        root, exp, opt_type, strike = parse_occ_symbol("SOFI  230616P00001500")
        assert root == "SOFI"
        assert exp == "2023-06-16"
        assert opt_type == "put"
        assert strike == 1.5

    def test_call_fractional_strike(self):
        root, exp, opt_type, strike = parse_occ_symbol("SOFI  230616C00010500")
        assert opt_type == "call"
        assert strike == 10.5

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_occ_symbol("not an occ symbol")


class TestCloseWindow:
    def test_handles_dst(self):
        # EDT (UTC-4) in June vs EST (UTC-5) in January
        start_summer, end_summer = _close_window_utc("2023-06-15")
        start_winter, end_winter = _close_window_utc("2023-01-15")
        assert end_summer == "2023-06-15T20:00:00Z"
        assert end_winter == "2023-01-15T21:00:00Z"


class TestDatabentoClientNoDataDay:
    def test_symbology_invalid_request_returns_empty_not_raise(self):
        """A day with no listed options (pre-IPO, or a rare unexplained
        gap -- 2 of ~8500 real AMC days hit this) makes the smart-symbol
        resolver 422 rather than returning zero rows. day_chains must not
        propagate this: the whole backtest crashed on exactly this before
        the fix (SOFI, a plain weekday with no options quoted)."""
        from databento.common.error import BentoClientError
        client = DatabentoClient.__new__(DatabentoClient)
        client.client = mock.Mock()
        client.client.timeseries.get_range.side_effect = BentoClientError(
            http_status=422,
            json_body={"detail": {"case": "symbology_invalid_request",
                                  "message": "Could not resolve smart symbols: SOFI.OPT"}},
        )
        assert client.eod_quotes("SOFI", "2021-05-20") == []

    def test_other_client_errors_still_raise(self):
        from databento.common.error import BentoClientError
        client = DatabentoClient.__new__(DatabentoClient)
        client.client = mock.Mock()
        client.client.timeseries.get_range.side_effect = BentoClientError(
            http_status=401, json_body={"detail": {"case": "auth_invalid_key"}},
        )
        with pytest.raises(BentoClientError):
            client.eod_quotes("SOFI", "2021-05-20")


class TestDatabentoClientTransientRetry:
    """A multi-hour real backtest run crashed on a bare ConnectTimeout with
    zero retry — unlike DoltHubClient/UWClient, which both retry transient
    errors. These lock in the fix."""

    def test_retries_then_succeeds(self):
        import requests
        client = DatabentoClient.__new__(DatabentoClient)
        client.client = mock.Mock()
        ok_result = mock.Mock()
        ok_result.to_df.return_value = pd.DataFrame([_quote_row()])
        client.client.timeseries.get_range.side_effect = [
            requests.exceptions.ConnectTimeout("timed out"),
            requests.exceptions.ConnectTimeout("timed out"),
            ok_result,
        ]
        with mock.patch("time.sleep"):
            rows = client.eod_quotes("SOFI", "2023-06-15")
        assert len(rows) == 1
        assert client.client.timeseries.get_range.call_count == 3

    def test_exhausts_retries_and_raises(self):
        import requests
        client = DatabentoClient.__new__(DatabentoClient)
        client.client = mock.Mock()
        client.client.timeseries.get_range.side_effect = requests.exceptions.ConnectTimeout(
            "timed out")
        with mock.patch("time.sleep"):
            with pytest.raises(RuntimeError, match="retries exhausted"):
                client.eod_quotes("SOFI", "2023-06-15")
        assert client.client.timeseries.get_range.call_count == 4


class TestDatabentoHistory:
    def test_day_chains_columns_and_values(self, tmp_path):
        rows = [_quote_row()]
        client = mock.Mock()
        client.eod_quotes.return_value = rows
        hist = DatabentoHistory(client=client, cache_dir=tmp_path,
                                spot_lookup={("SOFI", "2023-06-15"): 10.0})
        df = hist.day_chains("SOFI", "2023-06-15", max_dte=50)
        assert list(df.columns) == DAY_CHAIN_COLUMNS
        assert len(df) == 1
        leg = df.iloc[0]
        assert leg["expiration"] == "2023-06-16"
        assert leg["type"] == "put"
        assert leg["strike"] == 10.0
        assert leg["bid"] == 0.53 and leg["ask"] == 0.54
        assert leg["iv"] > 0    # solved from price, not from the dataset
        assert leg["delta"] == 0.0   # left for the caller's BS fallback

    def test_no_spot_returns_empty_frame(self, tmp_path):
        client = mock.Mock()
        client.eod_quotes.return_value = [_quote_row()]
        hist = DatabentoHistory(client=client, cache_dir=tmp_path, spot_lookup={})
        df = hist.day_chains("SOFI", "2023-06-15")
        assert df.empty
        assert list(df.columns) == DAY_CHAIN_COLUMNS
        client.eod_quotes.assert_not_called()   # no point spending the call

    def test_no_rows_returns_empty_frame(self, tmp_path):
        client = mock.Mock()
        client.eod_quotes.return_value = []
        hist = DatabentoHistory(client=client, cache_dir=tmp_path,
                                spot_lookup={("SOFI", "2023-06-15"): 10.0})
        df = hist.day_chains("SOFI", "2023-06-15")
        assert df.empty

    def test_filters_expiration_outside_max_dte(self, tmp_path):
        rows = [_quote_row(symbol="SOFI  230616P00001500"),
               _quote_row(symbol="SOFI  231215P00001500", ts_event="2023-06-15T19:59:31Z")]
        client = mock.Mock()
        client.eod_quotes.return_value = rows
        hist = DatabentoHistory(client=client, cache_dir=tmp_path,
                                spot_lookup={("SOFI", "2023-06-15"): 10.0})
        df = hist.day_chains("SOFI", "2023-06-15", max_dte=10)
        assert len(df) == 1
        assert df.iloc[0]["expiration"] == "2023-06-16"

    def test_dedupes_to_latest_quote_per_contract(self, tmp_path):
        rows = [
            _quote_row(bid=0.50, ask=0.55, ts_event="2023-06-15T19:59:00Z"),
            _quote_row(bid=0.53, ask=0.54, ts_event="2023-06-15T19:59:30Z"),
        ]
        client = mock.Mock()
        client.eod_quotes.return_value = rows
        hist = DatabentoHistory(client=client, cache_dir=tmp_path,
                                spot_lookup={("SOFI", "2023-06-15"): 10.0})
        df = hist.day_chains("SOFI", "2023-06-15")
        assert len(df) == 1
        assert df.iloc[0]["bid"] == 0.53 and df.iloc[0]["ask"] == 0.54

    def test_skips_crossed_or_zero_quotes(self, tmp_path):
        rows = [_quote_row(bid=0.0, ask=0.0), _quote_row(bid=0.60, ask=0.55)]
        client = mock.Mock()
        client.eod_quotes.return_value = rows
        hist = DatabentoHistory(client=client, cache_dir=tmp_path,
                                spot_lookup={("SOFI", "2023-06-15"): 10.0})
        df = hist.day_chains("SOFI", "2023-06-15")
        assert df.empty

    def test_caches_to_disk_and_skips_second_api_call(self, tmp_path):
        client = mock.Mock()
        client.eod_quotes.return_value = [_quote_row()]
        hist = DatabentoHistory(client=client, cache_dir=tmp_path,
                                spot_lookup={("SOFI", "2023-06-15"): 10.0})
        hist.day_chains("SOFI", "2023-06-15")
        hist.day_chains("SOFI", "2023-06-15")
        assert client.eod_quotes.call_count == 1

    def test_prefetch_warms_cache_for_all_symbol_day_pairs(self, tmp_path):
        client = mock.Mock()
        client.eod_quotes.return_value = [_quote_row()]
        hist = DatabentoHistory(client=client, cache_dir=tmp_path,
                                spot_lookup={("SOFI", "2023-06-15"): 10.0})
        hist.prefetch(["SOFI"], ["2023-06-15"])
        assert (tmp_path / "SOFI_2023-06-15.json").exists()
