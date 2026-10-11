"""Databento (OPRA.PILLAR) historical EOD option-chain source.

Source: Databento's consolidated OPRA feed, `cbbo-1m` schema (best bid/offer
across all option exchanges, resampled once per minute per instrument),
queried with `stype_in="parent"` (e.g. symbol `SOFI.OPT` returns every
listed contract on that underlying). Coverage runs back to 2013-04-01 for
this schema — unlike DoltHub (dolthub.py), which has zero chain rows for
SOFI/RIVN/SNAP/AMC across the whole 2019-2026 sample and only a few months
for MARA/CLSK. This module exists specifically to fill that gap.

**Cost matters here, unlike the free sources.** Databento bills by bytes
transferred. A continuous full-trading-day pull of `cbbo-1m` (390 snapshots
per instrument per day) over the full 2021-2026 backtest range for all 6
gap tickers was quoted at ~$570 via `metadata.get_cost`. A single 1-minute
window at market close, queried per day like DoltHub/UW's per-(symbol,day)
caching, is ~35-40x cheaper (~$0.001-0.009/ticker/day even on GME's 2021
squeeze peak) because it captures one snapshot per instrument instead of
390 — full range is closer to $20-40. `DatabentoHistory` only ever issues
narrow per-day windows for this reason; never widen the window without
re-checking `metadata.get_cost` first.

No IV or delta comes back from raw OPRA quotes (unlike DoltHub/UW, which
carry a vendor-computed IV column) — `day_chains` solves IV from the
bid/ask mid via `signals.credit.implied_vol_from_price` and leaves `delta`
at 0.0, matching UW/DoltHub's convention when a source has no delta of its
own: `signals.credit._with_deltas` recomputes it from `iv` downstream.
This means a spot price for the (symbol, day) is required to compute
anything usable; days with no entry in `spot_lookup` are skipped (empty
frame), same as dolthub.py's `rows_to_snapshots` skipping spot-less days.

The OCC-format contract symbol (e.g. "SOFI  230616P00001500") comes back
on every quote row, so contract metadata (root/expiration/type/strike) is
parsed directly from it — no separate `definition` schema pull needed.

Imported/cached data lives in its own cache dir (default
`data_databento_cache/`), kept separate from DoltHub/UW/live caches.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import pandas as pd

from ..signals.credit import implied_vol_from_price
from .unusual_whales import load_api_key  # noqa: F401  (re-exported for callers)

logger = logging.getLogger(__name__)

OPRA_DATASET = "OPRA.PILLAR"
NY_TZ = ZoneInfo("America/New_York")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})\s*(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
                     r"(?P<cp>[CP])(?P<strike>\d{8})$")


def _check_symbol(symbol: str) -> str:
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"Suspicious symbol {symbol!r}")
    return symbol


def _check_date(d: str) -> str:
    if not _DATE_RE.match(d):
        raise ValueError(f"Dates must be YYYY-MM-DD, got {d!r}")
    return d


def parse_occ_symbol(raw: str) -> tuple[str, str, str, float]:
    """Parse an OCC-format option symbol into (root, expiration, type, strike).
    E.g. "SOFI  230616P00001500" -> ("SOFI", "2023-06-16", "put", 1.5)."""
    m = _OCC_RE.match(raw)
    if not m:
        raise ValueError(f"Not a recognizable OCC option symbol: {raw!r}")
    yy, mm, dd = m["yy"], m["mm"], m["dd"]
    expiration = f"20{yy}-{mm}-{dd}"
    option_type = "call" if m["cp"] == "C" else "put"
    strike = int(m["strike"]) / 1000.0
    return m["root"], expiration, option_type, strike


def _close_window_utc(day: str, window_minutes: int = 1) -> tuple[str, str]:
    """[market_close - window_minutes, market_close] for `day`, in UTC
    ISO-8601. Uses 16:00 America/New_York, so DST is handled automatically.
    Half trading days (early 13:00 close) fall outside this window and come
    back as a dataset gap, same as any other uncovered day."""
    d = date_cls.fromisoformat(day)
    close_ny = datetime(d.year, d.month, d.day, 16, 0, tzinfo=NY_TZ)
    start = close_ny - timedelta(minutes=window_minutes)
    return (start.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
            close_ny.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"))


class DatabentoClient:
    """Minimal read-only wrapper around databento.Historical, scoped to the
    one query pattern this repo needs: a narrow EOD quote window for one
    underlying's whole option chain."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("DATABENTO_API_KEY is required")
        import databento as db
        self._db = db
        self.client = db.Historical(api_key)

    def eod_quotes(self, symbol: str, day: str, window_minutes: int = 1) -> list[dict]:
        """Two failure modes need different handling, learned from a live
        multi-hour backtest crashing on each in turn:

        - A day with literally no listed options (pre-IPO, or -- rarely, as
          observed on 2 of ~8500 real AMC trading days -- an unexplained
          gap) makes the smart-symbol resolver 422 instead of returning zero
          rows the way DoltHub/UW do for an empty day. Treated as any other
          dataset gap: caught narrowly by the exact error case (never a bare
          except, so auth/rate-limit errors still surface) and turned into
          an empty result -- not retried, retrying won't produce data that
          doesn't exist.
        - Transient network errors (connect/read timeouts, resets -- ~9 of
          8520 jobs hit these in one real prefetch run) get retried with
          backoff, matching DoltHubClient/UWClient's existing pattern,
          instead of aborting the whole run over one flaky connection.
        """
        from databento.common.error import BentoClientError, BentoServerError
        start, end = _close_window_utc(day, window_minutes)
        for attempt in range(4):
            try:
                data = self.client.timeseries.get_range(
                    dataset=OPRA_DATASET, symbols=[f"{symbol}.OPT"], schema="cbbo-1m",
                    start=start, end=end, stype_in="parent",
                )
            except BentoClientError as exc:
                case = (exc.json_body or {}).get("detail", {}).get("case")
                if case == "symbology_invalid_request":
                    logger.info("%s %s: no options listed (dataset gap) — day skipped",
                               symbol, day)
                    return []
                raise
            except (requests.exceptions.RequestException, BentoServerError) as exc:
                wait = 2.0 * (attempt + 1)
                logger.info("Databento %s %s transient error (%s) — retrying in %.0fs",
                           symbol, day, exc, wait)
                time.sleep(wait)
                continue
            df = data.to_df()
            return [] if df.empty else df.reset_index().to_dict("records")
        raise RuntimeError(f"Databento API: retries exhausted for {symbol} {day}")


DAY_CHAIN_COLUMNS = ["expiration", "type", "strike", "bid", "ask", "iv", "delta"]


class DatabentoHistory:
    """Whole-day chain fetches matching DoltHubHistory/UWHistory's interface:
    `day_chains(symbol, day, max_dte) -> DataFrame` with DAY_CHAIN_COLUMNS.

    Requires a spot_lookup (see dolthub.build_spot_lookup) to solve IV from
    quoted bid/ask — a day with no spot entry is unusable and comes back as
    an empty frame, logged once. Disk-cached per (symbol, day) like the
    other two sources, so reruns and multi-variant sweeps never re-hit the
    API (and never re-incur its cost); `prefetch` warms the cache
    concurrently.
    """

    def __init__(self, api_key: str | None = None,
                 client: DatabentoClient | None = None,
                 cache_dir: str | Path = "data_databento_cache",
                 spot_lookup: dict[tuple[str, str], float] | None = None):
        self.client = client or DatabentoClient(api_key or load_api_key("DATABENTO_API_KEY"))
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.spot_lookup = spot_lookup or {}

    def _cached_fetch(self, symbol: str, day: str) -> list[dict]:
        import json
        path = self.cache_dir / f"{symbol}_{day}.json"
        if path.exists():
            return json.loads(path.read_text())
        rows = self.client.eod_quotes(symbol, day)
        cacheable = [{
            "symbol": r["symbol"],
            "bid_px_00": r.get("bid_px_00"),
            "ask_px_00": r.get("ask_px_00"),
            "ts_event": str(r.get("ts_event")),
        } for r in rows]
        path.write_text(json.dumps(cacheable))
        return cacheable

    def day_chains(self, symbol: str, day: str, max_dte: int = 50) -> pd.DataFrame:
        """Every chain row quoted at market close on `day`, expiration
        within max_dte. Columns: DAY_CHAIN_COLUMNS. `iv` is solved from
        bid/ask mid via Black-Scholes; `delta` is left 0.0 for the caller's
        BS fallback (see module docstring). Returns an empty frame when the
        dataset has no rows for the day or no spot is available."""
        symbol, day = _check_symbol(symbol), _check_date(day)
        spot = self.spot_lookup.get((symbol, day))
        if not spot:
            logger.warning("%s %s: no spot close available — day skipped", symbol, day)
            return pd.DataFrame(columns=DAY_CHAIN_COLUMNS)
        rows = self._cached_fetch(symbol, day)
        if not rows:
            return pd.DataFrame(columns=DAY_CHAIN_COLUMNS)
        d = date_cls.fromisoformat(day)
        hi = date_cls.fromordinal(d.toordinal() + max_dte).isoformat()

        latest: dict[str, dict] = {}
        for r in rows:
            try:
                root, expiration, option_type, strike = parse_occ_symbol(r["symbol"])
            except ValueError:
                continue
            if root != symbol or not (day <= expiration <= hi):
                continue
            bid, ask = float(r["bid_px_00"] or 0), float(r["ask_px_00"] or 0)
            if ask <= 0 or ask < bid:
                continue
            key = r["symbol"]
            if key not in latest or r["ts_event"] > latest[key]["ts_event"]:
                latest[key] = {"expiration": expiration, "type": option_type,
                               "strike": strike, "bid": bid, "ask": ask,
                               "ts_event": r["ts_event"]}

        records = []
        for leg in latest.values():
            dte = max((date_cls.fromisoformat(leg["expiration"]) - d).days, 1)
            t_years = dte / 365.0
            mid = (leg["bid"] + leg["ask"]) / 2.0
            iv = implied_vol_from_price(leg["type"], mid, spot, leg["strike"], t_years)
            records.append({
                "expiration": leg["expiration"], "type": leg["type"],
                "strike": leg["strike"], "bid": leg["bid"], "ask": leg["ask"],
                "iv": iv, "delta": 0.0,
            })
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
            futures = {pool.submit(self._cached_fetch, s, d): (s, d) for s, d in jobs}
            for fut in as_completed(futures):
                s, d = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    logger.warning("prefetch %s %s failed: %s", s, d, exc)
                done += 1
                if done % progress_every == 0:
                    logger.info("prefetch: %d/%d day-chains", done, len(jobs))
