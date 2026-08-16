"""Unusual Whales historical EOD option-chain importer.

Source: `/api/stock/{ticker}/option-chains?date=YYYY-MM-DD&greeks=true`
(https://api.unusualwhales.com/docs), which returns every option contract
live on that ticker on that day in a single response: strike, expiry,
type, NBBO bid/ask, implied_volatility, open_interest, volume, and full
greeks (delta/gamma/theta/vega/rho) — no per-contract or per-expiry calls
needed, unlike EODHD (see eodhd.py). This is the richest of the three
historical sources this repo supports.

Trial/plan limitation: free-trial keys get a **rolling window** of
historical access (observed as 90 trading days back from today) on this
endpoint. Querying a date outside that window returns HTTP 403 with
`{"code": "historic_data_access_missing", ...}` — `UWClient` turns that
into a `HistoricalAccessError` so the importer can skip the day and keep
going instead of aborting the whole run.

No underlying spot price is returned by this endpoint (only per-contract
strike/NBBO), so spot is joined the same way as the other two importers:
daily closes from yfinance via `build_spot_lookup` (reused from dolthub.py).

Imported snapshots write to their own SnapshotStore root (default
`data_snapshots_uw/`), kept separate from live collection and from the
DoltHub/EODHD imports.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date as date_cls

import pandas as pd
import requests

from .provider import ChainSnapshot, CHAIN_COLUMNS
from .dolthub import build_spot_lookup  # noqa: F401  (re-exported for callers)

logger = logging.getLogger(__name__)

API_BASE = "https://api.unusualwhales.com"
REQUEST_PAUSE_S = 0.25
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HistoricalAccessError(RuntimeError):
    """Raised when the queried date is outside the API key's historical
    access window (free-trial keys get a rolling ~90-trading-day lookback)."""


def _check_symbol(symbol: str) -> str:
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"Suspicious symbol {symbol!r}")
    return symbol


def _check_date(d: str) -> str:
    if not _DATE_RE.match(d):
        raise ValueError(f"Dates must be YYYY-MM-DD, got {d!r}")
    return d


class UWClient:
    """Minimal read-only client for Unusual Whales' option-chains endpoint.
    Auth is a bearer token, not a query param (unlike DoltHub/EODHD)."""

    def __init__(self, api_key: str, pause_s: float = REQUEST_PAUSE_S):
        if not api_key:
            raise ValueError("UW_API_KEY is required")
        self.api_key = api_key
        self.pause_s = pause_s

    def option_chains(self, symbol: str, day: str | None = None,
                       greeks: bool = True) -> list[dict]:
        params: dict[str, str] = {"greeks": "true" if greeks else "false"}
        if day:
            params["date"] = day
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Accept": "application/json"}
        for attempt in range(4):
            resp = requests.get(
                f"{API_BASE}/api/stock/{symbol}/option-chains",
                headers=headers, params=params, timeout=60,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2.0 * (attempt + 1)
                logger.info("UW %s — retrying in %.0fs", resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 403:
                body = {}
                try:
                    body = resp.json()
                except ValueError:
                    pass
                if body.get("code") == "historic_data_access_missing":
                    raise HistoricalAccessError(body.get("message", "").strip())
                raise RuntimeError(
                    f"UW 403 Forbidden: {body.get('message', resp.text[:300])}"
                )
            resp.raise_for_status()
            return resp.json().get("data", [])
        raise RuntimeError("UW API: retries exhausted")


class UWImporter:
    def __init__(self, api_key: str, client: UWClient | None = None):
        self.client = client or UWClient(api_key)

    def fetch_day(self, symbol: str, day: str, max_dte: int = 10) -> list[dict]:
        """All chain rows for one symbol/scan-day, expirations within max_dte.
        Raises HistoricalAccessError if `day` is outside the key's
        historical access window."""
        symbol, day = _check_symbol(symbol), _check_date(day)
        d = date_cls.fromisoformat(day)
        exp_max = date_cls.fromordinal(d.toordinal() + max_dte).isoformat()
        rows = self.client.option_chains(symbol, day=day, greeks=True)
        return [r for r in rows if day <= str(r["expires"])[:10] <= exp_max]


def rows_to_snapshots(day: str, symbol: str, rows: list[dict],
                      spot_lookup: dict[tuple[str, str], float]) -> list[ChainSnapshot]:
    """Group one day's UW option-chains rows into one ChainSnapshot per
    expiration. Rows for a day with no spot available are skipped with a
    warning — a snapshot without spot is unusable.

    volume/open_interest/iv/delta come straight from the dataset (real
    values, richest of the three importers) so the strategy's normal
    liquidity filters apply without needing a zeroed override config.
    """
    spot = spot_lookup.get((symbol, day))
    if not spot:
        logger.warning("%s %s: no spot close available — day skipped", symbol, day)
        return []

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(str(r["expires"])[:10], []).append(r)

    snapshots: list[ChainSnapshot] = []
    for expiration, chunk in sorted(grouped.items()):
        records = []
        for r in chunk:
            records.append({
                "type": str(r["option_type"]).strip().lower(),
                "strike": float(r["strike"]),
                "bid": float(r["nbbo_bid"] or 0),
                "ask": float(r["nbbo_ask"] or 0),
                "volume": int(r["volume"] or 0),
                "open_interest": int(r["open_interest"] or 0),
                "iv": float(r["implied_volatility"] or 0),
            })
        chain = pd.DataFrame(records, columns=CHAIN_COLUMNS)
        snapshots.append(ChainSnapshot(
            underlying=symbol,
            spot=float(spot),
            expiration=expiration,
            taken_at=f"{day}T16:00:00",   # EOD data
            chain=chain,
        ))
    return snapshots
