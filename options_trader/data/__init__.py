from .provider import ChainSnapshot, DataProvider, YFinanceProvider, SnapshotStore

__all__ = [
    "ChainSnapshot",
    "DataProvider",
    "YFinanceProvider",
    "MCPDataProvider",
    "SnapshotStore",
    "get_settlement_provider",
]

SETTLEMENT_PROVIDERS = ("yfinance", "alphavantage")


def get_settlement_provider(name: str = "yfinance"):
    """Factory for get_settlement_close() sources: 'yfinance' (default,
    free, no key) or 'alphavantage' (needs ALPHA_VANTAGE_API_KEY; free tier
    only — no options chains, see options_trader/data/alphavantage.py).
    Useful when yfinance's endpoint is unreachable but Alpha Vantage's is."""
    if name == "yfinance":
        return YFinanceProvider()
    if name == "alphavantage":
        from .alphavantage import AlphaVantageProvider
        return AlphaVantageProvider()
    raise ValueError(
        f"Unknown price provider {name!r} (choices: {SETTLEMENT_PROVIDERS})"
    )


def __getattr__(name):
    # Lazy so importing options_trader.data doesn't require the MCP
    # provider's dependencies unless it is actually used.
    if name == "MCPDataProvider":
        from .mcp_provider import MCPDataProvider
        return MCPDataProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
