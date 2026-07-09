"""Market data layer.

`DataProvider` is the interface the rest of the pipeline consumes. The
yfinance implementation is the free fallback; a Robinhood MCP implementation
plugs in behind the same interface once live chain tools are available.

`SnapshotStore` persists each scan's chains to disk. Free historical options
data does not exist, so the honest way to backtest this strategy is to
collect snapshots forward from today and replay them at expiry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path

import pandas as pd

# Normalized chain columns used everywhere downstream.
CHAIN_COLUMNS = [
    "type",        # 'call' | 'put'
    "strike",
    "bid",
    "ask",
    "volume",
    "open_interest",
    "iv",          # implied volatility, annualized decimal (0.18 = 18%)
]


@dataclass
class ChainSnapshot:
    underlying: str
    spot: float
    expiration: str            # YYYY-MM-DD
    taken_at: str              # ISO timestamp of the quote snapshot
    chain: pd.DataFrame        # normalized to CHAIN_COLUMNS

    @property
    def dte(self) -> int:
        exp = date.fromisoformat(self.expiration)
        taken = datetime.fromisoformat(self.taken_at).date()
        return (exp - taken).days


class DataProvider:
    """Interface. Implementations must return normalized ChainSnapshots."""

    def get_spot(self, underlying: str) -> float:
        raise NotImplementedError

    def get_expirations(self, underlying: str) -> list[str]:
        raise NotImplementedError

    def get_chain(self, underlying: str, expiration: str) -> ChainSnapshot:
        raise NotImplementedError


class YFinanceProvider(DataProvider):
    """Free-data fallback. Quotes are delayed ~15 min — fine for scanning
    and paper trading, NOT sufficient for live execution decisions."""

    def __init__(self):
        import yfinance as yf  # imported lazily so tests don't need it
        self._yf = yf
        self._tickers: dict = {}

    def _ticker(self, underlying: str):
        if underlying not in self._tickers:
            self._tickers[underlying] = self._yf.Ticker(underlying)
        return self._tickers[underlying]

    def get_spot(self, underlying: str) -> float:
        t = self._ticker(underlying)
        price = t.fast_info.get("lastPrice")
        if not price:
            raise RuntimeError(f"No spot price for {underlying}")
        return float(price)

    def get_expirations(self, underlying: str) -> list[str]:
        return list(self._ticker(underlying).options)

    def get_chain(self, underlying: str, expiration: str) -> ChainSnapshot:
        t = self._ticker(underlying)
        raw = t.option_chain(expiration)
        frames = []
        for df, opt_type in [(raw.calls, "call"), (raw.puts, "put")]:
            norm = pd.DataFrame(
                {
                    "type": opt_type,
                    "strike": df["strike"].astype(float),
                    "bid": df["bid"].fillna(0.0).astype(float),
                    "ask": df["ask"].fillna(0.0).astype(float),
                    "volume": df["volume"].fillna(0).astype(int),
                    "open_interest": df["openInterest"].fillna(0).astype(int),
                    "iv": df["impliedVolatility"].fillna(0.0).astype(float),
                }
            )
            frames.append(norm)
        chain = pd.concat(frames, ignore_index=True)
        return ChainSnapshot(
            underlying=underlying,
            spot=self.get_spot(underlying),
            expiration=expiration,
            taken_at=datetime.now().isoformat(timespec="seconds"),
            chain=chain,
        )

    def get_settlement_close(self, underlying: str, expiration: str) -> float | None:
        """Official close on expiration day, for settling expired paper trades."""
        hist = self._ticker(underlying).history(
            start=expiration, period="5d", auto_adjust=False
        )
        if hist.empty:
            return None
        day = hist.loc[hist.index.strftime("%Y-%m-%d") == expiration]
        if day.empty:
            return None
        return float(day["Close"].iloc[0])


class SnapshotStore:
    """Persists chain snapshots as CSV + JSON metadata for backtesting."""

    def __init__(self, root: str | Path = "data_snapshots"):
        self.root = Path(root)

    def save(self, snap: ChainSnapshot) -> Path:
        day = snap.taken_at[:10]
        dir_ = self.root / snap.underlying
        dir_.mkdir(parents=True, exist_ok=True)
        # Include time in filename so multiple snapshots per day (e.g. every 45 min)
        # for the same expiration do not overwrite each other.
        t = datetime.fromisoformat(snap.taken_at)
        time_str = t.strftime("%H%M%S")
        base = dir_ / f"{day}_{time_str}_exp{snap.expiration}"
        snap.chain.to_csv(base.with_suffix(".csv"), index=False)
        meta = {
            "underlying": snap.underlying,
            "spot": snap.spot,
            "expiration": snap.expiration,
            "taken_at": snap.taken_at,
        }
        base.with_suffix(".json").write_text(json.dumps(meta, indent=2))
        return base.with_suffix(".csv")

    def load_all(self) -> list[ChainSnapshot]:
        snaps = []
        for meta_path in sorted(self.root.rglob("*.json")):
            csv_path = meta_path.with_suffix(".csv")
            if not csv_path.exists():
                continue
            meta = json.loads(meta_path.read_text())
            chain = pd.read_csv(csv_path)
            snaps.append(ChainSnapshot(chain=chain, **meta))
        return snaps
