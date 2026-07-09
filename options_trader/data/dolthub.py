"""DoltHub historical EOD option-chain importer.

Source: the free `post-no-preference/options` database on DoltHub
(https://www.dolthub.com/repositories/post-no-preference/options), table
`option_chain(date, act_symbol, expiration, strike, call_put, bid, ask,
vol, delta, gamma, theta, vega, rho)` — end-of-day chains for US equity
options back to ~2019. `vol` is implied volatility.

Honest limitations of this dataset, which the importer makes explicit
rather than papering over:

- **No volume or open interest columns.** Imported rows carry 0 for both,
  which the strategy's liquidity filter would reject — so backtests over
  this data must use a config that zeroes `min_open_interest`/`min_volume`
  (see configs/dolthub_backtest.json). Results are therefore OPTIMISTIC on
  liquidity: a spread that looks tradeable in this backtest may have been
  practically untradeable. Forward-collected live snapshots remain the
  higher-fidelity dataset.
- **EOD only.** One snapshot per trading day, stamped 16:00; no intraday
  management can be evaluated.
- **No underlying spot in the table.** Spot is joined from daily closes
  (yfinance) via an injectable lookup.
- Community-maintained data: spot-check a few chains against a broker
  before trusting aggregates.

Imported snapshots write to their own SnapshotStore root (default
`data_snapshots_dolthub/`) so historical EOD data never mixes with the
live forward-collected dataset.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, date as date_cls

import pandas as pd
import requests

from .provider import ChainSnapshot, CHAIN_COLUMNS

logger = logging.getLogger(__name__)

API_URL = "https://www.dolthub.com/api/v1alpha1/{owner}/{database}/{branch}"
DEFAULT_OWNER = "post-no-preference"
DEFAULT_DATABASE = "options"
DEFAULT_BRANCH = "master"
PAGE_SIZE = 200          # DoltHub API caps result sizes; paginate defensively
REQUEST_PAUSE_S = 0.25   # be polite to the free API
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_symbol(symbol: str) -> str:
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(f"Suspicious symbol {symbol!r}")
    return symbol


def _check_date(d: str) -> str:
    if not _DATE_RE.match(d):
        raise ValueError(f"Dates must be YYYY-MM-DD, got {d!r}")
    return d


class DoltHubClient:
    """Minimal read-only client for DoltHub's SQL API with pagination."""

    def __init__(self, owner: str = DEFAULT_OWNER,
                 database: str = DEFAULT_DATABASE,
                 branch: str = DEFAULT_BRANCH,
                 pause_s: float = REQUEST_PAUSE_S):
        self.url = API_URL.format(owner=owner, database=database, branch=branch)
        self.pause_s = pause_s

    def query(self, sql: str) -> list[dict]:
        for attempt in range(4):
            resp = requests.get(self.url, params={"q": sql}, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2.0 * (attempt + 1)
                logger.info("DoltHub %s — retrying in %.0fs", resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            body = resp.json()
            status = body.get("query_execution_status")
            if status not in ("Success", "RowLimit"):
                raise RuntimeError(
                    f"DoltHub query failed ({status}): "
                    f"{body.get('query_execution_message', '')[:300]}"
                )
            if status == "RowLimit":
                logger.warning("DoltHub row limit hit — page size too large?")
            return body.get("rows", [])
        raise RuntimeError("DoltHub API: retries exhausted")

    def query_paged(self, sql_without_limit: str) -> list[dict]:
        """Append LIMIT/OFFSET pagination to a query and drain all pages.
        The query must have a deterministic ORDER BY."""
        out: list[dict] = []
        offset = 0
        while True:
            page = self.query(
                f"{sql_without_limit} LIMIT {PAGE_SIZE} OFFSET {offset}"
            )
            out.extend(page)
            if len(page) < PAGE_SIZE:
                return out
            offset += PAGE_SIZE
            time.sleep(self.pause_s)


class DoltHubImporter:
    def __init__(self, client: DoltHubClient | None = None):
        self.client = client or DoltHubClient()

    def available_dates(self, symbol: str, start: str, end: str) -> list[str]:
        symbol, start, end = _check_symbol(symbol), _check_date(start), _check_date(end)
        rows = self.client.query_paged(
            "SELECT DISTINCT `date` FROM `option_chain` "
            f"WHERE `act_symbol` = '{symbol}' "
            f"AND `date` BETWEEN '{start}' AND '{end}' ORDER BY `date`"
        )
        return [str(r["date"])[:10] for r in rows]

    def fetch_day(self, symbol: str, day: str, max_dte: int = 10) -> list[dict]:
        """All chain rows for one symbol/scan-day, expirations within max_dte."""
        symbol, day = _check_symbol(symbol), _check_date(day)
        d = date_cls.fromisoformat(day)
        exp_max = date_cls.fromordinal(d.toordinal() + max_dte).isoformat()
        return self.client.query_paged(
            "SELECT `date`, `act_symbol`, `expiration`, `strike`, `call_put`, "
            "`bid`, `ask`, `vol` FROM `option_chain` "
            f"WHERE `act_symbol` = '{symbol}' AND `date` = '{day}' "
            f"AND `expiration` BETWEEN '{day}' AND '{exp_max}' "
            "ORDER BY `expiration`, `call_put`, `strike`"
        )


def rows_to_snapshots(rows: list[dict],
                      spot_lookup: dict[tuple[str, str], float]) -> list[ChainSnapshot]:
    """Group DoltHub option_chain rows into one ChainSnapshot per
    (scan date, symbol, expiration). Rows for days with no spot available
    are skipped with a warning — a snapshot without spot is unusable.

    volume/open_interest are set to 0: THE DATASET DOES NOT PROVIDE THEM.
    Backtests over these snapshots must disable those liquidity minimums
    (configs/dolthub_backtest.json) and read results accordingly.
    """
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        key = (str(r["date"])[:10], str(r["act_symbol"]), str(r["expiration"])[:10])
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
                "type": str(r["call_put"]).strip().lower(),
                "strike": float(r["strike"]),
                "bid": float(r["bid"] or 0),
                "ask": float(r["ask"] or 0),
                "volume": 0,          # not in dataset — see docstring
                "open_interest": 0,   # not in dataset — see docstring
                "iv": float(r["vol"] or 0),
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


def build_spot_lookup(symbols: list[str], start: str, end: str) -> dict:
    """Daily closes from yfinance keyed by (symbol, YYYY-MM-DD)."""
    import yfinance as yf
    lookup: dict[tuple[str, str], float] = {}
    end_exclusive = date_cls.fromordinal(
        date_cls.fromisoformat(end).toordinal() + 1
    ).isoformat()
    for symbol in symbols:
        hist = yf.Ticker(symbol).history(
            start=start, end=end_exclusive, auto_adjust=False
        )
        for ts, row in hist.iterrows():
            lookup[(symbol, ts.strftime("%Y-%m-%d"))] = float(row["Close"])
    return lookup
