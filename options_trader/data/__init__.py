from .provider import ChainSnapshot, DataProvider, YFinanceProvider, SnapshotStore

__all__ = [
    "ChainSnapshot",
    "DataProvider",
    "YFinanceProvider",
    "MCPDataProvider",
    "SnapshotStore",
]


def __getattr__(name):
    # Lazy so importing options_trader.data doesn't require the MCP
    # provider's dependencies unless it is actually used.
    if name == "MCPDataProvider":
        from .mcp_provider import MCPDataProvider
        return MCPDataProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
