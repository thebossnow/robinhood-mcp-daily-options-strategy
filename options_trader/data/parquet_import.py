"""Importer for the philippdubach historical options dataset (parquet).

Source: https://github.com/philippdubach/options-data — free EOD option
chains for 100+ US equities/ETFs, 2008–2025, hosted as one parquet per
ticker:

    https://static.philippdubach.com/data/options/{TICKER}/options.parquet
    https://static.philippdubach.com/data/options/{TICKER}/underlying.parquet

Schema (options): contract_id, symbol, expiration, strike, type, bid, ask,
volume, open_interest, date, implied_volatility, delta, gamma, theta, vega,
rho. The underlying file carries daily spot prices.

This source supersedes the DoltHub importer as the preferred historical
backtest dataset because it includes **volume and open interest**, so the
strategy's full liquidity filter applies — no zeroed-minimums config, no
"optimistic on liquidity" caveat. EOD-only still holds (snapshots stamped
16:00, hold-to-expiry evaluation), and it's community data: spot-check a
few chains against a broker.

Provided for educational/research use per the dataset's terms.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from pathlib import Path

import pandas as pd

from .provider import ChainSnapshot, CHAIN_COLUMNS

logger = logging.getLogger(__name__)

BASE_URL = "https://static.philippdubach.com/data/options"

OPTIONS_COLUMNS = ["date", "expiration", "strike", "type", "bid", "ask",
                   "volume", "open_interest", "implied_volatility"]

_TYPE_MAP = {"call": "call", "c": "call", "put": "put", "p": "put"}


def download_urls(ticker: str) -> tuple[str, str]:
    return (f"{BASE_URL}/{ticker}/options.parquet",
            f"{BASE_URL}/{ticker}/underlying.parquet")


def _norm_date_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).dt.strftime("%Y-%m-%d")


def load_options(path: str | Path, start: str, end: str) -> pd.DataFrame:
    """Load the per-ticker options parquet, date-filtered with predicate
    pushdown so multi-GB files don't need to fit in memory whole."""
    df = pd.read_parquet(
        path,
        columns=OPTIONS_COLUMNS,
        filters=[("date", ">=", start), ("date", "<=", end)],
    )
    if df.empty:
        # Some writers store dates as timestamps; retry without pushdown.
        df = pd.read_parquet(path, columns=OPTIONS_COLUMNS)
        df["date"] = _norm_date_series(df["date"])
        df = df[(df["date"] >= start) & (df["date"] <= end)]
    else:
        df["date"] = _norm_date_series(df["date"])
    df["expiration"] = _norm_date_series(df["expiration"])
    return df


def load_underlying(path: str | Path) -> dict[str, float]:
    """Daily closes keyed by YYYY-MM-DD. Tolerates close/price/adj_close
    column naming."""
    df = pd.read_parquet(path)
    df.columns = [c.lower() for c in df.columns]
    price_col = next(
        (c for c in ("close", "price", "adj_close", "last") if c in df.columns),
        None,
    )
    date_col = next((c for c in ("date", "quote_date") if c in df.columns), None)
    if price_col is None or date_col is None:
        raise RuntimeError(
            f"Unrecognized underlying schema {list(df.columns)} — expected a "
            "date column and one of close/price/adj_close/last."
        )
    return dict(zip(_norm_date_series(df[date_col]), df[price_col].astype(float)))


def frame_to_snapshots(options: pd.DataFrame, spot_by_day: dict[str, float],
                       ticker: str, max_dte: int) -> list[ChainSnapshot]:
    """Group rows into one ChainSnapshot per (scan date, expiration),
    keeping expirations within max_dte. Days without spot are skipped."""
    df = options.copy()
    df["type"] = df["type"].astype(str).str.strip().str.lower().map(_TYPE_MAP)
    if df["type"].isna().any():
        bad = options["type"][df["type"].isna()].unique()[:5]
        raise RuntimeError(f"Unrecognized option type values: {list(bad)}")

    dte = (pd.to_datetime(df["expiration"]) - pd.to_datetime(df["date"])).dt.days
    df = df[(dte >= 0) & (dte <= max_dte)]

    snapshots: list[ChainSnapshot] = []
    skipped_days = set()
    for (day, expiration), group in df.groupby(["date", "expiration"], sort=True):
        spot = spot_by_day.get(day)
        if not spot:
            skipped_days.add(day)
            continue
        chain = pd.DataFrame({
            "type": group["type"].values,
            "strike": group["strike"].astype(float).values,
            "bid": group["bid"].fillna(0.0).astype(float).values,
            "ask": group["ask"].fillna(0.0).astype(float).values,
            "volume": group["volume"].fillna(0).astype(int).values,
            "open_interest": group["open_interest"].fillna(0).astype(int).values,
            "iv": group["implied_volatility"].fillna(0.0).astype(float).values,
        }, columns=CHAIN_COLUMNS)
        snapshots.append(ChainSnapshot(
            underlying=ticker,
            spot=float(spot),
            expiration=expiration,
            taken_at=f"{day}T16:00:00",
            chain=chain,
        ))
    for day in sorted(skipped_days):
        logger.warning("%s %s: no underlying close — day skipped", ticker, day)
    return snapshots


def fetch_to(path: Path, url: str) -> None:
    """Download a parquet if not already cached locally."""
    if path.exists():
        return
    import requests
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s", url)
    with requests.get(url, stream=True, timeout=(10, 600)) as resp:
        resp.raise_for_status()
        tmp = path.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        tmp.rename(path)
