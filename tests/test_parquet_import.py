"""Tests for the philippdubach parquet importer — fixture parquets on disk,
no network. Includes the full-liquidity end-to-end path that distinguishes
this source from DoltHub."""

import pandas as pd
import pytest

from options_trader.backtest import BacktestEngine
from options_trader.config import StrategyConfig
from options_trader.data.parquet_import import (
    frame_to_snapshots, load_options, load_underlying,
)
from options_trader.signals import generate_candidates


def _opt_row(date="2026-06-01", exp="2026-06-05", strike=100.0, typ="call",
             bid=1.58, ask=1.62, volume=200, oi=1000, iv=0.5):
    return {"date": date, "expiration": exp, "strike": strike, "type": typ,
            "bid": bid, "ask": ask, "volume": volume, "open_interest": oi,
            "implied_volatility": iv, "contract_id": f"{typ}{strike}{exp}",
            "symbol": "SPY", "delta": 0.5, "gamma": 0.0, "theta": 0.0,
            "vega": 0.0, "rho": 0.0}


@pytest.fixture
def parquet_files(tmp_path):
    options = pd.DataFrame([
        _opt_row(strike=100.0, typ="call", bid=1.58, ask=1.62),
        _opt_row(strike=102.0, typ="call", bid=0.98, ask=1.02),
        _opt_row(strike=100.0, typ="put", bid=1.53, ask=1.57),
        _opt_row(strike=98.0, typ="put", bid=0.98, ask=1.02),
        # A far expiration that must be excluded by the DTE filter
        _opt_row(exp="2026-09-18", strike=100.0, typ="call"),
        # A date outside the requested range
        _opt_row(date="2025-01-02", strike=100.0, typ="call"),
    ])
    underlying = pd.DataFrame({
        "date": ["2025-01-02", "2026-06-01", "2026-06-05"],
        "close": [90.0, 100.0, 103.0],
    })
    opt_path = tmp_path / "options.parquet"
    und_path = tmp_path / "underlying.parquet"
    options.to_parquet(opt_path, index=False)
    underlying.to_parquet(und_path, index=False)
    return opt_path, und_path


class TestLoading:
    def test_date_range_filter(self, parquet_files):
        opt_path, _ = parquet_files
        df = load_options(opt_path, "2026-01-01", "2026-12-31")
        assert set(df["date"]) == {"2026-06-01"}

    def test_underlying_close_lookup(self, parquet_files):
        _, und_path = parquet_files
        spots = load_underlying(und_path)
        assert spots["2026-06-01"] == 100.0

    def test_underlying_alt_price_column(self, tmp_path):
        pd.DataFrame({"quote_date": ["2026-06-01"], "price": [99.5]}).to_parquet(
            tmp_path / "u.parquet", index=False)
        assert load_underlying(tmp_path / "u.parquet")["2026-06-01"] == 99.5

    def test_underlying_unknown_schema_raises(self, tmp_path):
        pd.DataFrame({"foo": [1]}).to_parquet(tmp_path / "u.parquet", index=False)
        with pytest.raises(RuntimeError, match="Unrecognized underlying schema"):
            load_underlying(tmp_path / "u.parquet")


class TestConversion:
    def test_dte_filter_and_grouping(self, parquet_files):
        opt_path, und_path = parquet_files
        df = load_options(opt_path, "2026-01-01", "2026-12-31")
        snaps = frame_to_snapshots(df, load_underlying(und_path), "SPY", max_dte=10)
        assert len(snaps) == 1  # 2026-09-18 expiry excluded (109 DTE)
        snap = snaps[0]
        assert snap.expiration == "2026-06-05"
        assert snap.spot == 100.0 and snap.taken_at == "2026-06-01T16:00:00"
        assert len(snap.chain) == 4

    def test_liquidity_columns_survive(self, parquet_files):
        opt_path, und_path = parquet_files
        df = load_options(opt_path, "2026-01-01", "2026-12-31")
        snap = frame_to_snapshots(df, load_underlying(und_path), "SPY", 10)[0]
        assert (snap.chain["volume"] == 200).all()
        assert (snap.chain["open_interest"] == 1000).all()
        assert (snap.chain["iv"] == 0.5).all()

    def test_type_letter_codes_normalized(self):
        df = pd.DataFrame([_opt_row(typ="C"), _opt_row(typ="P", strike=98.0)])
        snaps = frame_to_snapshots(df, {"2026-06-01": 100.0}, "SPY", 10)
        assert set(snaps[0].chain["type"]) == {"call", "put"}

    def test_unknown_type_raises(self):
        df = pd.DataFrame([_opt_row(typ="straddle")])
        with pytest.raises(RuntimeError, match="Unrecognized option type"):
            frame_to_snapshots(df, {"2026-06-01": 100.0}, "SPY", 10)

    def test_day_without_spot_skipped(self):
        df = pd.DataFrame([_opt_row()])
        assert frame_to_snapshots(df, {}, "SPY", 10) == []


class TestEndToEnd:
    def test_standard_config_full_pipeline(self, parquet_files):
        """The reason this source beats DoltHub: real volume/OI means the
        STANDARD config applies — no zeroed liquidity minimums — and the
        snapshot flows through candidates and backtest settlement."""
        opt_path, und_path = parquet_files
        df = load_options(opt_path, "2026-01-01", "2026-12-31")
        spots = load_underlying(und_path)
        snap = frame_to_snapshots(df, spots, "SPY", 10)[0]

        cfg = StrategyConfig(account_equity=10_000.0)  # standard filters
        cands = generate_candidates(snap, cfg)
        assert cands, "full-liquidity chain should produce candidates"

        settlements = {("SPY", "2026-06-05"): spots["2026-06-05"]}
        result = BacktestEngine(cfg).run([snap], settlements)
        assert result.summary["trades"] == 1
        # Settled at 103: through the 102 short call → bull call max profit
        assert result.trades[0]["pnl"] != 0.0
