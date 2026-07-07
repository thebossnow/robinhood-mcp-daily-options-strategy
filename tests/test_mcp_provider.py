"""Unit tests for the MCP data provider. All network I/O is mocked — these
verify the transport parsing, pagination, chain validation, OAuth refresh,
and retry logic that the live run could not exercise."""

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest
import requests

from options_trader.data.mcp_provider import (
    MCPDataProvider,
    _find_cursor,
    _parse_result,
    _read_jsonrpc,
    _refresh_token,
    _discover_token_endpoint,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeResp:
    def __init__(self, status=200, json_data=None, headers=None,
                 sse_lines=None, text=""):
        self.status_code = status
        self._json = json_data
        self.headers = dict(headers or {})
        self._sse = sse_lines
        self.text = text
        if sse_lines is not None:
            self.headers.setdefault("Content-Type", "text/event-stream")
        elif json_data is not None:
            self.headers.setdefault("Content-Type", "application/json")

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._json

    def iter_lines(self, decode_unicode=True):
        return iter(self._sse or [])

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(str(self.status_code))
            err.response = self
            raise err


def _provider():
    return MCPDataProvider(
        token={"access_token": "tok", "refresh_token": "ref"}, connect=False
    )


def _wrap(data: dict) -> dict:
    """Wrap payload the way tools/call returns it."""
    return {"result": {"structuredContent": {"data": data}}}


class TestJsonRpcParsing:
    def test_sse_matches_request_id(self):
        resp = FakeResp(sse_lines=[
            'data: {"jsonrpc":"2.0","id":99,"result":{"other":true}}',
            "",
            'data: {"jsonrpc":"2.0","id":7,"result":{"mine":true}}',
        ])
        msg = _read_jsonrpc(resp, request_id=7)
        assert msg["result"] == {"mine": True}

    def test_sse_no_match_returns_none(self):
        resp = FakeResp(sse_lines=['data: {"jsonrpc":"2.0","id":99,"result":{}}'])
        assert _read_jsonrpc(resp, request_id=7) is None

    def test_plain_json_response(self):
        resp = FakeResp(json_data={"jsonrpc": "2.0", "id": 7, "result": {"a": 1}})
        assert _read_jsonrpc(resp, 7)["result"] == {"a": 1}

    def test_unexpected_content_type_raises(self):
        resp = FakeResp(headers={"Content-Type": "text/html"})
        with pytest.raises(RuntimeError, match="content type"):
            _read_jsonrpc(resp, 1)


class TestParseResult:
    def test_prefers_structured_content(self):
        assert _parse_result(_wrap({"x": 1})) == {"x": 1}

    def test_falls_back_to_text_content(self):
        resp = {"result": {"content": [{"text": json.dumps({"data": {"y": 2}})}]}}
        assert _parse_result(resp) == {"y": 2}

    def test_api_error_text_raises(self):
        resp = {"result": {"content": [{"text": "API error: forbidden"}]}}
        with pytest.raises(RuntimeError, match="API error"):
            _parse_result(resp)

    def test_garbage_text_raises(self):
        resp = {"result": {"content": [{"text": "<html>nope"}]}}
        with pytest.raises(RuntimeError, match="unparseable"):
            _parse_result(resp)


class TestFindCursor:
    def test_bare_cursor(self):
        assert _find_cursor({"next_cursor": "abc"}) == "abc"

    def test_nested_pagination(self):
        assert _find_cursor({"pagination": {"cursor": "p2"}}) == "p2"

    def test_robinhood_style_next_url(self):
        url = "https://api.example.com/instruments/?cursor=CURSOR123&x=1"
        assert _find_cursor({"next": url}) == "CURSOR123"

    def test_no_cursor(self):
        assert _find_cursor({"instruments": []}) is None
        assert _find_cursor({"next": None}) is None


class TestInstrumentPagination:
    def test_follows_cursor_across_pages(self):
        p = _provider()
        pages = [
            _wrap({"instruments": [{"id": "a"}, {"id": "b"}], "next_cursor": "c2"}),
            _wrap({"instruments": [{"id": "c"}], "next_cursor": None}),
        ]
        calls = []

        def fake_call(name, args):
            calls.append(args)
            return pages[len(calls) - 1]

        p._call = fake_call
        out = p._fetch_all_instruments("chain1", "2026-07-10")
        assert [i["id"] for i in out] == ["a", "b", "c"]
        assert "cursor" not in calls[0]
        assert calls[1]["cursor"] == "c2"

    def test_warns_on_suspicious_page_boundary(self, caplog):
        p = _provider()
        p._call = lambda n, a: _wrap(
            {"instruments": [{"id": str(i)} for i in range(100)]}
        )
        with caplog.at_level("WARNING"):
            out = p._fetch_all_instruments("chain1", "2026-07-10")
        assert len(out) == 100
        assert any("page-size multiple" in r.message for r in caplog.records)

    def test_runaway_pagination_raises(self):
        p = _provider()
        p._call = lambda n, a: _wrap(
            {"instruments": [{"id": "x"}], "next_cursor": "forever"}
        )
        with pytest.raises(RuntimeError, match="did not terminate"):
            p._fetch_all_instruments("chain1", "2026-07-10")


class TestChainValidation:
    def _chain(self, rows):
        return pd.DataFrame(
            rows, columns=["type", "strike", "bid", "ask", "volume",
                           "open_interest", "iv"]
        )

    def test_good_chain_passes(self):
        chain = self._chain([
            ("call", 95.0, 1, 1.1, 10, 100, 0.3),
            ("put", 105.0, 1, 1.1, 10, 100, 0.3),
        ])
        MCPDataProvider._validate_chain(chain, "T", "2026-07-10", 100.0)

    def test_missing_puts_raises(self):
        chain = self._chain([("call", 95.0, 1, 1.1, 10, 100, 0.3),
                             ("call", 105.0, 1, 1.1, 10, 100, 0.3)])
        with pytest.raises(RuntimeError, match="incomplete"):
            MCPDataProvider._validate_chain(chain, "T", "2026-07-10", 100.0)

    def test_one_sided_strikes_raise(self):
        # Truncation signature: every strike below spot
        chain = self._chain([("call", 80.0, 1, 1.1, 10, 100, 0.3),
                             ("put", 90.0, 1, 1.1, 10, 100, 0.3)])
        with pytest.raises(RuntimeError, match="straddle"):
            MCPDataProvider._validate_chain(chain, "T", "2026-07-10", 100.0)


class TestChainSelection:
    def test_picks_symbol_match_not_first(self):
        p = _provider()
        p._call = lambda n, a: _wrap({"chains": [
            {"id": "adjusted", "symbol": "SPY1"},
            {"id": "standard", "symbol": "SPY"},
        ]})
        assert p._chain_meta("SPY")["id"] == "standard"

    def test_caches_chain_meta(self):
        p = _provider()
        counter = {"n": 0}

        def fake_call(name, args):
            counter["n"] += 1
            return _wrap({"chains": [{"id": "c1", "symbol": "SPY"}]})

        p._call = fake_call
        p._chain_meta("SPY")
        p._chain_meta("SPY")
        assert counter["n"] == 1


class TestOAuthRefresh:
    def test_refresh_is_form_encoded_with_client_id(self):
        token = {"access_token": "old", "refresh_token": "ref",
                 "client_id": "cid", "token_url": "https://as.example/token"}
        with mock.patch("options_trader.data.mcp_provider.requests.post") as post:
            post.return_value = FakeResp(200, json_data={
                "access_token": "new", "refresh_token": "ref2",
            })
            new = _refresh_token(token)
        kwargs = post.call_args.kwargs
        assert kwargs["data"] == {  # form body per RFC 6749 — not json=
            "grant_type": "refresh_token",
            "refresh_token": "ref",
            "client_id": "cid",
        }
        assert "json" not in kwargs
        assert new["access_token"] == "new"
        assert new["refresh_token"] == "ref2"  # rotation captured

    def test_refresh_failure_raises_with_reauth_instruction(self):
        token = {"access_token": "old", "refresh_token": "ref",
                 "token_url": "https://as.example/token"}
        with mock.patch("options_trader.data.mcp_provider.requests.post") as post:
            post.return_value = FakeResp(400, text="invalid_grant")
            with pytest.raises(RuntimeError, match="re-run the OAuth flow"):
                _refresh_token(token)

    def test_no_refresh_token_raises(self):
        with pytest.raises(RuntimeError, match="no refresh_token"):
            _refresh_token({"access_token": "old"})

    def test_endpoint_discovery_via_well_known(self):
        def fake_get(url, timeout):
            if url.endswith("oauth-protected-resource"):
                return FakeResp(200, json_data={
                    "authorization_servers": ["https://as.example"]})
            if url == "https://as.example/.well-known/oauth-authorization-server":
                return FakeResp(200, json_data={
                    "token_endpoint": "https://as.example/oauth/token"})
            return FakeResp(404, json_data={})

        with mock.patch("options_trader.data.mcp_provider.requests.get",
                        side_effect=fake_get):
            assert _discover_token_endpoint() == "https://as.example/oauth/token"

    def test_rotated_token_is_persisted(self, tmp_path):
        token_file = tmp_path / "tok.json"
        token_file.write_text(json.dumps({
            "access_token": "old", "refresh_token": "ref",
            "token_url": "https://as.example/token",
        }))
        p = MCPDataProvider(token_path=token_file, connect=False)
        responses = iter([
            FakeResp(401),  # tools/call rejected
            FakeResp(json_data={"jsonrpc": "2.0", "id": 2,
                                "result": {"structuredContent": {"data": {}}}}),
        ])
        with mock.patch("options_trader.data.mcp_provider.requests.post",
                        side_effect=lambda *a, **k: next(responses)), \
             mock.patch("options_trader.data.mcp_provider._refresh_token",
                        return_value={"access_token": "new",
                                      "refresh_token": "ref2",
                                      "token_url": "https://as.example/token"}), \
             mock.patch.object(MCPDataProvider, "_connect"):
            p._call("get_equity_quotes", {"symbols": ["SPY"]})
        on_disk = json.loads(token_file.read_text())
        assert on_disk["access_token"] == "new"
        assert on_disk["refresh_token"] == "ref2"


class TestCallRetries:
    def test_429_backs_off_then_succeeds(self):
        p = _provider()
        responses = iter([
            FakeResp(429, headers={"Retry-After": "0.01",
                                   "Content-Type": "application/json"},
                     json_data={}),
            FakeResp(json_data={"jsonrpc": "2.0", "id": 2,
                                "result": {"structuredContent": {"data": {"ok": 1}}}}),
        ])
        sleeps = []
        with mock.patch("options_trader.data.mcp_provider.requests.post",
                        side_effect=lambda *a, **k: next(responses)), \
             mock.patch("options_trader.data.mcp_provider.time.sleep",
                        side_effect=sleeps.append):
            msg = p._call("get_equity_quotes", {})
        assert msg["result"]["structuredContent"]["data"] == {"ok": 1}
        assert sleeps == [0.01]

    def test_jsonrpc_error_raises(self):
        p = _provider()
        resp = FakeResp(json_data={"jsonrpc": "2.0", "id": 1,
                                   "error": {"code": -32602, "message": "bad args"}})
        with mock.patch("options_trader.data.mcp_provider.requests.post",
                        return_value=resp):
            with pytest.raises(RuntimeError, match="bad args"):
                p._call("get_option_chains", {})


def test_data_package_import_stays_lazy():
    """Importing options_trader.data must not import the MCP provider (or
    requests) — yfinance users and the test suite shouldn't need it."""
    code = (
        "import sys; import options_trader.data; "
        "sys.exit(1 if 'options_trader.data.mcp_provider' in sys.modules else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT)
    assert proc.returncode == 0
