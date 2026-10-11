"""Routes one PortfolioBacktestEngine across multiple chain-history sources
by ticker, so F/AAL/GME (free, DoltHub) and SOFI/RIVN/SNAP/AMC/MARA/CLSK
(paid, Databento -- see databento.py's module docstring for why DoltHub
can't serve these) can back the same backtest run without the engine
knowing sources differ. Every source here already shares the same
day_chains(symbol, day, max_dte) / prefetch(...) interface (DoltHubHistory,
UWHistory, DatabentoHistory) -- this just dispatches by symbol.
"""

from __future__ import annotations

import pandas as pd


class MultiSourceHistory:
    """`routes` maps ticker -> history source object. `default`, if given,
    handles any ticker not in `routes` explicitly."""

    def __init__(self, routes: dict[str, object], default: object | None = None):
        self.routes = routes
        self.default = default

    def _source_for(self, symbol: str):
        src = self.routes.get(symbol, self.default)
        if src is None:
            raise KeyError(f"No history source configured for ticker {symbol!r}")
        return src

    def day_chains(self, symbol: str, day: str, max_dte: int = 50) -> pd.DataFrame:
        return self._source_for(symbol).day_chains(symbol, day, max_dte)

    def prefetch(self, symbols: list[str], days: list[str], max_dte: int = 50,
                 workers: int = 4, progress_every: int = 25) -> None:
        """Groups symbols by source and prefetches each source once, so a
        mixed-vendor ticker universe still only issues one prefetch call per
        underlying source (each source's own cache/dedup still applies)."""
        by_source: dict[int, tuple[object, list[str]]] = {}
        for symbol in symbols:
            src = self._source_for(symbol)
            by_source.setdefault(id(src), (src, []))[1].append(symbol)
        for src, syms in by_source.values():
            src.prefetch(syms, days, max_dte=max_dte, workers=workers,
                        progress_every=progress_every)
