"""Tests for the Unusual Whales EOD options importer. All API I/O mocked."""

from unittest import mock

import pytest

from options_trader.config import StrategyConfig
from options_trader.data.unusual_whales import (
    HistoricalAccessError,
    UWClient,
    UWImporter,
    rows_to_snapshots,
)
from options_trader.signals import generate_candidates


def _row(exp="2026-06-05", opt_type="call", strike=100.0, bid=1.58, ask=1.62,
         volume=120, open_interest=800, iv=0.5, delta=0.4):
    return {
        "expires": exp,
        "option_type": opt_type,
        "strike": strike,
        "nbbo_bid": bid,
        "nbbo_ask": ask,
        "volume": volume,
        "open_interest": open_interest,
        "implied_volatility": iv,
        "delta": delta,
        "option_symbol": f"SPY{exp}{opt_type[0].upper()}",
        "last_tape_time": "2026-06-01T21:10:13Z",
    }


SPOTS = {("SPY", "2026-06-01"): 100.0}


class TestRowConversion:
    def test_groups_by_expiration(self):
        rows = [
            _row(exp="2026-06-05"),
            _row(exp="2026-06-05", opt_type="put", strike=98.0),
            _row(exp="2026-06-12"),
        ]
        snaps = rows_to_snapshots("2026-06-01", "SPY", rows, SPOTS)
        assert len(snaps) == 2
        by_exp = {s.expiration: s for s in snaps}
        assert len(by_exp["2026-06-05"].chain) == 2
        assert len(by_exp["2026-06-12"].chain) == 1

    def test_field_mapping_and_eod_timestamp(self):
        snap = rows_to_snapshots("2026-06-01", "SPY", [_row()], SPOTS)[0]
        assert snap.underlying == "SPY" and snap.spot == 100.0
        assert snap.taken_at == "2026-06-01T16:00:00"
        assert snap.dte == 4
        leg = snap.chain.iloc[0]
        assert leg["type"] == "call"
        assert leg["strike"] == 100.0
        assert leg["bid"] == 1.58 and leg["ask"] == 1.62
        assert leg["iv"] == 0.5
        # Real volume/OI, richest of the three importers
        assert leg["volume"] == 120 and leg["open_interest"] == 800

    def test_null_greeks_field_treated_as_zero(self):
        row = _row(iv=None)
        snap = rows_to_snapshots("2026-06-01", "SPY", [row], SPOTS)[0]
        assert snap.chain.iloc[0]["iv"] == 0.0

    def test_day_without_spot_returns_no_snapshots(self):
        snaps = rows_to_snapshots("2026-06-02", "SPY", [_row()], SPOTS)
        assert snaps == []

    def test_imported_chain_flows_through_candidate_generation(self):
        """End-to-end: UW rows -> snapshot -> candidates, using the
        strategy's normal (non-zeroed) liquidity minimums."""
        rows = [
            _row(strike=100.0, opt_type="call", bid=1.58, ask=1.62),
            _row(strike=102.0, opt_type="call", bid=0.98, ask=1.02),
            _row(strike=100.0, opt_type="put", bid=1.53, ask=1.57),
            _row(strike=98.0, opt_type="put", bid=0.98, ask=1.02),
        ]
        snap = rows_to_snapshots("2026-06-01", "SPY", rows, SPOTS)[0]
        cfg = StrategyConfig(account_equity=10_000.0)  # real min_oi=500, min_vol=50
        cands = generate_candidates(snap, cfg)
        assert cands, "real volume/OI in the dataset should admit candidates"


class TestValidation:
    def test_bad_symbol_rejected(self):
        imp = UWImporter("key", client=mock.Mock())
        with pytest.raises(ValueError, match="Suspicious symbol"):
            imp.fetch_day("SPY'; DROP TABLE --", "2026-06-01")

    def test_bad_date_rejected(self):
        imp = UWImporter("key", client=mock.Mock())
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            imp.fetch_day("SPY", "June 1st")

    def test_missing_api_key_rejected(self):
        with pytest.raises(ValueError, match="UW_API_KEY"):
            UWClient("")


class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text or str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class TestClient:
    def test_option_chains_success(self):
        c = UWClient("key", pause_s=0)
        body = {"data": [{"option_symbol": "x"}]}
        with mock.patch("options_trader.data.unusual_whales.requests.get",
                        return_value=FakeResp(200, body)):
            assert c.option_chains("SPY", day="2026-06-01") == body["data"]

    def test_sends_bearer_auth_and_date_param(self):
        c = UWClient("mykey", pause_s=0)
        with mock.patch("options_trader.data.unusual_whales.requests.get",
                        return_value=FakeResp(200, {"data": []})) as get:
            c.option_chains("SPY", day="2026-06-01")
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer mykey"
        assert get.call_args.kwargs["params"]["date"] == "2026-06-01"
        assert get.call_args.kwargs["params"]["greeks"] == "true"

    def test_403_historic_access_missing_raises_typed_error(self):
        c = UWClient("key", pause_s=0)
        body = {"code": "historic_data_access_missing",
                "message": "The earliest date currently available to you is 2026-04-07"}
        with mock.patch("options_trader.data.unusual_whales.requests.get",
                        return_value=FakeResp(403, body)):
            with pytest.raises(HistoricalAccessError, match="2026-04-07"):
                c.option_chains("SPY", day="2020-01-01")

    def test_other_403_raises_plain_runtime_error(self):
        c = UWClient("key", pause_s=0)
        with mock.patch("options_trader.data.unusual_whales.requests.get",
                        return_value=FakeResp(403, {"message": "nope"})):
            with pytest.raises(RuntimeError, match="nope"):
                c.option_chains("SPY", day="2026-06-01")

    def test_retries_on_429(self):
        c = UWClient("key", pause_s=0)
        responses = iter([FakeResp(429), FakeResp(200, {"data": []})])
        with mock.patch("options_trader.data.unusual_whales.requests.get",
                        side_effect=lambda *a, **k: next(responses)), \
             mock.patch("options_trader.data.unusual_whales.time.sleep"):
            assert c.option_chains("SPY", day="2026-06-01") == []

    def test_fetch_day_filters_by_max_dte(self):
        client = mock.Mock()
        client.option_chains.return_value = [
            _row(exp="2026-06-05"),   # within window
            _row(exp="2026-06-20"),   # outside window
        ]
        imp = UWImporter("key", client=client)
        rows = imp.fetch_day("SPY", "2026-06-01", max_dte=7)
        assert len(rows) == 1
        assert rows[0]["expires"] == "2026-06-05"

    def test_fetch_day_propagates_historical_access_error(self):
        client = mock.Mock()
        client.option_chains.side_effect = HistoricalAccessError("too old")
        imp = UWImporter("key", client=client)
        with pytest.raises(HistoricalAccessError):
            imp.fetch_day("SPY", "2020-01-01")
