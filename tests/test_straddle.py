"""Long straddle/strangle candidate generation and settlement."""

import pandas as pd
import pytest

from options_trader.backtest import StraddleBacktestEngine
from options_trader.data.provider import ChainSnapshot
from options_trader.signals.straddle import (
    StraddleVariantConfig, generate_straddle_candidate,
)


def make_chain(spot: float = 100.0, volume: int = 100,
               open_interest: int = 1000) -> pd.DataFrame:
    """Synthetic single-expiration chain around spot with tight quotes,
    liquid by default. Strikes every 1.0 from 80 to 120."""
    rows = []
    for strike in range(80, 121):
        k = float(strike)
        call_mid = max(0.10, (spot - k) + 3.0 if k < spot else 3.0 - (k - spot) * 0.15)
        put_mid = max(0.10, (k - spot) + 3.0 if k > spot else 3.0 - (spot - k) * 0.15)
        for opt_type, mid in (("call", call_mid), ("put", put_mid)):
            rows.append({
                "type": opt_type, "strike": k,
                "bid": round(max(0.01, mid - 0.05), 2), "ask": round(mid + 0.05, 2),
                "volume": volume, "open_interest": open_interest, "iv": 0.25,
            })
    return pd.DataFrame(rows)


def snap(chain=None, spot=100.0, expiration="2026-02-13", dte=7,
        underlying="SPY", taken_at=None) -> ChainSnapshot:
    """taken_at defaults to `dte` days before `expiration` so callers can
    control ChainSnapshot.dte (a computed property) via a plain int."""
    if taken_at is None:
        from datetime import date, timedelta
        exp = date.fromisoformat(expiration)
        taken_at = (exp - timedelta(days=dte)).isoformat() + "T16:00:00"
    return ChainSnapshot(
        underlying=underlying, spot=spot, expiration=expiration,
        taken_at=taken_at, chain=chain if chain is not None else make_chain(spot),
    )


class TestCandidateGeneration:
    def test_straddle_picks_nearest_strike_each_side(self):
        cand = generate_straddle_candidate(snap(), StraddleVariantConfig("s"))
        assert cand is not None
        assert cand.kind == "straddle"
        assert cand.call_strike == 100.0 and cand.put_strike == 100.0

    def test_strangle_pushes_legs_otm(self):
        cfg = StraddleVariantConfig("s", strangle_width_frac=0.05)
        cand = generate_straddle_candidate(snap(), cfg)
        assert cand is not None
        assert cand.kind == "strangle"
        assert cand.call_strike == 105.0   # nearest to 100*1.05
        assert cand.put_strike == 95.0     # nearest to 100*0.95

    def test_debit_is_sum_of_both_mids(self):
        cand = generate_straddle_candidate(snap(), StraddleVariantConfig("s"))
        call_mid = (cand.call_leg["bid"] + cand.call_leg["ask"]) / 2.0
        put_mid = (cand.put_leg["bid"] + cand.put_leg["ask"]) / 2.0
        assert cand.debit == round(call_mid + put_mid, 4)

    def test_breakevens(self):
        cand = generate_straddle_candidate(snap(), StraddleVariantConfig("s"))
        assert cand.breakeven_low == round(cand.put_strike - cand.debit, 4)
        assert cand.breakeven_high == round(cand.call_strike + cand.debit, 4)

    def test_dte_outside_window_rejected(self):
        cfg = StraddleVariantConfig("s", min_dte=5, max_dte=14)
        assert generate_straddle_candidate(snap(dte=20), cfg) is None
        assert generate_straddle_candidate(snap(dte=1), cfg) is None

    def test_illiquid_leg_rejected(self):
        chain = make_chain(volume=0, open_interest=0)
        cfg = StraddleVariantConfig("s", min_open_interest=500, min_volume=50)
        assert generate_straddle_candidate(snap(chain=chain), cfg) is None

    def test_missing_side_rejected(self):
        chain = make_chain()
        calls_only = chain[chain["type"] == "call"]
        assert generate_straddle_candidate(snap(chain=calls_only),
                                           StraddleVariantConfig("s")) is None

    def test_debit_band_rejects_too_cheap_or_expensive(self):
        cfg = StraddleVariantConfig("s", min_debit=100.0)
        assert generate_straddle_candidate(snap(), cfg) is None


class TestSettlement:
    def test_settle_below_put_strike_profits_put_leg(self):
        cfg = StraddleVariantConfig("s")
        cand = generate_straddle_candidate(snap(), cfg)
        engine = StraddleBacktestEngine(cfg)
        result = engine.run([snap()], {("SPY", "2026-02-13"): 80.0})
        assert len(result.trades) == 1
        t = result.trades[0]
        # settle_value = max(0, 80-100) + max(0, 100-80) = 20
        assert t["settle_value"] == 20.0
        assert t["pnl"] == round((20.0 - t["entry_debit"]) * 100.0, 2)

    def test_settle_at_strike_is_total_loss(self):
        cfg = StraddleVariantConfig("s")
        engine = StraddleBacktestEngine(cfg)
        result = engine.run([snap()], {("SPY", "2026-02-13"): 100.0})
        t = result.trades[0]
        assert t["settle_value"] == 0.0
        assert t["pnl"] < 0

    def test_unsettled_expiration_skipped(self):
        cfg = StraddleVariantConfig("s")
        engine = StraddleBacktestEngine(cfg)
        result = engine.run([snap()], {})
        assert result.trades == []
        assert result.skipped_unsettled == 1

    def test_no_candidate_tracked_separately(self):
        cfg = StraddleVariantConfig("s", min_dte=100, max_dte=200)
        engine = StraddleBacktestEngine(cfg)
        result = engine.run([snap()], {("SPY", "2026-02-13"): 100.0})
        assert result.skipped_no_candidate == 1
        assert result.skipped_unsettled == 0

    def test_summary_computes_win_rate_and_drawdown(self):
        cfg = StraddleVariantConfig("s")
        engine = StraddleBacktestEngine(cfg)
        result = engine.run(
            [snap(expiration="2026-02-13"), snap(expiration="2026-02-20", dte=14)],
            {("SPY", "2026-02-13"): 80.0, ("SPY", "2026-02-20"): 100.0},
        )
        s = result.summary
        assert s["trades"] == 2
        assert s["win_rate"] == 0.5
        assert "max_drawdown" in s and "profit_factor" in s
