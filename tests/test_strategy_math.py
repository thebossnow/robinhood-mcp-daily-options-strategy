"""Unit tests for the pure strategy math. Run: python -m unittest discover tests"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from strategy_math import (
    LiquidityRules, build_trade_plan, expected_move, max_contracts,
    mid_price, passes_liquidity, prob_itm, prob_touch, spread_pct, trade_ev,
)


class TestQuotes(unittest.TestCase):
    def test_mid_price(self):
        self.assertEqual(mid_price(1.00, 1.10), 1.05)
        self.assertEqual(mid_price(0, 1.10), 0.0)      # no bid -> unusable
        self.assertEqual(mid_price(1.10, 1.00), 0.0)   # crossed -> unusable

    def test_spread_pct(self):
        self.assertAlmostEqual(spread_pct(0.95, 1.05), 0.10, places=6)
        self.assertEqual(spread_pct(0, 0), float("inf"))


class TestProbabilities(unittest.TestCase):
    def test_atm_prob_near_half(self):
        # ATM option with modest vol/time expires ITM ~50% of the time.
        p = prob_itm("call", spot=100, strike=100, iv=0.20, t_years=5 / 252)
        self.assertAlmostEqual(p, 0.5, delta=0.03)

    def test_far_otm_prob_small(self):
        p = prob_itm("call", spot=100, strike=115, iv=0.20, t_years=5 / 252)
        self.assertLess(p, 0.01)

    def test_put_call_probs_sum_to_one(self):
        kwargs = dict(spot=100, strike=103, iv=0.25, t_years=10 / 252)
        self.assertAlmostEqual(
            prob_itm("call", **kwargs) + prob_itm("put", **kwargs), 1.0, places=6)

    def test_prob_touch_capped(self):
        self.assertLessEqual(
            prob_touch("call", spot=100, strike=100.01, iv=0.5, t_years=0.1), 0.95)

    def test_invalid_inputs_zero(self):
        self.assertEqual(prob_itm("call", 0, 100, 0.2, 0.1), 0.0)
        self.assertEqual(prob_itm("call", 100, 100, 0, 0.1), 0.0)


class TestExpectancyAndSizing(unittest.TestCase):
    def test_trade_ev(self):
        # 40% to win $100, 60% to lose $50 -> EV = 40 - 30 = +$10
        self.assertAlmostEqual(trade_ev(0.4, 100, 50), 10.0)
        # 20% to win $100, 80% to lose $50 -> EV = 20 - 40 = -$20
        self.assertAlmostEqual(trade_ev(0.2, 100, 50), -20.0)

    def test_max_contracts(self):
        self.assertEqual(max_contracts(1000, 0.02, 10), 2)
        self.assertEqual(max_contracts(1000, 0.02, 25), 0)  # can't size within risk
        self.assertEqual(max_contracts(0, 0.02, 10), 0)

    def test_expected_move(self):
        self.assertAlmostEqual(expected_move(100, 1.0, 1.0), 1.7)
        self.assertEqual(expected_move(100, 0, 0), 0.0)


class TestLiquidity(unittest.TestCase):
    def test_rejects_wide_spread(self):
        # 0.30/0.40: spread 0.10 on mid 0.35 = 29% > 10% cap
        self.assertFalse(passes_liquidity(0.30, 0.40, 5000, 1000))

    def test_rejects_dust_premium(self):
        # mid 0.10 below the 0.30 floor even with a tight spread
        self.assertFalse(passes_liquidity(0.09, 0.11, 5000, 1000))

    def test_accepts_liquid_contract(self):
        self.assertTrue(passes_liquidity(0.98, 1.02, 1000, 500))

    def test_oi_or_volume_carries(self):
        self.assertTrue(passes_liquidity(0.98, 1.02, 0, 500))    # volume only
        self.assertTrue(passes_liquidity(0.98, 1.02, 1000, 0))   # OI only
        self.assertFalse(passes_liquidity(0.98, 1.02, 10, 10))   # neither


class TestBuildTradePlan(unittest.TestCase):
    BASE = dict(ticker="SPY", expiration="2026-07-10", option_type="call",
                strike=101.0, bid=0.98, ask=1.02, iv=0.20, spot=100.0,
                days_to_expiry=3, open_interest=2000, volume=800,
                account_value=5000)

    def test_qualifying_trade_builds_plan(self):
        plan = build_trade_plan(**self.BASE)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.debit, 100.0)
        self.assertEqual(plan.profit_target, 2.0)   # +100% of 1.00 mid
        self.assertEqual(plan.stop_loss, 0.5)       # -50%
        self.assertGreater(plan.ev_per_contract, 0)
        self.assertGreaterEqual(plan.contracts, 1)

    def test_negative_ev_rejected(self):
        # Deep OTM: touch probability tiny, EV must go negative -> None
        plan = build_trade_plan(**{**self.BASE, "strike": 115.0, "bid": 0.30,
                                   "ask": 0.32})
        self.assertIsNone(plan)

    def test_illiquid_rejected(self):
        plan = build_trade_plan(**{**self.BASE, "open_interest": 0, "volume": 0})
        self.assertIsNone(plan)

    def test_account_too_small_rejected(self):
        # $5 account can't risk $50 within 2% -> zero contracts -> None
        plan = build_trade_plan(**{**self.BASE, "account_value": 5})
        self.assertIsNone(plan)


if __name__ == "__main__":
    unittest.main()
