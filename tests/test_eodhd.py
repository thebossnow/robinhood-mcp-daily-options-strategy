"""Tests for the EODHD EOD options importer. All API I/O mocked."""

from unittest import mock

import pytest

from options_trader.config import StrategyConfig
from options_trader.data.eodhd import (
    PAGE_LIMIT,
    EODHDClient,
    EODHDImporter,
    rows_to_snapshots,
)
from options_trader.signals import generate_candidates


def _row(day="2026-06-01", symbol="SPY", exp="2026-06-05", strike=100.0,
         opt_type="call", bid=1.58, ask=1.62, volume=120, open_interest=800,
         iv=0.5):
    return {
        "underlying_symbol": symbol,
        "exp_date": exp,
        "type": opt_type,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "open_interest": open_interest,
        "volatility": iv,
        "tradetime": day,
    }


SPOTS = {("SPY", "2026-06-01"): 100.0}


class TestRowConversion:
    def test_groups_by_date_symbol_expiration(self):
        rows = [
            _row(exp="2026-06-05"),
            _row(exp="2026-06-05", opt_type="put", strike=98.0),
            _row(exp="2026-06-12"),
        ]
        snaps = rows_to_snapshots(rows, SPOTS)
        assert len(snaps) == 2
        by_exp = {s.expiration: s for s in snaps}
        assert len(by_exp["2026-06-05"].chain) == 2
        assert len(by_exp["2026-06-12"].chain) == 1

    def test_field_mapping_and_eod_timestamp(self):
        snap = rows_to_snapshots([_row()], SPOTS)[0]
        assert snap.underlying == "SPY" and snap.spot == 100.0
        assert snap.taken_at == "2026-06-01T16:00:00"
        assert snap.dte == 4
        leg = snap.chain.iloc[0]
        assert leg["type"] == "call"
        assert leg["strike"] == 100.0
        assert leg["bid"] == 1.58 and leg["ask"] == 1.62
        assert leg["iv"] == 0.5
        # Unlike DoltHub, this dataset carries real volume/OI
        assert leg["volume"] == 120 and leg["open_interest"] == 800

    def test_days_without_spot_are_skipped(self):
        rows = [_row(), _row(day="2026-06-02")]  # no spot for 06-02
        snaps = rows_to_snapshots(rows, SPOTS)
        assert {s.taken_at[:10] for s in snaps} == {"2026-06-01"}

    def test_imported_chain_flows_through_candidate_generation(self):
        """End-to-end: EODHD rows -> snapshot -> candidates, using the
        strategy's normal (non-zeroed) liquidity minimums."""
        rows = [
            _row(strike=100.0, opt_type="call", bid=1.58, ask=1.62),
            _row(strike=102.0, opt_type="call", bid=0.98, ask=1.02),
            _row(strike=100.0, opt_type="put", bid=1.53, ask=1.57),
            _row(strike=98.0, opt_type="put", bid=0.98, ask=1.02),
        ]
        snap = rows_to_snapshots(rows, SPOTS)[0]
        cfg = StrategyConfig(account_equity=10_000.0)  # real min_oi=500, min_vol=50
        cands = generate_candidates(snap, cfg)
        assert cands, "real volume/OI in the dataset should admit candidates"


class TestValidation:
    def test_bad_symbol_rejected(self):
        imp = EODHDImporter("key", client=mock.Mock())
        with pytest.raises(ValueError, match="Suspicious symbol"):
            imp.fetch_day("SPY'; DROP TABLE --", "2026-01-01")

    def test_bad_date_rejected(self):
        imp = EODHDImporter("key", client=mock.Mock())
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            imp.fetch_day("SPY", "June 1st")

    def test_missing_api_key_rejected(self):
        with pytest.raises(ValueError, match="EODHD_API_KEY"):
            EODHDClient("")


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class TestClient:
    def test_query_success(self):
        c = EODHDClient("key", pause_s=0)
        body = {"data": [{"id": "x", "attributes": {"a": "1"}}]}
        with mock.patch("options_trader.data.eodhd.requests.get",
                        return_value=FakeResp(200, body)):
            assert c.query({"underlying_symbol": "SPY"}) == body["data"]

    def test_query_sends_api_token_and_filters(self):
        c = EODHDClient("mykey", pause_s=0)
        with mock.patch("options_trader.data.eodhd.requests.get",
                        return_value=FakeResp(200, {"data": []})) as get:
            c.query({"underlying_symbol": "SPY", "tradetime_eq": "2026-06-01"})
        params = get.call_args.kwargs["params"]
        assert params["api_token"] == "mykey"
        assert params["filter[underlying_symbol]"] == "SPY"
        assert params["filter[tradetime_eq]"] == "2026-06-01"

    def test_403_raises_add_on_hint(self):
        c = EODHDClient("key", pause_s=0)
        with mock.patch("options_trader.data.eodhd.requests.get",
                        return_value=FakeResp(403, {})):
            with pytest.raises(RuntimeError, match="marketplace"):
                c.query({"underlying_symbol": "SPY"})

    def test_retries_on_429(self):
        c = EODHDClient("key", pause_s=0)
        responses = iter([FakeResp(429), FakeResp(200, {"data": []})])
        with mock.patch("options_trader.data.eodhd.requests.get",
                        side_effect=lambda *a, **k: next(responses)), \
             mock.patch("options_trader.data.eodhd.time.sleep"):
            assert c.query({"underlying_symbol": "SPY"}) == []

    def test_query_all_drains_pages(self):
        c = EODHDClient("key", pause_s=0)
        page1 = [{"id": str(i), "attributes": {}} for i in range(PAGE_LIMIT)]
        page2 = [{"id": "last", "attributes": {}}]
        with mock.patch.object(c, "query", side_effect=[page1, page2]) as q, \
             mock.patch("options_trader.data.eodhd.time.sleep"):
            rows = c.query_all({"underlying_symbol": "SPY"})
        assert len(rows) == PAGE_LIMIT + 1
        assert q.call_args_list[0].kwargs["offset"] == 0
        assert q.call_args_list[1].kwargs["offset"] == PAGE_LIMIT

    def test_fetch_day_query_shape(self):
        client = mock.Mock()
        client.query_all.return_value = []
        imp = EODHDImporter("key", client=client)
        imp.fetch_day("SPY", "2026-06-01", max_dte=7)
        filters = client.query_all.call_args.args[0]
        assert filters["underlying_symbol"] == "SPY"
        assert filters["tradetime_eq"] == "2026-06-01"
        assert filters["exp_date_from"] == "2026-06-01"
        assert filters["exp_date_to"] == "2026-06-08"
