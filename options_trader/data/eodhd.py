"""EODHD historical EOD option-chain importer.

Source: EODHD's marketplace `/mp/unicornbay/options/eod` endpoint
(https://eodhd.com/financial-apis/stock-options-data), which returns
end-of-day options records per contract: bid, ask, volume, open_interest,
implied volatility, and greeks. Requires an EODHD API key with the
"US Stock Options" marketplace add-on enabled (the base/free plan gets a
403 on this endpoint — the DoltHub importer remains the free fallback,
see dolthub.py) and costs 10 API calls per request against your quota.

Unlike DoltHub, this dataset carries real `volume` and `open_interest` per
contract, so backtests over it can run the strategy's actual liquidity
filters (min_open_interest=500, min_volume=50) instead of the zeroed
DoltHub override — see configs/dolthub_backtest.json for that comparison.

No underlying spot price is returned by this endpoint either (only
`moneyness`), so spot is joined the same way as the DoltHub importer: daily
closes from yfinance via `build_spot_lookup` (reused from dolthub.py).

Imported snapshots write to their own SnapshotStore root (default
`data_snapshots_eodhd/`), kept separate from both the live forward-collected
dataset and the DoltHub import.
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

API_URL = "https://eodhd.com/api/mp/unicornbay/options/eod"
PAGE_LIMIT = 1000        # endpoint max per page
MAX_OFFSET = 10000       # endpoint hard cap on page[offset]
REQUEST_PAUSE_S = 0.25
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FIELDS = (
    "contract,underlying_symbol,exp_date,type,strike,"
    "bid,ask,volume,open_interest,volatility,delta,tradetime"
)


def _check_symbol(symbol: str) -> str:
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"Suspicious symbol {symbol!r}")
    return symbol


def _check_date(d: str) -> str:
    if not _DATE_RE.match(d):
        raise ValueError(f"Dates must be YYYY-MM-DD, got {d!r}")
    return d


class EODHDClient:
    """Minimal read-only client for EODHD's options EOD endpoint with
    pagination. `api_key` is sent as the `api_token` query param."""

    def __init__(self, api_key: str, pause_s: float = REQUEST_PAUSE_S):
        if not api_key:
            raise ValueError("EODHD_API_KEY is required")
        self.api_key = api_key
        self.pause_s = pause_s

    def query(self, filters: dict[str, str], offset: int = 0,
              limit: int = PAGE_LIMIT) -> list[dict]:
        params = {f"filter[{k}]": v for k, v in filters.items()}
        params.update({
            "sort": "exp_date",
            "page[offset]": offset,
            "page[limit]": limit,
            "fields[options-eod]": FIELDS,
            "api_token": self.api_key,
            "fmt": "json",
        })
        for attempt in range(4):
            resp = requests.get(API_URL, params=params, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2.0 * (attempt + 1)
                logger.info("EODHD %s — retrying in %.0fs", resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 403:
                raise RuntimeError(
                    "EODHD 403 Forbidden on options/eod — the marketplace "
                    "'US Stock Options' add-on is not enabled on this API key's plan"
                )
            resp.raise_for_status()
            body = resp.json()
            return body.get("data", [])
        raise RuntimeError("EODHD API: retries exhausted")

    def query_all(self, filters: dict[str, str]) -> list[dict]:
        """Drain all pages for one filter set, up to the endpoint's
        page[offset] cap of 10000 records."""
        out: list[dict] = []
        offset = 0
        while offset <= MAX_OFFSET:
            page = self.query(filters, offset=offset)
            out.extend(page)
            if len(page) < PAGE_LIMIT:
                return out
            offset += PAGE_LIMIT
            time.sleep(self.pause_s)
        logger.warning(
            "EODHD page[offset] cap (%d) hit for filters=%s — results truncated",
            MAX_OFFSET, filters,
        )
        return out


class EODHDImporter:
    def __init__(self, api_key: str, client: EODHDClient | None = None):
        self.client = client or EODHDClient(api_key)

    def fetch_day(self, symbol: str, day: str, max_dte: int = 10) -> list[dict]:
        """All chain rows for one symbol/scan-day, expirations within max_dte."""
        symbol, day = _check_symbol(symbol), _check_date(day)
        d = date_cls.fromisoformat(day)
        exp_max = date_cls.fromordinal(d.toordinal() + max_dte).isoformat()
        rows = self.client.query_all({
            "underlying_symbol": symbol,
            "tradetime_eq": day,
            "exp_date_from": day,
            "exp_date_to": exp_max,
        })
        return [r["attributes"] for r in rows]


def rows_to_snapshots(rows: list[dict],
                      spot_lookup: dict[tuple[str, str], float]) -> list[ChainSnapshot]:
    """Group EODHD options-eod rows into one ChainSnapshot per
    (scan date, symbol, expiration). Rows for days with no spot available
    are skipped with a warning — a snapshot without spot is unusable.

    volume/open_interest come straight from the dataset (real values,
    unlike the DoltHub importer) so the strategy's normal liquidity
    filters apply without needing a zeroed override config.
    """
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        tradetime = str(r.get("tradetime") or "")[:10]
        key = (tradetime, str(r["underlying_symbol"]), str(r["exp_date"])[:10])
        grouped.setdefault(key, []).append(r)

    snapshots: list[ChainSnapshot] = []
    skipped_no_spot = set()
    for (day, symbol, expiration), chunk in sorted(grouped.items()):
        spot = spot_lookup.get((symbol, day))
        if not spot:
            skipped_no_spot.add((symbol, day))
            continue
        records = []
        for r in chunk:
            records.append({
                "type": str(r["type"]).strip().lower(),
                "strike": float(r["strike"]),
                "bid": float(r["bid"] or 0),
                "ask": float(r["ask"] or 0),
                "volume": int(r["volume"] or 0),
                "open_interest": int(r["open_interest"] or 0),
                "iv": float(r["volatility"] or 0),
            })
        chain = pd.DataFrame(records, columns=CHAIN_COLUMNS)
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
