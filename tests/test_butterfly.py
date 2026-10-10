"""Iron butterfly: structure, gates, off-by-default wiring, and the loss stop."""
from dataclasses import replace
from datetime import date

import pandas as pd

from options_trader.config import StrategyConfig
from options_trader.execution.paper import PaperBroker
from options_trader.signals.butterfly import (BUTTERFLY_VARIANTS, IRON_FLY,
                                              build_iron_butterfly)
from options_trader.signals.credit import build_position
from tests.test_income_scripts import _cfg, _chain, _condor, _open

SPOT = 700.4


def _fly_chain():
    rows = []
    for k in range(660, 742, 2):
        dist = abs(k - SPOT)
        ext = max(0.05, 11.0 - 0.45 * dist + 0.004 * dist * dist)
        put = ext + max(0.0, k - SPOT)
        call = ext + max(0.0, SPOT - k)
        rows.append({"type": "put", "strike": float(k), "bid": round(put - 0.05, 2),
                     "ask": round(put + 0.05, 2), "iv": 0.17})
        rows.append({"type": "call", "strike": float(k), "bid": round(call - 0.05, 2),
                     "ask": round(call + 0.05, 2), "iv": 0.15})
    return pd.DataFrame(rows)


def _build(cfg=IRON_FLY):
    return build_iron_butterfly(_fly_chain(), SPOT, "SPY", "2026-10-12",
                                "2026-11-20", 39, cfg)


class TestStructure:
    def test_shorts_share_the_atm_strike(self):
        pos = _build()
        assert pos is not None
        assert pos.short_put_strike == pos.short_call_strike == 700.0

    def test_four_legs_defined_risk(self):
        pos = _build()
        assert len(pos.legs) == 4
        assert sorted(l.side for l in pos.legs) == [-1, -1, 1, 1]
        w = max(pos.widths().values())
        assert abs(w - 0.03 * SPOT) <= 2.0
        assert 0 < pos.max_loss < w

    def test_build_position_dispatches_on_structure(self):
        a = _build()
        b = build_position(_fly_chain(), SPOT, "SPY", "2026-10-12",
                           "2026-11-20", 39, IRON_FLY)
        assert b is not None and b.legs == a.legs

    def test_credit_floor_rejects(self):
        assert _build(replace(IRON_FLY, min_credit_frac=0.99)) is None

    def test_missing_side_returns_none(self):
        chain = _fly_chain()
        assert build_iron_butterfly(chain[chain["type"] == "put"], SPOT, "SPY",
                                    "2026-10-12", "2026-11-20", 39,
                                    IRON_FLY) is None


class TestFrameworkRules:
    def test_window_and_exits(self):
        assert (IRON_FLY.min_dte, IRON_FLY.max_dte) == (30, 45)
        assert IRON_FLY.profit_take_frac == 0.20
        assert IRON_FLY.time_exit_threshold(40) == 20
        assert IRON_FLY.time_exit_threshold(44) == 21
        assert IRON_FLY.stop_loss_frac is None
        assert not IRON_FLY.exit_on_breach

    def test_off_by_default(self):
        assert StrategyConfig().enable_iron_butterfly is False
        assert StrategyConfig.from_json(
            "configs/income_framework.json").enable_iron_butterfly is False

    def test_not_in_any_profile(self):
        from options_trader.signals.income import PROFILES
        for p in PROFILES.values():
            assert IRON_FLY.name not in p.variants

    def test_management_resolves_the_fly_config(self):
        from scripts.manage_income import variant_config
        from options_trader.signals.income import AS_SPECIFIED
        assert variant_config("spy_iron_fly", AS_SPECIFIED) is \
            BUTTERFLY_VARIANTS["spy_iron_fly"]


