import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from options_trader.config import StrategyConfig
from options_trader.data.provider import ChainSnapshot


def _leg(opt_type, strike, bid, ask, volume=200, oi=1000, iv=0.5):
    return {
        "type": opt_type, "strike": strike, "bid": bid, "ask": ask,
        "volume": volume, "open_interest": oi, "iv": iv,
    }


@pytest.fixture
def cfg():
    # Equity bumped so a ~$60 max-loss spread passes the 1% per-trade cap.
    return StrategyConfig(account_equity=10_000.0)


@pytest.fixture
def snapshot():
    """Synthetic 5-DTE chain around spot=100 with IV=0.5.

    Mids are set slightly cheap relative to BS at that IV, so the 100/102
    bull call (debit 0.60) and 100/98 bear put (debit 0.55) both carry
    positive expected value after costs. Includes deliberately bad legs:
    a zero-OI call and a wide-spread call.
    """
    now = datetime(2026, 7, 6, 10, 0, 0)
    exp = (now + timedelta(days=5)).strftime("%Y-%m-%d")
    rows = [
        _leg("call", 100.0, 1.58, 1.62),
        _leg("call", 102.0, 0.98, 1.02),
        _leg("call", 104.0, 0.48, 0.52),
        _leg("call", 106.0, 0.20, 0.24, volume=500, oi=0),      # zero OI
        _leg("call", 108.0, 0.05, 0.15),                        # ~67% spread
        _leg("put", 100.0, 1.53, 1.57),
        _leg("put", 98.0, 0.98, 1.02),
        _leg("put", 96.0, 0.48, 0.52),
    ]
    return ChainSnapshot(
        underlying="TEST",
        spot=100.0,
        expiration=exp,
        taken_at=now.isoformat(timespec="seconds"),
        chain=pd.DataFrame(rows),
    )
