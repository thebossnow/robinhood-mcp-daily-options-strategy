"""Robinhood MCP data provider — live option chains, quotes, and Greeks.

Replaces yfinance as the primary data source. Talks to the Robinhood
Trading MCP server (https://agent.robinhood.com/mcp/trading) via HTTP
with OAuth Bearer tokens refreshed automatically.

Token persistence: reads from .rh_mcp_token.json (fresh OAuth) or
~/.claude/.credentials.json (Claude-cached MCP OAuth) as fallback.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from .provider import ChainSnapshot, DataProvider, CHAIN_COLUMNS

logger = logging.getLogger(__name__)

MCP_URL = "https://agent.robinhood.com/mcp/trading"
TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
QUOTES_BATCH_SIZE = 20       # max instrument_ids per get_option_quotes call


def _load_token() -> dict[str, str]:
    """Load token from repo-local file or Claude's credential cache."""
    # 1. Repo-local fresh OAuth token
    local = Path(".rh_mcp_token.json")
    if local.exists():
        data = json.loads(local.read_text())
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
        }

    # 2. Claude's cached MCP OAuth token
    claude_creds = Path.home() / ".claude" / ".credentials.json"
    if claude_creds.exists():
        creds = json.loads(claude_creds.read_text())
        mcp = creds.get("mcpOAuth", {})
        for key, val in mcp.items():
            if "robinhood" in key:
                return {
                    "access_token": val["accessToken"],
                    "refresh_token": val.get("refreshToken", ""),
                }
    raise RuntimeError(
        "No Robinhood MCP token found. Run OAuth flow or "
        "place .rh_mcp_token.json in the project root."
    )


def _refresh_token(refresh_token: str) -> dict[str, str]:
    """Exchange a refresh token for a new access token."""
    resp = requests.post(
        TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
    }


def _mcp_initialize(token: str) -> str:
    """Initialize an MCP session and return the session ID."""
    resp = requests.post(
        MCP_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hermes-options-trader", "version": "1.0"},
            },
            "id": 0,
        },
        stream=True,
        timeout=30,
    )
    sid = resp.headers.get("Mcp-Session-Id")
    if not sid:
        raise RuntimeError("MCP initialize: no session ID returned")
    # Drain the SSE response
    for _ in resp.iter_lines(decode_unicode=True):
        pass
    return sid


def _call_mcp(token: str, session_id: str, name: str,
              arguments: dict, cid: int = 1) -> dict:
    """Call an MCP tool with a standalone HTTP request."""
    resp = requests.post(
        MCP_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Mcp-Session-Id": session_id,
        },
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": cid,
        },
        stream=True,
        timeout=30,
    )
    resp.raise_for_status()

    result: dict | None = None
    for line in resp.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            data = json.loads(line[6:])
            if "error" in data:
                result = data
            elif "result" in data and result is None:
                result = data

    if result is None:
        raise RuntimeError(f"MCP call {name}: no result in SSE")
    if "error" in result:
        raise RuntimeError(f"MCP error from {name}: {result['error']}")
    return result


def _parse_result(response: dict) -> dict:
    """Unwrap MCP response: structuredContent.data (pre-parsed) or content[0].text → JSON → data."""
    # Prefer structuredContent — already parsed by the MCP server
    sc = response.get("result", {}).get("structuredContent", {})
    if sc and "data" in sc:
        return sc["data"]

    # Fallback: parse the text content, or detect API errors
    content = response["result"]["content"]
    if not content:
        return {}
    text = content[0].get("text", "")
    if not text:
        return {}
    if text.startswith("API error"):
        raise RuntimeError(f"MCP API error: {text}")
    try:
        return json.loads(text).get("data", {})
    except json.JSONDecodeError:
        raise RuntimeError(f"MCP unparseable response: {text[:200]}")


class MCPDataProvider(DataProvider):
    """Live data from Robinhood Trading MCP.

    Token is auto-refreshed on 401. Each MCP call uses a standalone
    HTTP request to avoid SSE-over-HTTP/2 connection reuse issues.
    """

    def __init__(self, token_path: str | Path | None = None):
        self._token_path = Path(token_path) if token_path else None
        self._token = (
            json.loads(Path(self._token_path).read_text())
            if self._token_path
            else _load_token()
        )
        self._session_id = _mcp_initialize(self._token["access_token"])
        self._cid = 1

    def _next_cid(self) -> int:
        cid = self._cid
        self._cid += 1
        return cid

    def _call(self, name: str, arguments: dict) -> dict:
        """Call an MCP tool, with one retry on 401."""
        for attempt in range(2):
            try:
                return _call_mcp(
                    self._token["access_token"],
                    self._session_id,
                    name,
                    arguments,
                    self._next_cid(),
                )
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 401 and attempt == 0:
                    logger.info("Token expired — refreshing")
                    self._token = _refresh_token(self._token["refresh_token"])
                    self._session_id = _mcp_initialize(self._token["access_token"])
                    continue
                raise
        raise RuntimeError(f"MCP call {name}: unreachable")  # pragma: no cover

    # ── DataProvider interface ──────────────────────────────────────

    def get_spot(self, underlying: str) -> float:
        data = _parse_result(
            self._call("get_equity_quotes", {"symbols": [underlying]})
        )
        results = data.get("results", [])
        if not results:
            raise RuntimeError(f"No quote for {underlying}")
        q = results[0].get("quote", results[0])
        price = q.get("last_trade_price")
        if price is None:
            raise RuntimeError(f"No last_trade_price for {underlying}")
        return float(price)

    def get_expirations(self, underlying: str) -> list[str]:
        data = _parse_result(
            self._call("get_option_chains", {"underlying_symbol": underlying})
        )
        chains = data.get("chains", [])
        if not chains:
            return []
        return chains[0].get("expiration_dates", [])

    def get_chain(self, underlying: str, expiration: str) -> ChainSnapshot:
        """Fetch full chain for one underlying/expiration via MCP."""
        spot = self.get_spot(underlying)

        # Get chain ID
        chain_data = _parse_result(
            self._call("get_option_chains", {"underlying_symbol": underlying})
        )
        chains = chain_data.get("chains", [])
        if not chains:
            raise RuntimeError(f"No chains for {underlying}")
        chain_id = chains[0]["id"]

        # Get all instruments for this expiration (single page — 100 is plenty)
        data = _parse_result(
            self._call("get_option_instruments", {
                "chain_id": chain_id,
                "expiration_dates": expiration,
                "state": "active",
            })
        )
        all_instruments: list[dict] = data.get("instruments", [])

        if not all_instruments:
            raise RuntimeError(f"No instruments for {underlying} {expiration}")

        # Batch-fetch quotes
        quote_by_id: dict[str, dict] = {}
        for i in range(0, len(all_instruments), QUOTES_BATCH_SIZE):
            chunk = all_instruments[i : i + QUOTES_BATCH_SIZE]
            ids = [inst["id"] for inst in chunk]
            qdata = _parse_result(
                self._call("get_option_quotes", {"instrument_ids": ids})
            )
            for q in qdata.get("results", []):
                qq = q.get("quote", {})
                if qq.get("instrument_id"):
                    quote_by_id[qq["instrument_id"]] = qq

        # Normalize to DataFrame
        rows = []
        for inst in all_instruments:
            q = quote_by_id.get(inst["id"], {})
            rows.append({
                "type": inst["type"],
                "strike": float(inst["strike_price"]),
                "bid": float(q.get("bid_price", 0) or 0),
                "ask": float(q.get("ask_price", 0) or 0),
                "volume": int(q.get("volume", 0) or 0),
                "open_interest": int(q.get("open_interest", 0) or 0),
                "iv": float(q.get("implied_volatility", 0) or 0),
            })

        chain = pd.DataFrame(rows, columns=CHAIN_COLUMNS)
        return ChainSnapshot(
            underlying=underlying,
            spot=spot,
            expiration=expiration,
            taken_at=datetime.now().isoformat(timespec="seconds"),
            chain=chain,
        )
