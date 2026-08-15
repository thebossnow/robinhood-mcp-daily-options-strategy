"""EODHD data provider — live option chains and historical EOD chains.

Source: https://eodhd.com. Two distinct products are used here:

- **Base plan** (`/api/real-time`, `/api/eod`) — underlying spot and daily
  closes. Available on every plan including the free tier.
- **UnicornBay options marketplace add-on** (`/api/mp/unicornbay/options/*`)
  — option contracts and per-day EOD option prices. This is a SEPARATE PAID
  SKU: a key without it gets a 402/403 from those paths, which this module
  surfaces as a clear `EODHDSubscriptionError` rather than an empty chain.

Why this provider exists: the DoltHub historical importer (`dolthub.py`) has
no volume or open-interest columns, so backtests over it must zero those
liquidity minimums and are optimistic on fillability. UnicornBay's chains
carry `volume` and `openInterest` per contract, so the SAME liquidity filter
the live scanner applies can run over history. That is the gap this closes.

**API call budget.** EODHD bills "API calls" per request, and the options
endpoints cost **10 calls each** (base EOD/real-time cost 1). The free tier
allows 20 calls/day — i.e. two options requests. This module is written to
be frugal accordingly: `get_expirations` fetches every contract in the DTE
window in ONE paginated request and caches it, so a scan over N expirations
costs one options request per underlying, not N.

Request shape: the API is JSON:API-flavoured — filters are `filter[name]=v`
and pagination is `page[limit]` / `page[offset]`. (A flat `underlying_symbol=`
is rejected with "The filter.underlying_symbol field is required".) Data
items are unwrapped from `attributes` when present so both the flat and
JSON:API response encodings work.

Auth: set `EODHD_API_KEY` in the environment. Never hard-code a token.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, date as date_cls

import pandas as pd
import requests

from .provider import ChainSnapshot, DataProvider, CHAIN_COLUMNS

logger = logging.getLogger(__name__)

BASE_URL = "https://eodhd.com/api"
CONTRACTS_PATH = "mp/unicornbay/options/contracts"
OPTIONS_EOD_PATH = "mp/unicornbay/options/eod"

PAGE_LIMIT = 1000        # API maximum per page
MAX_PAGES = 50           # hard stop against cursor/offset loops
MAX_OFFSET = 10_000      # API maximum offset
REQUEST_PAUSE_S = 0.2
OPTIONS_CALL_COST = 10   # API-call units billed per options request

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# OCC 21-char option symbol: root(≤6) + YYMMDD + C/P + strike*1000 (8 digits)
_OCC_RE = re.compile(r"^(?P<root>[A-Z0-9.\-]{1,6})(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


class EODHDError(RuntimeError):
    """Any EODHD API failure."""


class EODHDSubscriptionError(EODHDError):
    """The key is valid but the plan lacks this endpoint (options add-on)."""


def _check_symbol(symbol: str) -> str:
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"Suspicious symbol {symbol!r}")
    return symbol


def _check_date(d: str) -> str:
    if not _DATE_RE.match(d):
        raise ValueError(f"Dates must be YYYY-MM-DD, got {d!r}")
    return d


def _norm_type(value: str) -> str:
    """Normalize a contract type to 'call' / 'put'."""
    v = str(value).strip().lower()
    if v.startswith("c"):
        return "call"
    if v.startswith("p"):
        return "put"
    raise ValueError(f"Unrecognized option type {value!r}")


def parse_occ(contract: str) -> dict | None:
    """Decode an OCC contract name (e.g. 'SPY260814C00500000').

    Used only as a fallback when a response row omits type/strike/expiration.
    Returns None when the name isn't OCC-shaped — callers then skip the row
    rather than guessing.
    """
    m = _OCC_RE.match(str(contract).strip().upper())
    if not m:
        return None
    ymd = m.group("ymd")
    try:
        exp = date_cls(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])).isoformat()
    except ValueError:
        return None
    return {
        "underlying": m.group("root"),
        "expiration": exp,
        "type": "call" if m.group("cp") == "C" else "put",
        "strike": int(m.group("strike")) / 1000.0,
    }


def _unwrap(item: dict) -> dict:
    """JSON:API resource objects nest fields under 'attributes'; flat
    encodings put them at the top level. Accept either."""
    attrs = item.get("attributes")
    if isinstance(attrs, dict):
        merged = dict(attrs)
        # Keep an id/contract name if it only exists on the envelope.
        for key in ("id", "contractName", "contract"):
            if key in item and key not in merged:
                merged[key] = item[key]
        return merged
    return item


def _f(row: dict, *names: str, default: float = 0.0) -> float:
    """First present, non-null, numeric value among `names`."""
    for n in names:
        v = row.get(n)
        if v is None or v == "" or v == "NA":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return default


def _i(row: dict, *names: str, default: int = 0) -> int:
    return int(_f(row, *names, default=float(default)))


def _s(row: dict, *names: str) -> str | None:
    for n in names:
        v = row.get(n)
        if v not in (None, "", "NA"):
            return str(v)
    return None


class EODHDClient:
    """Read-only HTTP client with JSON:API params, pagination and backoff."""

    def __init__(self, api_key: str | None = None, *,
                 base_url: str = BASE_URL, pause_s: float = REQUEST_PAUSE_S,
                 timeout: int = 60):
        key = api_key or os.environ.get("EODHD_API_KEY")
        if not key:
            raise EODHDError(
                "No EODHD API key. Set EODHD_API_KEY in the environment "
                "(or pass api_key=). Get one at https://eodhd.com."
            )
        self.api_key = key
        self.base_url = base_url.rstrip("/")
        self.pause_s = pause_s
        self.timeout = timeout
        self.calls_billed = 0     # running estimate of API-call units spent

    def _request(self, path: str, params: dict, cost: int = 1) -> dict:
        query = {**params, "api_token": self.api_key, "fmt": "json"}
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(4):
            resp = requests.get(url, params=query, timeout=self.timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = min(2.0 * (attempt + 1), 15.0)
                logger.info("EODHD %s on %s — retrying in %.0fs",
                            resp.status_code, path, wait)
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403):
                raise EODHDSubscriptionError(
                    f"EODHD rejected the key for {path} ({resp.status_code}). "
                    "Either the token is wrong or your plan does not include "
                    "this endpoint."
                )
            if resp.status_code == 402:
                raise EODHDSubscriptionError(
                    f"EODHD {path} requires a paid add-on your plan lacks "
                    "(402). The options endpoints are the UnicornBay "
                    "marketplace SKU, sold separately from the base plan."
                )
            self.calls_billed += cost
            if resp.status_code >= 400:
                raise EODHDError(
                    f"EODHD {path} failed ({resp.status_code}): {resp.text[:300]}"
                )
            try:
                body = resp.json()
            except ValueError:
                raise EODHDError(
                    f"EODHD {path}: non-JSON response: {resp.text[:200]}"
                )
            # Validation failures come back as {"errors": {field: [msg, ...]}}
            # and can carry a 200, so check the body shape, not just the code.
            if isinstance(body, dict) and body.get("errors"):
                raise EODHDError(
                    f"EODHD {path} rejected the request: {body['errors']}"
                )
            return body
        raise EODHDError(f"EODHD {path}: retries exhausted")

    def get(self, path: str, params: dict | None = None, cost: int = 1):
        return self._request(path, params or {}, cost=cost)

    def get_paged(self, path: str, filters: dict,
                  cost: int = OPTIONS_CALL_COST) -> list[dict]:
        """Drain a JSON:API collection. `filters` are plain names; they are
        wrapped as filter[name] here so callers never hand-encode brackets."""
        params = {f"filter[{k}]": v for k, v in filters.items() if v is not None}
        rows: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            page_params = {
                **params,
                "page[limit]": PAGE_LIMIT,
                "page[offset]": offset,
            }
            body = self.get(path, page_params, cost=cost)
            data = body.get("data") if isinstance(body, dict) else body
            page = [_unwrap(item) for item in (data or []) if isinstance(item, dict)]
            rows.extend(page)
            if len(page) < PAGE_LIMIT:
                return rows
            offset += PAGE_LIMIT
            if offset > MAX_OFFSET:
                logger.warning(
                    "%s: hit the API's %d-row offset ceiling for %s — results "
                    "are truncated; narrow the filters.", path, MAX_OFFSET, filters
                )
                return rows
            time.sleep(self.pause_s)
        raise EODHDError(f"EODHD {path}: pagination did not terminate")

    # -- base-plan endpoints (1 call each) --

    def real_time(self, symbol: str, exchange: str = "US") -> dict:
        body = self.get(f"real-time/{_check_symbol(symbol)}.{exchange}")
        return body if isinstance(body, dict) else {}

    def eod(self, symbol: str, exchange: str = "US", *,
            start: str | None = None, end: str | None = None) -> list[dict]:
        params = {"period": "d"}
        if start:
            params["from"] = _check_date(start)
        if end:
            params["to"] = _check_date(end)
        body = self.get(f"eod/{_check_symbol(symbol)}.{exchange}", params)
        return body if isinstance(body, list) else []


def _chain_rows(rows: list[dict]) -> list[dict]:
    """Normalize UnicornBay contract rows to CHAIN_COLUMNS.

    Unlike the DoltHub importer, volume and open interest are REAL here —
    the strict liquidity filter can run unmodified over this data.
    """
    out = []
    for r in rows:
        occ = parse_occ(_s(r, "contractName", "contract", "id") or "") or {}
        raw_type = _s(r, "type", "optionType", "callPut")
        try:
            opt_type = _norm_type(raw_type) if raw_type else occ["type"]
        except (ValueError, KeyError):
            logger.debug("skipping row with undecodable type: %.120s", r)
            continue
        strike = _f(r, "strike", "strikePrice", default=occ.get("strike", 0.0))
        if strike <= 0:
            continue
        out.append({
            "type": opt_type,
            "strike": strike,
            "bid": _f(r, "bid", "bidPrice"),
            "ask": _f(r, "ask", "askPrice"),
            "volume": _i(r, "volume"),
            "open_interest": _i(r, "openInterest", "open_interest"),
            "iv": _f(r, "impliedVolatility", "implied_volatility", "iv"),
        })
    return out


def _validate_chain(chain: pd.DataFrame, underlying: str,
                    expiration: str, spot: float) -> None:
    """Refuse a chain that looks truncated — a silently partial chain
    corrupts both the scan and the snapshot dataset behind backtests."""
    problems = []
    types = set(chain["type"].unique()) if not chain.empty else set()
    if not {"call", "put"} <= types:
        problems.append(f"only {sorted(types)} present (need calls and puts)")
    if not chain.empty:
        lo, hi = chain["strike"].min(), chain["strike"].max()
        if not (lo < spot < hi):
            problems.append(
                f"strikes [{lo:g}, {hi:g}] do not straddle spot {spot:.2f}"
            )
    if problems:
        raise EODHDError(
            f"{underlying} {expiration}: chain looks incomplete — "
            + "; ".join(problems)
            + ". Refusing to use a possibly-truncated chain."
        )


class EODHDProvider(DataProvider):
    """Current option chains from the UnicornBay contracts endpoint.

    Quotes are the vendor's latest snapshot, not a live streaming book —
    fine for scanning and paper trading, NOT a live-execution price source.

    One options request covers the whole DTE window per underlying (see the
    module docstring on call cost); per-expiration `get_chain` calls are then
    served from that cache.
    """

    def __init__(self, client: EODHDClient | None = None, *,
                 api_key: str | None = None, exchange: str = "US",
                 min_dte: int = 0, max_dte: int = 60):
        self.client = client or EODHDClient(api_key=api_key)
        self.exchange = exchange
        self.min_dte = min_dte
        self.max_dte = max_dte
        self._window_cache: dict[str, dict[str, list[dict]]] = {}

    # -- window fetch --

    def _window(self, underlying: str, *, refresh: bool = False
                ) -> dict[str, list[dict]]:
        """All contracts in the DTE window, grouped by expiration. Cached."""
        underlying = _check_symbol(underlying)
        if refresh or underlying not in self._window_cache:
            today = date_cls.today()
            lo = date_cls.fromordinal(today.toordinal() + self.min_dte)
            hi = date_cls.fromordinal(today.toordinal() + self.max_dte)
            rows = self.client.get_paged(CONTRACTS_PATH, {
                "underlying_symbol": underlying,
                "exp_date_from": lo.isoformat(),
                "exp_date_to": hi.isoformat(),
            })
            grouped: dict[str, list[dict]] = {}
            for r in rows:
                occ = parse_occ(_s(r, "contractName", "contract", "id") or "") or {}
                exp = _s(r, "expirationDate", "exp_date", "expiration") or occ.get("expiration")
                if not exp:
                    continue
                grouped.setdefault(str(exp)[:10], []).append(r)
            if not grouped:
                raise EODHDError(
                    f"No option contracts returned for {underlying} between "
                    f"{lo} and {hi}. Check the symbol and that your plan "
                    "includes the UnicornBay options add-on."
                )
            self._window_cache[underlying] = grouped
        return self._window_cache[underlying]

    # -- DataProvider interface --

    def get_spot(self, underlying: str) -> float:
        underlying = _check_symbol(underlying)
        quote = self.client.real_time(underlying, self.exchange)
        price = _f(quote, "close", "last", "previousClose", default=0.0)
        if price > 0:
            return price
        # Real-time can return "NA" outside market hours on some plans.
        bars = self.client.eod(underlying, self.exchange)
        if bars:
            close = _f(bars[-1], "close", default=0.0)
            if close > 0:
                logger.info("%s: real-time unavailable, using last EOD close",
                            underlying)
                return close
        raise EODHDError(f"No spot price available for {underlying}")

    def get_expirations(self, underlying: str) -> list[str]:
        return sorted(self._window(underlying).keys())

    def get_chain(self, underlying: str, expiration: str) -> ChainSnapshot:
        underlying = _check_symbol(underlying)
        expiration = _check_date(expiration)
        window = self._window(underlying)
        if expiration not in window:
            raise EODHDError(
                f"{underlying}: no contracts for {expiration} in the cached "
                f"DTE window ({self.min_dte}-{self.max_dte} days). "
                f"Available: {sorted(window)[:8]}"
            )
        spot = self.get_spot(underlying)
        chain = pd.DataFrame(_chain_rows(window[expiration]), columns=CHAIN_COLUMNS)
        _validate_chain(chain, underlying, expiration, spot)
        return ChainSnapshot(
            underlying=underlying,
            spot=spot,
            expiration=expiration,
            taken_at=datetime.now().isoformat(timespec="seconds"),
            chain=chain,
        )


class EODHDHistory:
    """Historical EOD option chains for backtesting.

    Unlike DoltHub, rows carry real volume and open interest, so backtests
    can run the production liquidity filter instead of a relaxed one.

    Still EOD-only: one snapshot per trading day, stamped 16:00, so intraday
    management cannot be evaluated — matching the engine's hold-to-expiry
    assumption. Underlying spot is joined from EODHD's own daily closes.
    """

    def __init__(self, client: EODHDClient | None = None, *,
                 api_key: str | None = None, exchange: str = "US"):
        self.client = client or EODHDClient(api_key=api_key)
        self.exchange = exchange

    def spot_lookup(self, symbols: list[str], start: str, end: str
                    ) -> dict[tuple[str, str], float]:
        """Daily closes keyed by (symbol, YYYY-MM-DD)."""
        lookup: dict[tuple[str, str], float] = {}
        for symbol in symbols:
            for bar in self.client.eod(symbol, self.exchange,
                                       start=_check_date(start),
                                       end=_check_date(end)):
                day = _s(bar, "date")
                close = _f(bar, "close", default=0.0)
                if day and close > 0:
                    lookup[(symbol, day[:10])] = close
        return lookup

    def fetch_eod(self, symbol: str, start: str, end: str, *,
                  max_dte: int | None = None) -> list[dict]:
        """Raw per-contract-per-day EOD rows for a symbol over a date range."""
        symbol = _check_symbol(symbol)
        filters = {
            "underlying_symbol": symbol,
            "tradetime_from": _check_date(start),
            "tradetime_to": _check_date(end),
        }
        if max_dte is not None:
            end_d = date_cls.fromisoformat(end)
            filters["exp_date_from"] = start
            filters["exp_date_to"] = date_cls.fromordinal(
                end_d.toordinal() + max_dte
            ).isoformat()
        return self.client.get_paged(OPTIONS_EOD_PATH, filters)

    def snapshots(self, symbol: str, start: str, end: str, *,
                  max_dte: int | None = None,
                  spot_lookup: dict[tuple[str, str], float] | None = None
                  ) -> list[ChainSnapshot]:
        """Fetch and group into one ChainSnapshot per (trade day, expiration).

        Days with no underlying close available are skipped with a warning —
        a snapshot without spot is unusable downstream.
        """
        rows = self.fetch_eod(symbol, start, end, max_dte=max_dte)
        spots = spot_lookup if spot_lookup is not None else \
            self.spot_lookup([symbol], start, end)
        return rows_to_snapshots(rows, spots, default_symbol=symbol)


def rows_to_snapshots(rows: list[dict],
                      spot_lookup: dict[tuple[str, str], float],
                      *, default_symbol: str | None = None
                      ) -> list[ChainSnapshot]:
    """Group UnicornBay EOD option rows into ChainSnapshots.

    volume/open_interest come straight from the vendor — they are NOT
    zero-filled the way the DoltHub importer must do.
    """
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    undated = 0
    for r in rows:
        occ = parse_occ(_s(r, "contractName", "contract", "id") or "") or {}
        symbol = (_s(r, "underlying_symbol", "underlyingSymbol", "symbol")
                  or occ.get("underlying") or default_symbol)
        day = _s(r, "tradetime", "trade_date", "date")
        exp = _s(r, "expirationDate", "exp_date", "expiration") or occ.get("expiration")
        if not (symbol and day and exp):
            undated += 1
            continue
        grouped.setdefault((str(day)[:10], str(symbol), str(exp)[:10]), []).append(r)
    if undated:
        logger.warning("%d rows lacked symbol/date/expiration and were skipped",
                       undated)

    snapshots: list[ChainSnapshot] = []
    skipped_no_spot: set[tuple[str, str]] = set()
    for (day, symbol, expiration), chunk in sorted(grouped.items()):
        spot = spot_lookup.get((symbol, day))
        if not spot:
            skipped_no_spot.add((symbol, day))
            continue
        chain = pd.DataFrame(_chain_rows(chunk), columns=CHAIN_COLUMNS)
        if chain.empty:
            continue
        snapshots.append(ChainSnapshot(
            underlying=symbol,
            spot=float(spot),
            expiration=expiration,
            taken_at=f"{day}T16:00:00",   # EOD data
            chain=chain,
        ))
    for symbol, day in sorted(skipped_no_spot):
        logger.warning("%s %s: no spot close available — day skipped", symbol, day)
    return snapshots
