"""Alpha Vantage settlement-price fallback.

This is NOT a full DataProvider — Alpha Vantage's free tier does not include
option chains (HISTORICAL_OPTIONS / REALTIME_OPTIONS return a "premium
endpoint" error on a free key), only equity price series. What it does give
for free is exactly what `get_settlement_close()` needs: a daily close on a
given date, to settle an expired paper trade.

Use case: environments where yfinance's endpoint is unreachable (e.g. a
sandboxed network policy that blocks Yahoo Finance but allows Alpha
Vantage) and only a settlement close is needed, not a live chain. Chain
data (scan.py, backtest.py candidate generation) still needs yfinance, the
Robinhood MCP provider, or DoltHub.

Requires a free API key: https://www.alphavantage.co/support/#api-key,
read from the ALPHA_VANTAGE_API_KEY env var or passed explicitly.

Free-tier rate limits are tight (historically ~5 requests/minute, 25/day),
so `get_settlement_close` fetches and caches a symbol's daily series on
first use — settling many expired trades for the same underlying costs one
request, not one per trade — and `pause_s` spaces out requests across
different underlyings.

`outputsize` defaults to Alpha Vantage's `compact` response (the last ~100
trading days, ~5 months): `full` is reserved for paid plans and returns an
`Information`/premium error on a free key, which would otherwise fail every
lookup on the advertised free-tier path. `compact` comfortably covers this
strategy's settlement dates (DTE window is a few weeks at most), so it's the
default; pass `output_size="full"` explicitly if you have a paid key and
need to settle something older.
"""

from __future__ import annotations

import os
import time

import requests

API_URL = "https://www.alphavantage.co/query"


class AlphaVantageProvider:
    def __init__(self, api_key: str | None = None, pause_s: float = 12.0,
                 output_size: str = "compact"):
        self.api_key = api_key or os.environ.get("ALPHA_VANTAGE_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "AlphaVantageProvider needs an API key: pass api_key= or set "
                "the ALPHA_VANTAGE_API_KEY env var (free tier signup at "
                "https://www.alphavantage.co/support/#api-key)"
            )
        self.pause_s = pause_s
        self.output_size = output_size
        self._daily_cache: dict[str, dict] = {}
        self._last_request = 0.0

    def _get(self, params: dict) -> dict:
        wait = self.pause_s - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        resp = requests.get(API_URL, params={**params, "apikey": self.api_key},
                            timeout=30)
        self._last_request = time.monotonic()
        resp.raise_for_status()
        body = resp.json()
        # The free REST API reports both bad params and plan/rate-limit
        # restrictions as 200 OK with an explanatory key instead of a
        # non-2xx status — surface those as errors rather than empty data.
        if "Error Message" in body:
            raise RuntimeError(f"Alpha Vantage error: {body['Error Message']}")
        if "Note" in body or "Information" in body:
            raise RuntimeError(
                "Alpha Vantage rate limit or plan restriction: "
                f"{body.get('Note') or body.get('Information')}"
            )
        return body

    def _daily_series(self, underlying: str) -> dict:
        if underlying not in self._daily_cache:
            body = self._get({
                "function": "TIME_SERIES_DAILY",
                "symbol": underlying,
                "outputsize": self.output_size,
            })
            self._daily_cache[underlying] = body.get("Time Series (Daily)", {})
        return self._daily_cache[underlying]

    def get_settlement_close(self, underlying: str, expiration: str) -> float | None:
        """Official close on expiration day, for settling expired paper
        trades. None if that date has no entry in the fetched daily history
        — a future date the market hasn't traded yet, or (on the default
        `compact` series) a date older than ~100 trading days back. Pass
        output_size='full' at construction (needs a paid AV plan) to settle
        older expirations."""
        row = self._daily_series(underlying).get(expiration)
        if row is None:
            return None
        return float(row["4. close"])
