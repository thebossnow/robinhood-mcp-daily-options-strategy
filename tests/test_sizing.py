"""Tiered sizing: the schedule, the integer-contract floor, the book cap,
and the arithmetic that decides whether a structure is tradeable at all."""

import pytest

from options_trader.risk.sizing import (
    DEFAULT_TIERS, SizingTier, csp_capital_at_risk, min_equity_for_one_contract,
    risk_budget, size_position, spread_capital_at_risk, tier_for,
)


class TestTierSchedule:
    @pytest.mark.parametrize("equity,expected_pct", [
        (100.0, 0.10), (499.0, 0.10), (500.0, 0.10),
        (501.0, 0.07), (999.0, 0.07), (1000.0, 0.07),
        (1000.01, 0.05), (25_000.0, 0.05), (1e9, 0.05),
    ])
    def test_bands(self, equity, expected_pct):
        assert tier_for(equity).risk_pct == expected_pct

    def test_budget_is_pct_of_equity(self):
        assert risk_budget(1000.0) == pytest.approx(70.0)
        assert risk_budget(25_000.0) == pytest.approx(1250.0)

    def test_negative_equity_yields_zero_budget(self):
        assert risk_budget(-500.0) == 0.0

    def test_final_tier_must_be_open_ended(self):
        closed = (SizingTier(500.0, 0.10),)
        with pytest.raises(ValueError):
            tier_for(1_000.0, closed)

    def test_empty_tiers_rejected(self):
        with pytest.raises(ValueError):
            tier_for(1_000.0, ())


class TestBudgetNonMonotonicity:
    """The schedule is not monotonic in dollars: $500 at 10% permits $50,
    while $501 at 7% permits $35.07. min_equity_for_one_contract has to
    search bands rather than divide once."""

    def test_crossing_a_boundary_can_shrink_the_budget(self):
        assert risk_budget(500.0) > risk_budget(501.0)

    def test_position_needing_45_dollars_fits_the_bottom_band(self):
        # $45 fits inside $500's 10% budget, so no larger account is needed.
        assert min_equity_for_one_contract(45.0) == pytest.approx(450.0)

    def test_position_needing_60_dollars_skips_to_where_it_fits(self):
        # $60 needs $600 at 10% — but $600 sits in the 7% band, which only
        # allows $42. The answer is the first equity that genuinely holds it.
        need = min_equity_for_one_contract(60.0)
        assert risk_budget(need) >= 60.0 - 1e-9
        assert need == pytest.approx(60.0 / 0.07)

    def test_answer_always_actually_holds_the_position(self):
        for risk in (10, 35, 50, 51, 70, 71, 100, 400, 72_000):
            need = min_equity_for_one_contract(float(risk))
            assert risk_budget(need) >= risk - 1e-6, risk

    def test_zero_risk_rejected(self):
        with pytest.raises(ValueError):
            min_equity_for_one_contract(0.0)


class TestCapitalAtRisk:
    def test_spread_risk_is_width_less_credit(self):
        assert spread_capital_at_risk(5.0, 1.0) == pytest.approx(400.0)

    def test_credit_above_width_floors_at_zero(self):
        assert spread_capital_at_risk(1.0, 2.0) == 0.0

    def test_csp_risk_is_the_assignment_bill(self):
        assert csp_capital_at_risk(725.0, 6.0) == pytest.approx(71_900.0)

    def test_csp_dwarfs_the_spread_at_index_prices(self):
        spread = spread_capital_at_risk(1.0, 0.20)
        csp = csp_capital_at_risk(724.0, 6.0)
        assert csp > 800 * spread


class TestSizing:
    def test_sizes_down_to_whole_contracts(self):
        d = size_position(25_000.0, 400.0)          # budget 1250
        assert d.contracts == 3                      # not 3.125
        assert d.total_capital_at_risk == 1200.0
        assert d.risk_pct_of_equity == pytest.approx(0.048)

    def test_actual_risk_pct_never_exceeds_the_tier_cap(self):
        for equity in (400.0, 800.0, 5_000.0, 100_000.0):
            d = size_position(equity, 37.0)
            assert d.risk_pct_of_equity <= d.tier_pct + 1e-9

    def test_refuses_when_one_contract_is_too_big(self):
        d = size_position(500.0, 80.0)
        assert not d.feasible and d.contracts == 0
        assert "over the $50.00 allowed" in d.reasons[0]
        # NOT 80/0.10 = $800: that equity sits above the 10% band's $500
        # ceiling, and the 7% band tops out at $1,000 while needing
        # $1,142.86. The first equity that genuinely holds it is 80/0.05.
        assert d.min_equity_for_one_contract == pytest.approx(1_600.0)
        assert "$1,600.00 of equity" in d.reasons[0]

    def test_index_csp_is_refused_at_every_retail_equity(self):
        risk = csp_capital_at_risk(724.0, 6.0)
        for equity in (500.0, 1_000.0, 25_000.0, 250_000.0):
            assert not size_position(equity, risk).feasible

    def test_index_csp_becomes_feasible_only_in_seven_figures(self):
        risk = csp_capital_at_risk(724.0, 6.0)
        need = min_equity_for_one_contract(risk)
        assert need > 1_000_000
        assert size_position(need, risk).feasible

    def test_nonpositive_risk_rejected(self):
        d = size_position(1_000.0, 0.0)
        assert not d.feasible and "positive" in d.reasons[0]

    def test_max_contracts_caps_the_result(self):
        d = size_position(100_000.0, 400.0, max_contracts=2)
        assert d.contracts == 2


class TestPortfolioHeat:
    def test_heat_cap_trims_contracts(self):
        # 5% tier on 25k = 1250 budget = 3 contracts at 400; a 20% book cap
        # with 4600 already committed leaves room for only one.
        d = size_position(25_000.0, 400.0, open_capital_at_risk=4_600.0,
                          portfolio_heat_cap_pct=0.20)
        assert d.contracts == 1

    def test_full_book_refuses_entirely(self):
        d = size_position(25_000.0, 400.0, open_capital_at_risk=5_000.0,
                          portfolio_heat_cap_pct=0.20)
        assert not d.feasible
        assert "portfolio heat" in d.reasons[0]

    def test_no_cap_means_positions_are_sized_in_isolation(self):
        # The framework's literal reading: three positions, 15% of equity.
        d = size_position(25_000.0, 400.0, open_capital_at_risk=999_999.0)
        assert d.contracts == 3

    def test_heat_cap_never_increases_size(self):
        loose = size_position(25_000.0, 400.0, portfolio_heat_cap_pct=0.99)
        assert loose.contracts == 3


class TestSummary:
    def test_feasible_summary_states_dollars_and_pct(self):
        s = size_position(25_000.0, 400.0).summary()
        assert "3 contract(s)" in s and "4.80%" in s and "tier cap 5%" in s

    def test_infeasible_summary_leads_with_zero(self):
        assert size_position(500.0, 80.0).summary().startswith("0 contracts")
