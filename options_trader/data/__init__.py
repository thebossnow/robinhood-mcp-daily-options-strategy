from .provider import ChainSnapshot, DataProvider, YFinanceProvider, SnapshotStore

__all__ = [
    "ChainSnapshot",
    "DataProvider",
    "YFinanceProvider",
    "MCPDataProvider",
    "EODHDProvider",
    "EODHDHistory",
    "SnapshotStore",
]

_LAZY = {
    "MCPDataProvider": ".mcp_provider",
    "EODHDProvider": ".eodhd",
    "EODHDHistory": ".eodhd",
}


def __getattr__(name):
    # Lazy so importing options_trader.data doesn't require any one
    # provider's dependencies unless it is actually used.
    module = _LAZY.get(name)
    if module:
        from importlib import import_module
        return getattr(import_module(module, __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
