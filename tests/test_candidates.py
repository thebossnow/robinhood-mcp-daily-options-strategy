import pandas as pd

from options_trader.signals import generate_candidates, leg_passes_liquidity


def _row(**kw):
    base = {"type": "call", "strike": 100.0, "bid": 1.0, "ask": 1.04,
            "volume": 200, "open_interest": 1000, "iv": 0.5}
    base.update(kw)
    return pd.Series(base)


class TestLegLiquidity:
    def test_good_leg_passes(self, cfg):
        assert leg_passes_liquidity(_row(), cfg)

    def test_zero_oi_fails_even_with_volume(self, cfg):
        # The original scanner's `and` bug let this contract through.
        assert not leg_passes_liquidity(_row(open_interest=0, volume=5000), cfg)

    def test_zero_volume_fails_even_with_oi(self, cfg):
        assert not leg_passes_liquidity(_row(volume=0, open_interest=5000), cfg)

    def test_zero_bid_fails(self, cfg):
        assert not leg_passes_liquidity(_row(bid=0.0), cfg)

    def test_wide_spread_fails(self, cfg):
        # 0.90/1.10: 20% of mid, over the 10% cap
        assert not leg_passes_liquidity(_row(bid=0.90, ask=1.10), cfg)

    def test_crossed_market_fails(self, cfg):
        assert not leg_passes_liquidity(_row(bid=1.10, ask=1.00), cfg)


class TestGenerateCandidates:
    def test_finds_positive_ev_verticals(self, cfg, snapshot):
        cands = generate_candidates(snapshot, cfg)
        assert cands, "expected at least one candidate from the cheap chain"
        kinds = {c.kind for c in cands}
        assert "bull_call" in kinds and "bear_put" in kinds

    def test_all_candidates_pass_every_filter(self, cfg, snapshot):
        for c in generate_candidates(snapshot, cfg):
            assert c.ev_after_costs > cfg.min_ev_after_costs
            assert c.p_win >= cfg.min_p_win
            assert c.debit <= c.width * cfg.max_debit_fraction
            assert c.max_loss == round(c.debit * 100, 2)
            assert c.max_profit == round((c.width - c.debit) * 100, 2)

    def test_ev_is_not_a_tautology(self, cfg, snapshot):
        # p_win must come from the model, not be asserted; the old scanner's
        # rr was identically 2.0 for every row.
        cands = generate_candidates(snapshot, cfg)
        assert all(0 < c.p_win < 1 for c in cands)
        assert len({c.p_win for c in cands}) > 1

    def test_bull_call_100_102_math(self, cfg, snapshot):
        cands = generate_candidates(snapshot, cfg)
        spread = next(
            c for c in cands
            if c.kind == "bull_call" and c.long_strike == 100 and c.short_strike == 102
        )
        assert abs(spread.debit - 0.60) < 1e-9      # 1.60 mid - 1.00 mid
        assert spread.width == 2.0
        assert spread.breakeven == 100.60
        assert spread.max_loss == 60.0
        assert spread.max_profit == 140.0
        # Costs: half-spreads (0.02 + 0.02) * frac 0.5 * 2 sides * 100
        assert abs(spread.est_costs - 4.0) < 1e-9

    def test_illiquid_legs_never_appear(self, cfg, snapshot):
        strikes = set()
        for c in generate_candidates(snapshot, cfg):
            strikes.update({c.long_strike, c.short_strike})
        assert 106.0 not in strikes  # zero OI
        assert 108.0 not in strikes  # wide spread

    def test_dte_window_enforced(self, cfg, snapshot):
        cfg.min_dte, cfg.max_dte = 10, 20  # snapshot is 5 DTE
        assert generate_candidates(snapshot, cfg) == []

    def test_sorted_by_ev_after_costs(self, cfg, snapshot):
        evs = [c.ev_after_costs for c in generate_candidates(snapshot, cfg)]
        assert evs == sorted(evs, reverse=True)
