"""Tests for the DoltHub EOD importer. All API I/O mocked."""

from unittest import mock

import pytest

from options_trader.config import StrategyConfig
from options_trader.data.dolthub import (
    PAGE_SIZE,
    DoltHubClient,
    DoltHubImporter,
    rows_to_snapshots,
)
from options_trader.signals import generate_candidates


def _row(date="2026-06-01", symbol="SPY", exp="2026-06-05", strike="100.0",
         cp="Call", bid="1.58", ask="1.62", vol="0.5"):
    return {"date": date, "act_symbol": symbol, "expiration": exp,
            "strike": strike, "call_put": cp, "bid": bid, "ask": ask,
            "vol": vol}


SPOTS = {("SPY", "2026-06-01"): 100.0}


class TestRowConversion:
    def test_groups_by_date_symbol_expiration(self):
        rows = [
            _row(exp="2026-06-05"),
            _row(exp="2026-06-05", cp="Put", strike="98.0"),
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
        assert leg["type"] == "call"          # "Call" lowercased
        assert leg["strike"] == 100.0
        assert leg["bid"] == 1.58 and leg["ask"] == 1.62
        assert leg["iv"] == 0.5
        # Dataset has no volume/OI — imported as 0, never invented
        assert leg["volume"] == 0 and leg["open_interest"] == 0

    def test_days_without_spot_are_skipped(self):
        rows = [_row(), _row(date="2026-06-02")]  # no spot for 06-02
        snaps = rows_to_snapshots(rows, SPOTS)
        assert {s.taken_at[:10] for s in snaps} == {"2026-06-01"}

    def test_imported_chain_flows_through_candidate_generation(self):
        """End-to-end: DoltHub rows -> snapshot -> candidates, using the
        dolthub backtest config (volume/OI minimums zeroed)."""
        rows = [
            _row(strike="100.0", cp="Call", bid="1.58", ask="1.62"),
            _row(strike="102.0", cp="Call", bid="0.98", ask="1.02"),
            _row(strike="100.0", cp="Put", bid="1.53", ask="1.57"),
            _row(strike="98.0", cp="Put", bid="0.98", ask="1.02"),
        ]
        snap = rows_to_snapshots(rows, SPOTS)[0]
        strict = StrategyConfig(account_equity=10_000.0)
        assert generate_candidates(snap, strict) == []  # OI/vol=0 rejected
        relaxed = StrategyConfig(account_equity=10_000.0,
                                 min_open_interest=0, min_volume=0)
        cands = generate_candidates(snap, relaxed)
        assert cands, "zeroed liquidity minimums should admit dolthub rows"


class TestValidation:
    def test_bad_symbol_rejected(self):
        imp = DoltHubImporter(client=mock.Mock())
        with pytest.raises(ValueError, match="Suspicious symbol"):
            imp.available_dates("SPY'; DROP TABLE --", "2026-01-01", "2026-02-01")

    def test_bad_date_rejected(self):
        imp = DoltHubImporter(client=mock.Mock())
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            imp.fetch_day("SPY", "June 1st")


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
        c = DoltHubClient(pause_s=0)
        body = {"query_execution_status": "Success", "rows": [{"a": "1"}]}
        with mock.patch("options_trader.data.dolthub.requests.get",
                        return_value=FakeResp(200, body)):
            assert c.query("SELECT 1") == [{"a": "1"}]

    def test_query_failure_status_raises(self):
        c = DoltHubClient(pause_s=0)
        body = {"query_execution_status": "Error",
                "query_execution_message": "syntax error"}
        with mock.patch("options_trader.data.dolthub.requests.get",
                        return_value=FakeResp(200, body)):
            with pytest.raises(RuntimeError, match="syntax error"):
                c.query("SELEC 1")

    def test_retries_on_429(self):
        c = DoltHubClient(pause_s=0)
        ok = {"query_execution_status": "Success", "rows": []}
        responses = iter([FakeResp(429), FakeResp(200, ok)])
        with mock.patch("options_trader.data.dolthub.requests.get",
                        side_effect=lambda *a, **k: next(responses)), \
             mock.patch("options_trader.data.dolthub.time.sleep"):
            assert c.query("SELECT 1") == []

    def test_query_paged_drains_pages(self):
        c = DoltHubClient(pause_s=0)
        page1 = [{"i": str(i)} for i in range(PAGE_SIZE)]
        page2 = [{"i": "last"}]
        with mock.patch.object(c, "query", side_effect=[page1, page2]) as q, \
             mock.patch("options_trader.data.dolthub.time.sleep"):
            rows = c.query_paged("SELECT x FROM t ORDER BY x")
        assert len(rows) == PAGE_SIZE + 1
        assert f"LIMIT {PAGE_SIZE} OFFSET 0" in q.call_args_list[0].args[0]
        assert f"OFFSET {PAGE_SIZE}" in q.call_args_list[1].args[0]

    def test_fetch_day_query_shape(self):
        client = mock.Mock()
        client.query_paged.return_value = []
        imp = DoltHubImporter(client=client)
        imp.fetch_day("SPY", "2026-06-01", max_dte=7)
        sql = client.query_paged.call_args.args[0]
        assert "`act_symbol` = 'SPY'" in sql
        assert "`date` = '2026-06-01'" in sql
        assert "BETWEEN '2026-06-01' AND '2026-06-08'" in sql
        assert "ORDER BY" in sql
