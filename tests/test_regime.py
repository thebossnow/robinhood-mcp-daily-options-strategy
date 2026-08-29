"""Regime tagging and walk-forward split: bucket boundaries, SMA windowing,
trade tagging joins, and the train/test split rule."""

from dataclasses import dataclass

import pytest

from options_trader.reporting.regime import (
    compute_sma50, group_by, split_walk_forward, summarize_pnls, tag_trades,
    trend_bucket, vix_bucket,
)


class TestVixBucket:
    def test_boundaries(self):
        assert vix_bucket(14.99) == "<15"
        assert vix_bucket(15.0) == "15-20"
        assert vix_bucket(19.99) == "15-20"
        assert vix_bucket(20.0) == "20-30"
        assert vix_bucket(29.99) == "20-30"
        assert vix_bucket(30.0) == ">30"
        assert vix_bucket(45.0) == ">30"


class TestTrendBucket:
    def test_up_down_sideways(self):
        assert trend_bucket(110.0, 100.0) == "up"      # +10%, outside band
        assert trend_bucket(90.0, 100.0) == "down"      # -10%
        assert trend_bucket(100.5, 100.0) == "sideways"  # +0.5%, inside 1% band
        assert trend_bucket(100.0, None) == "unknown"
        assert trend_bucket(100.0, 0.0) == "unknown"


class TestComputeSma50:
    def test_needs_50_points_before_producing_a_value(self):
        closes = {f"2026-01-{d:02d}": 100.0 for d in range(1, 10)}
        sma = compute_sma50(closes)
        assert sma == {}   # fewer than 50 days supplied

    def test_matches_simple_average_once_window_full(self):
        days = [f"2026-{m:02d}-01" for m in range(1, 13)] * 5  # 60 unique-ish
        # Build 55 strictly increasing calendar days with known values.
        from datetime import date, timedelta
        base = date(2026, 1, 1)
        closes = {(base + timedelta(days=i)).isoformat(): float(i)
                 for i in range(55)}
        sma = compute_sma50(closes)
        last_day = (base + timedelta(days=54)).isoformat()
        # trailing 50 values ending at i=54: 5..54, average = 29.5
        assert sma[last_day] == pytest.approx(29.5)


@dataclass
class FakeTrade:
    sleeve_id: str
    underlying: str
    kind: str
    entry_date: str
    exit_date: str
    exit_reason: str
    contracts: int
    pnl: float


class TestTagTrades:
    def test_joins_vix_and_trend_by_entry_date_and_ticker(self):
        t = FakeTrade("S2", "F", "credit", "2026-01-05", "2026-01-10",
                      "profit_target", 2, 30.0)
        tagged = tag_trades(
            [t], vix_by_day={"2026-01-05": 22.0},
            sma50_by_ticker_day={("F", "2026-01-05"): 100.0},
            close_by_ticker_day={("F", "2026-01-05"): 110.0},
        )
        assert tagged[0]["vix_bucket"] == "20-30"
        assert tagged[0]["trend_bucket"] == "up"

    def test_unknown_when_data_missing(self):
        t = FakeTrade("S2", "F", "credit", "2026-01-05", "2026-01-10",
                      "profit_target", 2, 30.0)
        tagged = tag_trades([t], vix_by_day={}, sma50_by_ticker_day={},
                           close_by_ticker_day={})
        assert tagged[0]["vix_bucket"] == "unknown"
        assert tagged[0]["trend_bucket"] == "unknown"


class TestSummarizeAndGroupBy:
    def test_summarize_pnls_basic(self):
        s = summarize_pnls([10.0, -5.0, 20.0, -5.0])
        assert s["trades"] == 4
        assert s["win_rate"] == 0.5
        assert s["total_pnl"] == 20.0
        assert s["profit_factor"] == pytest.approx(3.0)

    def test_summarize_pnls_empty(self):
        assert summarize_pnls([]) == {"trades": 0}

    def test_group_by_buckets_pnl_per_key(self):
        tagged = [
            {"vix_bucket": "<15", "pnl": 10.0},
            {"vix_bucket": "<15", "pnl": -2.0},
            {"vix_bucket": ">30", "pnl": -50.0},
        ]
        grouped = group_by(tagged, "vix_bucket")
        assert grouped["<15"]["trades"] == 2
        assert grouped[">30"]["total_pnl"] == -50.0


class TestWalkForwardSplit:
    def test_splits_by_entry_date_inclusive_of_train_end(self):
        trades = [
            FakeTrade("S2", "F", "credit", "2023-12-31", "2024-01-05", "x", 1, 1.0),
            FakeTrade("S2", "F", "credit", "2024-01-01", "2024-01-10", "x", 1, 1.0),
            FakeTrade("S2", "F", "credit", "2022-06-01", "2022-06-10", "x", 1, 1.0),
        ]
        train, test = split_walk_forward(trades, train_end="2023-12-31")
        assert {t.entry_date for t in train} == {"2023-12-31", "2022-06-01"}
        assert {t.entry_date for t in test} == {"2024-01-01"}
