"""Cash-secured put candidate selection: delta window, liquidity, ROC."""

import pandas as pd
import pytest

from options_trader.signals.csp import CSPLiquidityRules, build_candidate, select_put


def make_chain(spot: float = 100.0) -> pd.DataFrame:
    """Synthetic put chain around spot=100 with explicit deltas (avoids any
    BS-fallback/IV mismatch) so strike selection is exactly checkable."""
    # Spreads are sized as a percentage of mid (all <= 10%, the default
    # max_spread_pct) rather than a flat dollar width — a flat width would
    # blow through the spread-pct filter on the cheaper, further-OTM legs.
    rows = [
        # strike, delta, bid, ask, oi, volume
        (100.0, 0.50, 2.95, 3.05, 2000, 500),
        (95.0, 0.35, 1.46, 1.54, 2000, 500),
        (90.0, 0.25, 0.82, 0.88, 2000, 500),
        (85.0, 0.15, 0.53, 0.57, 2000, 500),
        (80.0, 0.10, 0.335, 0.365, 2000, 500),
        (77.0, 0.08, 0.24, 0.26, 2000, 500),   # nearest to 0.10 target below 80
        (75.0, 0.06, 0.172, 0.188, 2000, 500),
        (70.0, 0.03, 0.077, 0.083, 2000, 500), # mid 0.08, below the premium floor
        (65.0, 0.02, 0.02, 0.20, 50, 10),      # illiquid AND wide — must be excluded
    ]
    return pd.DataFrame([
        {"type": "put", "strike": k, "bid": b, "ask": a,
         "open_interest": oi, "volume": v, "iv": 0.4, "delta": -d}
        for k, d, b, a, oi, v in rows
    ])


class TestSelectPut:
    def test_picks_strike_closest_to_target_within_window(self):
        chain = make_chain()
        row = select_put(chain, 100.0, dte=30, delta_min=0.05, delta_max=0.15,
                         delta_target=0.10)
        assert row is not None
        assert row["strike"] == 80.0   # delta 0.10 exactly

    def test_excludes_deltas_outside_window(self):
        chain = make_chain()
        row = select_put(chain, 100.0, dte=30, delta_min=0.05, delta_max=0.10,
                         delta_target=0.07)
        assert row is not None
        assert row["strike"] in (77.0, 75.0)  # 0.08 or 0.06, both inside [0.05,0.10]
        assert row["strike"] != 80.0            # delta 0.10 is at the edge but farther from 0.07

    def test_illiquid_wide_strike_excluded(self):
        chain = make_chain()
        # Force target near the illiquid 0.02-delta strike; it must never be picked.
        row = select_put(chain, 100.0, dte=30, delta_min=0.01, delta_max=0.05,
                         delta_target=0.02, rules=CSPLiquidityRules(min_open_interest=500))
        assert row is None or row["strike"] != 65.0

    def test_min_otm_pct_filters_close_strikes(self):
        chain = make_chain()
        # 95 is only 5% OTM; requiring 10% OTM should skip straight to 90 or below.
        row = select_put(chain, 100.0, dte=30, delta_min=0.20, delta_max=0.40,
                         delta_target=0.35, min_otm_pct=0.10)
        assert row is not None
        assert row["strike"] <= 90.0

    def test_returns_none_when_nothing_qualifies(self):
        chain = make_chain()
        row = select_put(chain, 100.0, dte=30, delta_min=0.90, delta_max=0.99,
                         delta_target=0.95)
        assert row is None


class TestBuildCandidate:
    def test_candidate_math(self):
        chain = make_chain()
        cand = build_candidate(chain, 100.0, "TEST", "2026-08-14", 30,
                               delta_min=0.05, delta_max=0.15, delta_target=0.10,
                               slippage_half_spread_frac=0.5)
        assert cand is not None
        assert cand.strike == 80.0
        # mid = (0.335+0.365)/2 = 0.35, half-spread 0.015, slippage 0.5 -> premium 0.3425
        assert cand.premium == pytest.approx(0.3425, abs=1e-6)
        assert cand.collateral == pytest.approx(8000.0)
        assert cand.roc == pytest.approx(0.3425 * 100 / 8000.0, abs=1e-5)
        assert cand.otm_pct == pytest.approx(0.20, abs=1e-6)

    def test_penny_premium_rejected(self):
        chain = make_chain()
        cand = build_candidate(chain, 100.0, "TEST", "2026-08-14", 30,
                               delta_min=0.01, delta_max=0.05, delta_target=0.02,
                               rules=CSPLiquidityRules(min_premium=0.15, min_open_interest=50))
        # strike 70 (delta .03, mid .07) is below the premium floor
        assert cand is None or cand.strike != 70.0

    def test_none_when_no_expiration_strike_qualifies(self):
        chain = make_chain()
        cand = build_candidate(chain, 100.0, "TEST", "2026-08-14", 30,
                               delta_min=0.90, delta_max=0.99, delta_target=0.95)
        assert cand is None
