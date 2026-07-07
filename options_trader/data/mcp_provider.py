"""Robinhood MCP data provider — live option chains, quotes, and Greeks.

Talks to the Robinhood Trading MCP server over streamable HTTP with OAuth
Bearer tokens. Implements the same DataProvider interface as the yfinance
fallback, so everything downstream (filters, EV, risk, journal) is unchanged.

Design constraints this module enforces:

- **No silent truncation.** Option-instrument fetches follow pagination
  cursors, and every assembled chain is validated (both types present,
  strikes straddling spot) before it is returned. A partial chain raises
  instead of quietly producing a corrupted snapshot dataset.
- **Spec-correct OAuth refresh.** Refresh requests are form-encoded
  (RFC 6749 §6) against a token endpoint taken from the token file or
  discovered via RFC 9728/8414 well-known metadata — never assumed to be
  the retail api.robinhood.com host. Rotated refresh tokens are persisted
  back to disk. A failed refresh raises with "re-run the OAuth flow".
- **Spec-correct MCP transport.** Requests send the required
  `Accept: application/json, text/event-stream` header, the client sends
  `notifications/initialized` after `initialize`, and responses are parsed
  from either plain JSON or SSE (matched by request id). 401 triggers one
  refresh, 404 one session re-init, 429 a bounded Retry-After backoff.

Token sources, in order: an explicit `token` dict (tests), an explicit
`token_path`, `.rh_mcp_token.json` at the repo root, or Claude's cached MCP
OAuth credentials (read-only fallback).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests

from .provider import ChainSnapshot, DataProvider, CHAIN_COLUMNS

logger = logging.getLogger(__name__)

MCP_URL = "https://agent.robinhood.com/mcp/trading"
PROTOCOL_VERSION = "2025-03-26"
QUOTES_BATCH_SIZE = 20        # max instrument_ids per get_option_quotes call
MAX_INSTRUMENT_PAGES = 50     # hard stop against cursor loops
# Cursor keys checked on paginated responses (top level and under "pagination").
CURSOR_KEYS = ("next_cursor", "cursor", "next", "next_page_token")

_ACCEPT = "application/json, text/event-stream"


# ── Token loading / persistence ─────────────────────────────────────────

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_token_path() -> Path:
    return _repo_root() / ".rh_mcp_token.json"


def _load_token(token_path: Path | None = None) -> tuple[dict, Path | None]:
    """Returns (token_dict, persist_path). persist_path is None when the
    source (e.g. Claude's credential cache) must not be written back."""
    path = token_path or _default_token_path()
    if path.exists():
        return json.loads(path.read_text()), path

    claude_creds = Path.home() / ".claude" / ".credentials.json"
    if claude_creds.exists():
        creds = json.loads(claude_creds.read_text())
        for key, val in creds.get("mcpOAuth", {}).items():
            if "robinhood" in key.lower():
                return (
                    {
                        "access_token": val["accessToken"],
                        "refresh_token": val.get("refreshToken", ""),
                        "client_id": val.get("clientId", ""),
                    },
                    None,  # never write into Claude's cache
                )
    raise RuntimeError(
        f"No Robinhood MCP token found (looked for {path} and Claude's "
        "credential cache). Complete the OAuth flow and save the token as "
        "JSON with access_token/refresh_token (plus client_id and token_url "
        "if you have them)."
    )


def _persist_token(token: dict, path: Path | None) -> None:
    if path is None:
        return
    path.write_text(json.dumps(token, indent=2))
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass


# ── OAuth refresh (RFC 6749 form-encoded, endpoint discovered per RFC 9728/8414)

def _discover_token_endpoint(mcp_url: str = MCP_URL) -> str:
    parsed = urlparse(mcp_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    auth_servers = [base]
    try:
        prm = requests.get(
            f"{base}/.well-known/oauth-protected-resource", timeout=15
        )
        if prm.ok:
            listed = prm.json().get("authorization_servers") or []
            auth_servers = listed + [base]
    except requests.RequestException:
        pass

    for as_url in auth_servers:
        for well_known in ("/.well-known/oauth-authorization-server",
                           "/.well-known/openid-configuration"):
            try:
                meta = requests.get(as_url.rstrip("/") + well_known, timeout=15)
                if meta.ok:
                    endpoint = meta.json().get("token_endpoint")
                    if endpoint:
                        return endpoint
            except requests.RequestException:
                continue
    raise RuntimeError(
        "Could not discover the OAuth token endpoint from well-known "
        "metadata. Re-run the OAuth flow to get a fresh token, and store "
        "the token_endpoint as 'token_url' in the token file."
    )


def _refresh_token(token: dict) -> dict:
    """Exchange refresh_token for a new access token. Raises with a clear
    re-auth instruction on any failure — never limps along with a dead token."""
    refresh = token.get("refresh_token")
    if not refresh:
        raise RuntimeError(
            "Access token rejected (401) and no refresh_token is stored — "
            "re-run the OAuth flow."
        )
    token_url = token.get("token_url") or _discover_token_endpoint()
    form = {"grant_type": "refresh_token", "refresh_token": refresh}
    if token.get("client_id"):
        form["client_id"] = token["client_id"]
    resp = requests.post(  # form-encoded per RFC 6749 §6 — NOT json=
        token_url, data=form, headers={"Accept": "application/json"}, timeout=30
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Token refresh failed ({resp.status_code}): {resp.text[:200]} "
            "— re-run the OAuth flow."
        )
    data = resp.json()
    new = dict(token)
    new["access_token"] = data["access_token"]
    if data.get("refresh_token"):  # server may rotate the refresh token
        new["refresh_token"] = data["refresh_token"]
    new["token_url"] = token_url
    return new


# ── JSON-RPC over streamable HTTP ───────────────────────────────────────

def _read_jsonrpc(resp: requests.Response, request_id: int) -> dict | None:
    """Parse a JSON-RPC response that may be plain JSON or an SSE stream.
    For SSE, only the message matching our request id is returned."""
    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                msg = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue
            if msg.get("id") == request_id and ("result" in msg or "error" in msg):
                return msg
        return None
    if "application/json" in ctype:
        return resp.json()
    raise RuntimeError(f"Unexpected MCP response content type: {ctype!r}")


def _parse_result(response: dict) -> dict:
    """Unwrap a tools/call result: prefer structuredContent.data, fall back
    to parsing content[0].text as JSON."""
    result = response.get("result", {})
    sc = result.get("structuredContent") or {}
    if "data" in sc:
        return sc["data"]
    content = result.get("content") or []
    if not content:
        return {}
    text = content[0].get("text", "")
    if not text:
        return {}
    if text.startswith("API error"):
        raise RuntimeError(f"MCP API error: {text[:300]}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"MCP unparseable response: {text[:200]}")
    return parsed.get("data", parsed)


def _find_cursor(data: dict) -> str | None:
    """Locate a pagination cursor without assuming the server's exact schema.
    Handles bare cursors and Robinhood-style full 'next' URLs."""
    for scope in (data, data.get("pagination") or {}):
        for key in CURSOR_KEYS:
            val = scope.get(key)
            if not val or not isinstance(val, str):
                continue
            if val.startswith("http"):
                qs = parse_qs(urlparse(val).query)
                if "cursor" in qs:
                    return qs["cursor"][0]
                return val  # opaque next-URL; pass through as-is
            return val
    return None


# ── Provider ────────────────────────────────────────────────────────────

class MCPDataProvider(DataProvider):
    """Live data from the Robinhood Trading MCP server."""

    def __init__(self, token_path: str | Path | None = None, *,
                 token: dict | None = None, connect: bool = True):
        if token is not None:
            self._token, self._token_persist_path = dict(token), None
        else:
            self._token, self._token_persist_path = _load_token(
                Path(token_path) if token_path else None
            )
        self._session_id: str | None = None
        self._cid = 0
        self._chain_meta_cache: dict[str, dict] = {}
        if connect:
            try:
                self._connect()
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 401:
                    self._token = _refresh_token(self._token)
                    _persist_token(self._token, self._token_persist_path)
                    self._connect()
                else:
                    raise

    # -- transport --

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": _ACCEPT,
            "Authorization": f"Bearer {self._token['access_token']}",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _next_cid(self) -> int:
        self._cid += 1
        return self._cid

    def _connect(self) -> None:
        """MCP initialize + initialized notification."""
        rid = self._next_cid()
        self._session_id = None
        resp = requests.post(
            MCP_URL,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "options-trader", "version": "0.2"},
                },
                "id": rid,
            },
            stream=True,
            timeout=(10, 60),
        )
        resp.raise_for_status()
        self._session_id = resp.headers.get("Mcp-Session-Id")
        msg = _read_jsonrpc(resp, rid)
        if msg is not None and "error" in msg:
            raise RuntimeError(f"MCP initialize failed: {msg['error']}")
        # Required by the MCP lifecycle before normal requests.
        requests.post(
            MCP_URL,
            headers=self._headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout=(10, 30),
        )

    def _call(self, name: str, arguments: dict) -> dict:
        """tools/call with bounded retries: one 401→refresh, one 404→re-init,
        Retry-After honoring backoff on 429."""
        refreshed = reinitialized = False
        for attempt in range(5):
            rid = self._next_cid()
            resp = requests.post(
                MCP_URL,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                    "id": rid,
                },
                stream=True,
                timeout=(10, 60),
            )
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                logger.info("MCP 401 — refreshing OAuth token")
                self._token = _refresh_token(self._token)
                _persist_token(self._token, self._token_persist_path)
                self._connect()
                continue
            if resp.status_code == 404 and not reinitialized:
                reinitialized = True
                logger.info("MCP 404 — session expired, re-initializing")
                self._connect()
                continue
            if resp.status_code == 429:
                wait = min(float(resp.headers.get("Retry-After", 2 * (attempt + 1))), 30.0)
                logger.info("MCP 429 — backing off %.1fs", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            msg = _read_jsonrpc(resp, rid)
            if msg is None:
                raise RuntimeError(f"MCP call {name}: no JSON-RPC response for id {rid}")
            if "error" in msg:
                raise RuntimeError(f"MCP error from {name}: {msg['error']}")
            return msg
        raise RuntimeError(f"MCP call {name}: retries exhausted")

    # -- chain metadata --

    def _chain_meta(self, underlying: str) -> dict:
        """Standard chain for the underlying — matched by symbol, never
        blindly chains[0] (adjusted/non-standard chains exist after
        corporate actions)."""
        if underlying not in self._chain_meta_cache:
            data = _parse_result(
                self._call("get_option_chains", {"underlying_symbol": underlying})
            )
            chains = data.get("chains", [])
            if not chains:
                raise RuntimeError(f"No option chains for {underlying}")
            matches = [
                c for c in chains
                if str(c.get("symbol", "")).upper() == underlying.upper()
            ]
            if not matches:
                logger.warning(
                    "%s: no chain with matching symbol among %d chains; "
                    "using the first — verify it is the standard chain",
                    underlying, len(chains),
                )
            self._chain_meta_cache[underlying] = (matches or chains)[0]
        return self._chain_meta_cache[underlying]

    def _fetch_all_instruments(self, chain_id: str, expiration: str) -> list[dict]:
        """Follow pagination cursors until the instrument list is exhausted."""
        base_args = {
            "chain_id": chain_id,
            "expiration_dates": expiration,
            "state": "active",
        }
        instruments: list[dict] = []
        cursor: str | None = None
        saw_cursor = False
        for _ in range(MAX_INSTRUMENT_PAGES):
            args = dict(base_args)
            if cursor:
                args["cursor"] = cursor
            data = _parse_result(self._call("get_option_instruments", args))
            page = data.get("instruments") or data.get("results") or []
            instruments.extend(page)
            cursor = _find_cursor(data)
            saw_cursor = saw_cursor or cursor is not None
            if not cursor or not page:
                break
        else:
            raise RuntimeError(
                f"Instrument pagination did not terminate within "
                f"{MAX_INSTRUMENT_PAGES} pages for chain {chain_id} {expiration}"
            )
        if not saw_cursor and instruments and len(instruments) % 100 == 0:
            logger.warning(
                "Instrument count (%d) is an exact page-size multiple and no "
                "pagination cursor was seen — the server may paginate with a "
                "schema this client doesn't recognize. Inspect a raw "
                "get_option_instruments response.", len(instruments),
            )
        return instruments

    @staticmethod
    def _validate_chain(chain: pd.DataFrame, underlying: str,
                        expiration: str, spot: float) -> None:
        """Refuse to return a chain that looks truncated. A silently partial
        chain corrupts scans AND the snapshot dataset used for backtests."""
        types = set(chain["type"].unique())
        problems = []
        if not {"call", "put"} <= types:
            problems.append(f"only {sorted(types)} present (need calls and puts)")
        if not chain.empty:
            lo, hi = chain["strike"].min(), chain["strike"].max()
            if not (lo < spot < hi):
                problems.append(
                    f"strikes [{lo:g}, {hi:g}] do not straddle spot {spot:.2f}"
                )
        if problems:
            raise RuntimeError(
                f"{underlying} {expiration}: chain looks incomplete — "
                + "; ".join(problems)
                + ". Refusing to use a possibly-truncated chain."
            )

    # ── DataProvider interface ──────────────────────────────────────────

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
        return list(self._chain_meta(underlying).get("expiration_dates", []))

    def get_chain(self, underlying: str, expiration: str) -> ChainSnapshot:
        spot = self.get_spot(underlying)
        chain_id = self._chain_meta(underlying)["id"]
        instruments = self._fetch_all_instruments(chain_id, expiration)
        if not instruments:
            raise RuntimeError(f"No instruments for {underlying} {expiration}")

        quote_by_id: dict[str, dict] = {}
        for i in range(0, len(instruments), QUOTES_BATCH_SIZE):
            chunk = instruments[i:i + QUOTES_BATCH_SIZE]
            qdata = _parse_result(
                self._call(
                    "get_option_quotes",
                    {"instrument_ids": [inst["id"] for inst in chunk]},
                )
            )
            for q in qdata.get("results", []):
                qq = q.get("quote", q)
                if qq.get("instrument_id"):
                    quote_by_id[qq["instrument_id"]] = qq

        missing = sum(1 for inst in instruments if inst["id"] not in quote_by_id)
        if missing:
            logger.warning(
                "%s %s: %d/%d instruments returned no quote",
                underlying, expiration, missing, len(instruments),
            )

        rows = []
        for inst in instruments:
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
        self._validate_chain(chain, underlying, expiration, spot)
        return ChainSnapshot(
            underlying=underlying,
            spot=spot,
            expiration=expiration,
            taken_at=datetime.now().isoformat(timespec="seconds"),
            chain=chain,
        )
