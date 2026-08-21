"""Cash-secured puts: collateral arithmetic, return gates, and the reason
they are not a defined-risk spread wearing a different name."""

import pandas as pd
import pytest

from options_trader.risk.sizing import csp_capital_at_risk, size_position
from options_trader.signals.csp import CSPConfig, CSPPosition, build_csp


def _chain(rows=None):
    return pd.DataFrame(rows or [
        {"type": "put", "strike": 725.0, "bid": 5.90, "ask": 6.10,
         "iv": 0.16, "delta": -0.25},
        {"type": "put", "strike": 700.0, "bid": 3.40, "ask": 3.60,
         "iv": 0.18, "delta": -0.15},
        {"type": "put", "strike": 650.0, "bid": 0.90, "ask": 1.10,
         "iv": 0.22, "delta": -0.05},
        {"type": "call", "strike": 800.0, "bid": 4.00, "ask": 4.20,
         "iv": 0.13, "delta": 0.20},
    ])


def _build(cfg=None, chain=None, dte=35):
    return build_csp(chain if chain is not None else _chain(), 762.60, "SPY",
                     "2026-08-21", "2026-09-25", dte, cfg or CSPConfig())


class TestConstruction:
    def test_selects_the_strike_nearest_the_delta_target(self):
        pos = _build(CSPConfig(short_put_delta=0.25))
        assert pos.strike == 725.0

        # Return floor off: the 700 strike's 3.50 credit annualizes to
        # ~5%, so the default 8% floor would reject it before we could
        # check which strike delta selection picked.
        far = _build(CSPConfig(short_put_delta=0.15, min_annualized_return=0.0))
        assert far.strike == 700.0

    def test_entry_credit_pays_slippage(self):
        pos = _build()
        assert pos.credit_mid == pytest.approx(6.00)
        assert pos.credit == pytest.approx(5.95)   # mid - 0.5 * half-spread

    def test_records_delta_and_iv_for_the_confirmation_line(self):
        pos = _build()
        assert pos.entry_delta == pytest.approx(-0.25)
        assert pos.entry_iv == pytest.approx(0.16)

    def test_no_put_side_yields_nothing(self):
        calls_only = _chain().query("type == 'call'")
        assert _build(chain=calls_only) is None

    def test_bid_below_the_floor_yields_nothing(self):
        thin = pd.DataFrame([{"type": "put", "strike": 725.0, "bid": 0.01,
                              "ask": 0.03, "iv": 0.16, "delta": -0.25}])
        assert _build(CSPConfig(min_short_bid=0.05), chain=thin) is None


class TestCollateralAndReturn:
    def test_collateral_is_the_assignment_bill_net_of_credit(self):
        pos = _build()
        assert pos.collateral == pytest.approx((725.0 - 5.95) * 100)

    def test_max_loss_matches_collateral_per_share(self):
        pos = _build()
        assert pos.max_loss * 100 == pytest.approx(pos.collateral)

    def test_breakeven_sits_below_the_strike(self):
        pos = _build()
        assert pos.breakeven == pytest.approx(719.05)
        assert pos.discount_to_spot == pytest.approx(
            (762.60 - 719.05) / 762.60, rel=1e-4)

    def test_annualized_return_scales_by_dte(self):
        short = _build(dte=35)
        assert short.annualized_return == pytest.approx(
            short.return_on_collateral * (365 / 35), rel=1e-9)

    def test_zero_dte_reports_zero_rather_than_dividing_by_zero(self):
        pos = CSPPosition("SPY", "2026-08-21", "2026-08-21", 0, 762.6,
                          725.0, credit=1.0)
        assert pos.annualized_return == 0.0

    def test_return_floor_rejects_a_thin_premium(self):
        # 25-delta put paying 6.00 on 35 DTE annualizes to ~8.6%.
        assert _build(CSPConfig(min_annualized_return=0.08)) is not None
        assert _build(CSPConfig(min_annualized_return=0.20)) is None


class TestSizingInteraction:
    """The point of the module: a CSP hands sizing its collateral, so a
    small account is refused instead of silently mis-sized."""

    def test_csp_risk_is_not_a_wing_width(self):
        pos = _build()
        risk = csp_capital_at_risk(pos.strike, pos.credit)
        assert risk == pytest.approx(pos.collateral)
        assert risk > 70_000

    def test_refused_at_every_account_the_tier_schedule_targets(self):
        pos = _build()
        risk = csp_capital_at_risk(pos.strike, pos.credit)
        for equity in (500.0, 1_000.0, 25_000.0, 500_000.0):
            assert not size_position(equity, risk).feasible

    def test_a_cheap_underlying_makes_it_feasible(self):
        cheap = pd.DataFrame([{"type": "put", "strike": 18.0, "bid": 0.45,
                               "ask": 0.55, "iv": 0.40, "delta": -0.25}])
        pos = build_csp(cheap, 20.0, "XYZ", "2026-08-21", "2026-09-25", 35,
                        CSPConfig())
        risk = csp_capital_at_risk(pos.strike, pos.credit)
        assert size_position(50_000.0, risk).feasible


class TestSerialization:
    def test_to_dict_carries_the_derived_figures(self):
        d = _build().to_dict()
        assert d["structure"] == "cash_secured_put"
        for key in ("collateral", "breakeven", "annualized_return",
                    "discount_to_spot", "max_loss"):
            assert key in d
