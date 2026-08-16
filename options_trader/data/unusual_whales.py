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
from pathlib import Path

import pandas as pd
import requests

from .provider import ChainSnapshot, CHAIN_COLUMNS
from .dolthub import build_spot_lookup  # noqa: F401  (re-exported for callers)

logger = logging.getLogger(__name__)

API_BASE = "https://api.unusualwhales.com"
REQUEST_PAUSE_S = 0.25
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_api_key(env_var: str) -> str:
    """Read an API key from the environment, falling back to a `KEY=value`
    line in a `.env` file at the repo root. Shared by both UW-backed scripts
    (import_unusual_whales.py, backtest_credit.py --source uw)."""
    import os
    key = os.environ.get(env_var)
    if key:
        return key
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{env_var}="):
                return line.split("=", 1)[1].strip()
    return ""


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


DAY_CHAIN_COLUMNS = ["expiration", "type", "strike", "bid", "ask", "iv", "delta"]


class UWHistory:
    """Whole-day chain fetches for the managed credit backtest
    (options_trader/backtest/managed.py), matching DoltHubHistory's
    interface: `day_chains(symbol, day, max_dte) -> DataFrame` with columns
    DAY_CHAIN_COLUMNS.

    Unlike DoltHub, one UW call returns every expiration for the day in a
    single response, so caching keys on (symbol, day) only — the same
    cached fetch serves any max_dte the backtest asks for, filtered
    client-side. Results are cached on disk (JSON per symbol/day), so
    reruns and multi-variant sweeps never re-hit the API; `prefetch` warms
    the cache concurrently.

    Days outside the API key's historical access window (see
    HistoricalAccessError) are treated as dataset gaps — logged once and
    returned as an empty frame — matching how the engine already handles
    DoltHub's unquoted-expiration gaps, rather than aborting the run.
    """

    def __init__(self, api_key: str, client: UWClient | None = None,
                 cache_dir: str | Path = "data_uw_cache"):
        self.client = client or UWClient(api_key)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cached_fetch(self, symbol: str, day: str) -> list[dict] | None:
        path = self.cache_dir / f"{symbol}_{day}.json"
        if path.exists():
            import json
            return json.loads(path.read_text())
        try:
            rows = self.client.option_chains(symbol, day=day, greeks=True)
        except HistoricalAccessError as exc:
            logger.warning("%s %s: outside historical access window — %s",
                           symbol, day, exc)
            return None   # not cached: a wider access window later should retry
        import json
        path.write_text(json.dumps(rows))
        return rows

    def day_chains(self, symbol: str, day: str, max_dte: int = 50) -> pd.DataFrame:
        """Every chain row quoted on `day` with expiration within max_dte.

        Columns: DAY_CHAIN_COLUMNS. Returns an empty frame for days the
        dataset doesn't cover (access-window gap or empty response).
        """
        symbol, day = _check_symbol(symbol), _check_date(day)
        rows = self._cached_fetch(symbol, day)
        if not rows:
            return pd.DataFrame(columns=DAY_CHAIN_COLUMNS)
        d = date_cls.fromisoformat(day)
        hi = date_cls.fromordinal(d.toordinal() + max_dte).isoformat()
        records = [{
            "expiration": str(r["expires"])[:10],
            "type": str(r["option_type"]).strip().lower(),
            "strike": float(r["strike"]),
            "bid": float(r["nbbo_bid"] or 0),
            "ask": float(r["nbbo_ask"] or 0),
            "iv": float(r["implied_volatility"] or 0),
            "delta": float(r["delta"] or 0),
        } for r in rows if day <= str(r["expires"])[:10] <= hi]
        return pd.DataFrame(records, columns=DAY_CHAIN_COLUMNS)

    def prefetch(self, symbols: list[str], days: list[str], max_dte: int = 50,
                 workers: int = 4, progress_every: int = 25) -> None:
        """Warm the cache for (symbol, day) pairs concurrently. Failures on
        individual days are logged and skipped — the engine treats missing
        days as dataset gaps."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        jobs = [(s, d) for s in symbols for d in days
                if _SYMBOL_RE.match(s) and _DATE_RE.match(d)]
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.day_chains, s, d, max_dte): (s, d)
                       for s, d in jobs}
            for fut in as_completed(futures):
                s, d = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    logger.warning("prefetch %s %s failed: %s", s, d, exc)
                done += 1
                if done % progress_every == 0:
                    logger.info("prefetch: %d/%d day-chains", done, len(jobs))
