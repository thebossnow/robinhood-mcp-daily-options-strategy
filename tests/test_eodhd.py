"""Tests for the EODHD provider. All HTTP mocked.

These cover the request *shape* (JSON:API filter[]/page[] encoding, which the
API rejects the request without), response normalization from both flat and
JSON:API encodings, and the failure modes that matter operationally: a plan
without the options add-on, and a truncated chain.
"""

from unittest import mock

import pytest

from options_trader.config import StrategyConfig
from options_trader.data.eodhd import (
    PAGE_LIMIT,
    EODHDClient,
    EODHDError,
    EODHDHistory,
    EODHDProvider,
    EODHDSubscriptionError,
    parse_occ,
    rows_to_snapshots,
)
from options_trader.signals import generate_candidates


class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _contract(strike=100.0, cp="call", exp="2026-06-05", bid=1.58, ask=1.62,
              volume=800, oi=2500, iv=0.5, name=None):
    return {
        "contractName": name or "SPY260605C00100000",
        "type": cp,
        "strike": strike,
        "expirationDate": exp,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "openInterest": oi,
        "impliedVolatility": iv,
    }


def _client(bodies):
    """An EODHDClient whose requests.get returns the given bodies in order."""
    c = EODHDClient(api_key="test-key", pause_s=0)
    responses = iter([FakeResp(200, b) for b in bodies])
    patcher = mock.patch("options_trader.data.eodhd.requests.get",
                         side_effect=lambda *a, **k: next(responses))
    return c, patcher


class TestOCC:
    def test_parses_standard_contract(self):
        got = parse_occ("SPY260814C00500000")
        assert got == {"underlying": "SPY", "expiration": "2026-08-14",
                       "type": "call", "strike": 500.0}

    def test_parses_put_and_fractional_strike(self):
        got = parse_occ("QQQ260814P00437500")
        assert got["type"] == "put" and got["strike"] == 437.5

    def test_returns_none_for_junk(self):
        assert parse_occ("not-a-contract") is None
        assert parse_occ("SPY261332C00500000") is None  # month 13


class TestClientRequestShape:
    def test_filters_and_pagination_use_bracket_syntax(self):
        """The API rejects flat `underlying_symbol=` with a validation error;
        it requires JSON:API filter[...] / page[...] encoding."""
        c = EODHDClient(api_key="k", pause_s=0)
        with mock.patch("options_trader.data.eodhd.requests.get",
                        return_value=FakeResp(200, {"data": []})) as get:
            c.get_paged("some/path", {"underlying_symbol": "SPY",
                                      "exp_date_from": "2026-06-01"})
        params = get.call_args.kwargs["params"]
        assert params["filter[underlying_symbol]"] == "SPY"
        assert params["filter[exp_date_from]"] == "2026-06-01"
        assert params["page[limit]"] == PAGE_LIMIT
        assert params["page[offset]"] == 0
        assert params["api_token"] == "k" and params["fmt"] == "json"

    def test_none_filters_are_dropped(self):
        c = EODHDClient(api_key="k", pause_s=0)
        with mock.patch("options_trader.data.eodhd.requests.get",
                        return_value=FakeResp(200, {"data": []})) as get:
            c.get_paged("p", {"underlying_symbol": "SPY", "exp_date_to": None})
        assert "filter[exp_date_to]" not in get.call_args.kwargs["params"]

    def test_pagination_drains_pages(self):
        full = [_contract() for _ in range(PAGE_LIMIT)]
        c, patcher = _client([{"data": full}, {"data": [_contract()]}])
        with patcher as get, mock.patch("options_trader.data.eodhd.time.sleep"):
            rows = c.get_paged("p", {"underlying_symbol": "SPY"})
        assert len(rows) == PAGE_LIMIT + 1
        assert get.call_args_list[1].kwargs["params"]["page[offset]"] == PAGE_LIMIT

    def test_json_api_attributes_are_unwrapped(self):
        body = {"data": [{"id": "SPY260605C00100000", "type": "option",
                          "attributes": {"strike": 100.0, "type": "call",
                                         "bid": 1.5, "volume": 10}}]}
        c, patcher = _client([body])
        with patcher:
            rows = c.get_paged("p", {"underlying_symbol": "SPY"})
        assert rows[0]["strike"] == 100.0 and rows[0]["bid"] == 1.5
        assert rows[0]["id"] == "SPY260605C00100000"

    def test_billed_call_estimate_tracks_options_cost(self):
        c, patcher = _client([{"data": []}])
        with patcher:
            c.get_paged("mp/unicornbay/options/contracts", {"underlying_symbol": "SPY"})
        assert c.calls_billed == 10   # options endpoints bill 10 units


class TestClientErrors:
    def test_missing_key_is_explicit(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with pytest.raises(EODHDError, match="No EODHD API key"):
                EODHDClient()

    def test_402_reports_missing_options_addon(self):
        c = EODHDClient(api_key="k", pause_s=0)
        with mock.patch("options_trader.data.eodhd.requests.get",
                        return_value=FakeResp(402, text="payment required")):
            with pytest.raises(EODHDSubscriptionError, match="UnicornBay"):
                c.get("mp/unicornbay/options/eod")

    def test_403_is_a_subscription_error_not_a_crash(self):
        c = EODHDClient(api_key="k", pause_s=0)
        with mock.patch("options_trader.data.eodhd.requests.get",
                        return_value=FakeResp(403)):
            with pytest.raises(EODHDSubscriptionError):
                c.get("mp/unicornbay/options/contracts")

    def test_validation_errors_in_body_are_raised(self):
        """The real API returned this shape when filters were flat."""
        body = {"errors": {"filter.underlying_symbol": [
            "The filter.underlying symbol field is required when "
            "filter.contract is not present."]}}
        c = EODHDClient(api_key="k", pause_s=0)
        with mock.patch("options_trader.data.eodhd.requests.get",
                        return_value=FakeResp(200, body)):
            with pytest.raises(EODHDError, match="rejected the request"):
                c.get("mp/unicornbay/options/eod")

    def test_retries_on_429(self):
        c = EODHDClient(api_key="k", pause_s=0)
        responses = iter([FakeResp(429), FakeResp(200, {"data": []})])
        with mock.patch("options_trader.data.eodhd.requests.get",
                        side_effect=lambda *a, **k: next(responses)), \
             mock.patch("options_trader.data.eodhd.time.sleep"):
            assert c.get("p") == {"data": []}


class TestProvider:
    def _provider(self, contracts, spot=100.0):
        client = mock.Mock()
        client.get_paged.return_value = contracts
        client.real_time.return_value = {"close": spot}
        return EODHDProvider(client=client, min_dte=0, max_dte=60), client

    def test_window_fetch_is_one_request_for_all_expirations(self):
        """Options requests bill 10 units each — a scan must not pay per
        expiration."""
        rows = [_contract(exp="2026-06-05"), _contract(exp="2026-06-12")]
        provider, client = self._provider(rows)
        assert provider.get_expirations("SPY") == ["2026-06-05", "2026-06-12"]
        provider.get_expirations("SPY")
        assert client.get_paged.call_count == 1     # cached

    def test_window_filters_by_dte(self):
        provider, client = self._provider([_contract()])
        provider.get_expirations("SPY")
        filters = client.get_paged.call_args.args[1]
        assert filters["underlying_symbol"] == "SPY"
        assert "exp_date_from" in filters and "exp_date_to" in filters

    def test_chain_carries_real_volume_and_open_interest(self):
        """The whole reason this provider exists over the DoltHub importer."""
        rows = [
            _contract(strike=102.0, cp="call", volume=800, oi=2500),
            _contract(strike=98.0, cp="put", volume=640, oi=1900),
        ]
        provider, _ = self._provider(rows)
        snap = provider.get_chain("SPY", "2026-06-05")
        assert snap.spot == 100.0 and snap.underlying == "SPY"
        assert set(snap.chain["volume"]) == {800, 640}
        assert set(snap.chain["open_interest"]) == {2500, 1900}
        assert set(snap.chain["type"]) == {"call", "put"}

    def test_unknown_expiration_lists_what_is_available(self):
        provider, _ = self._provider([_contract(exp="2026-06-05")])
        with pytest.raises(EODHDError, match="2026-06-05"):
            provider.get_chain("SPY", "2026-06-19")

    def test_truncated_chain_is_refused(self):
        """Calls only, no puts — must raise rather than poison a snapshot."""
        rows = [_contract(strike=100.0, cp="call"),
                _contract(strike=102.0, cp="call")]
        provider, _ = self._provider(rows)
        with pytest.raises(EODHDError, match="incomplete"):
            provider.get_chain("SPY", "2026-06-05")

    def test_strikes_must_straddle_spot(self):
        rows = [_contract(strike=200.0, cp="call"), _contract(strike=202.0, cp="put")]
        provider, _ = self._provider(rows, spot=100.0)
        with pytest.raises(EODHDError, match="straddle"):
            provider.get_chain("SPY", "2026-06-05")

    def test_empty_window_names_the_addon(self):
        provider, _ = self._provider([])
        with pytest.raises(EODHDError, match="UnicornBay"):
            provider.get_expirations("SPY")

    def test_spot_falls_back_to_eod_close(self):
        client = mock.Mock()
        client.real_time.return_value = {"close": "NA"}
        client.eod.return_value = [{"date": "2026-06-01", "close": 123.45}]
        provider = EODHDProvider(client=client)
        assert provider.get_spot("SPY") == 123.45

    def test_bad_symbol_rejected(self):
        provider, _ = self._provider([])
        with pytest.raises(ValueError, match="Suspicious symbol"):
            provider.get_spot("SPY'; DROP TABLE --")


SPOTS = {("SPY", "2026-06-01"): 100.0}


def _eod_row(**kw):
    row = _contract(**{k: v for k, v in kw.items() if k != "day"})
    row["underlying_symbol"] = "SPY"
    row["tradetime"] = kw.get("day", "2026-06-01")
    return row


class TestHistory:
    def test_groups_by_day_and_expiration(self):
        rows = [
            _eod_row(strike=100.0, cp="call"),
            _eod_row(strike=98.0, cp="put"),
            _eod_row(strike=100.0, cp="call", exp="2026-06-12"),
            _eod_row(strike=98.0, cp="put", exp="2026-06-12"),
        ]
        snaps = rows_to_snapshots(rows, SPOTS)
        assert len(snaps) == 2
        assert {s.expiration for s in snaps} == {"2026-06-05", "2026-06-12"}
        assert all(s.taken_at == "2026-06-01T16:00:00" for s in snaps)

    def test_days_without_spot_are_skipped(self):
        rows = [_eod_row(), _eod_row(day="2026-06-02")]
        snaps = rows_to_snapshots(rows, SPOTS)
        assert {s.taken_at[:10] for s in snaps} == {"2026-06-01"}

    def test_falls_back_to_occ_name_when_fields_missing(self):
        row = {"contractName": "SPY260605C00100000", "bid": 1.5, "ask": 1.6,
               "volume": 700, "openInterest": 2000, "impliedVolatility": 0.4,
               "tradetime": "2026-06-01"}
        put = {"contractName": "SPY260605P00098000", "bid": 1.0, "ask": 1.1,
               "volume": 700, "openInterest": 2000, "impliedVolatility": 0.4,
               "tradetime": "2026-06-01"}
        snaps = rows_to_snapshots([row, put], SPOTS)
        assert len(snaps) == 1
        leg = snaps[0].chain.iloc[0]
        assert leg["type"] == "call" and leg["strike"] == 100.0
        assert snaps[0].expiration == "2026-06-05"

    def test_fetch_eod_filter_shape(self):
        client = mock.Mock()
        client.get_paged.return_value = []
        hist = EODHDHistory(client=client)
        hist.fetch_eod("SPY", "2026-01-01", "2026-02-01", max_dte=7)
        path, filters = client.get_paged.call_args.args
        assert path.endswith("options/eod")
        assert filters["underlying_symbol"] == "SPY"
        assert filters["tradetime_from"] == "2026-01-01"
        assert filters["tradetime_to"] == "2026-02-01"
        assert filters["exp_date_to"] == "2026-02-08"

    def test_bad_date_rejected(self):
        hist = EODHDHistory(client=mock.Mock())
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            hist.fetch_eod("SPY", "Jan 1st", "2026-02-01")

    def test_history_passes_the_STRICT_liquidity_filter(self):
        """Contrast with test_dolthub: DoltHub rows have OI/volume 0 and only
        pass a relaxed config. EODHD rows carry real liquidity, so the
        production config admits them unmodified."""
        rows = [
            _eod_row(strike=100.0, cp="call", bid=1.58, ask=1.62),
            _eod_row(strike=102.0, cp="call", bid=0.98, ask=1.02),
            _eod_row(strike=100.0, cp="put", bid=1.53, ask=1.57),
            _eod_row(strike=98.0, cp="put", bid=0.98, ask=1.02),
        ]
        snap = rows_to_snapshots(rows, SPOTS)[0]
        strict = StrategyConfig(account_equity=10_000.0)
        assert generate_candidates(snap, strict), \
            "real volume/OI should pass the production liquidity filter"
