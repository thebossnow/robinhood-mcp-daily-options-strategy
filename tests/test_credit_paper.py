"""Credit paper-trading plumbing: journal migration, broker round trips,
validated variants, live liquidity gate, manage-script marking."""

import json
import sqlite3
from datetime import date

import pandas as pd
import pytest

from options_trader.config import StrategyConfig
from options_trader.execution.paper import PaperBroker
from options_trader.journal import Journal
from options_trader.signals.credit import (
    VALIDATED, VALIDATED_UNIVERSE, CreditLeg, CreditPosition,
    intrinsic_close_cost, leg_passes_live_liquidity,
)

OLD_SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL, underlying TEXT NOT NULL,
    expiration TEXT NOT NULL, kind TEXT NOT NULL,
    long_strike REAL NOT NULL, short_strike REAL NOT NULL,
    width REAL NOT NULL, contracts INTEGER NOT NULL,
    entry_debit REAL NOT NULL, max_loss REAL NOT NULL,
    max_profit REAL NOT NULL, p_win REAL, ev_after_costs REAL,
    candidate_json TEXT, status TEXT NOT NULL DEFAULT 'open',
    exit_value REAL, realized_pnl REAL, closed_at TEXT, notes TEXT
);
"""


def make_condor(credit=3.0, put_width=25.0, call_width=25.0) -> CreditPosition:
    legs = [
        CreditLeg("put", 650.0, -1, 3.4, 3.5, -0.15, 0.20),
        CreditLeg("put", 650.0 - put_width, 1, 1.4, 1.5, -0.08, 0.22),
        CreditLeg("call", 720.0, -1, 2.4, 2.5, 0.15, 0.18),
        CreditLeg("call", 720.0 + call_width, 1, 1.0, 1.1, 0.07, 0.17),
    ]
    pos = CreditPosition(
        underlying="SPY", variant="spy_condor15", entry_date="2026-07-13",
        expiration="2026-08-28", dte_at_entry=46, spot_at_entry=690.0,
        legs=legs, credit_mid=credit + 0.1, credit=credit,
        credit_frac=credit / max(put_width, call_width),
    )
    pos.max_loss = max(put_width, call_width) - credit
    return pos


def paper_cfg(**over) -> StrategyConfig:
    base = dict(account_equity=50000.0, max_risk_per_trade_pct=0.05,
                daily_loss_limit_pct=0.12, max_open_positions=6,
                max_consecutive_losses=5)
    base.update(over)
    return StrategyConfig(**base)


class TestJournalMigration:
    def test_old_db_gains_columns_and_keeps_rows(self, tmp_path):
        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.executescript(OLD_SCHEMA)
        conn.execute(
            """INSERT INTO trades (opened_at, underlying, expiration, kind,
               long_strike, short_strike, width, contracts, entry_debit,
               max_loss, max_profit) VALUES
               ('2026-01-05T10:00:00','SPY','2026-01-09','bull_call',
                600, 605, 5.0, 1, 2.0, 200.0, 300.0)""")
        conn.commit()
        conn.close()

        j = Journal(db)
        rec = j.get(1)
        assert rec.strategy == "vertical"    # old rows default
        assert rec.legs_json is None
        assert rec.underlying == "SPY"

    def test_vertical_pnl_sign_unchanged(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        tid = j.record_entry(
            {"underlying": "SPY", "expiration": "2026-08-28",
             "kind": "bull_call", "long_strike": 600.0, "short_strike": 605.0,
             "width": 5.0}, 1, 2.0)
        rec = j.record_exit(tid, 5.0)
        assert rec.realized_pnl == pytest.approx(300.0)


class TestCreditJournal:
    def test_entry_fields(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        pos = make_condor(credit=3.0, put_width=25.0)
        tid = j.record_credit_entry(pos.to_dict(), 1)
        rec = j.get(tid)
        assert rec.strategy == "credit"
        assert rec.kind == "spy_condor15"
        assert rec.entry_debit == pytest.approx(3.0)   # holds the credit
        assert rec.width == pytest.approx(25.0)
        assert rec.max_loss == pytest.approx((25.0 - 3.0) * 100)
        assert rec.max_profit == pytest.approx(300.0)
        assert len(json.loads(rec.legs_json)) == 4
        assert j.open_credit_positions()[0].id == tid

    def test_credit_pnl_sign_flipped(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        tid = j.record_credit_entry(make_condor(credit=3.0).to_dict(), 1)
        rec = j.record_exit(tid, 1.2)   # buy back cheaper than credit
        assert rec.realized_pnl == pytest.approx((3.0 - 1.2) * 100)
        assert j.open_credit_positions() == []

    def test_candidate_roundtrip(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        tid = j.record_credit_entry(make_condor().to_dict(), 1)
        cand = j.candidate(tid)
        assert cand["dte_at_entry"] == 46
        assert cand["variant"] == "spy_condor15"


class TestStrategyFilteredStats:
    """stats()/no_trade_count() must not blend the credit and vertical
    (debit) strategies' journal rows together — each strategy's paper-
    trading validation gate reads its own numbers only."""

    def test_stats_filters_closed_trades_by_strategy(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        vtid = j.record_entry(
            {"underlying": "SPY", "expiration": "2026-08-28", "kind": "bull_call",
             "long_strike": 600.0, "short_strike": 605.0, "width": 5.0}, 1, 2.0)
        j.record_exit(vtid, exit_value=3.0)   # vertical: +$100

        ctid = j.record_credit_entry(make_condor(credit=3.0).to_dict(), 1)
        j.record_exit(ctid, exit_value=1.2)   # credit: +$180

        assert j.stats()["closed_trades"] == 2
        credit_stats = j.stats(strategy="credit")
        assert credit_stats["closed_trades"] == 1
        assert credit_stats["total_pnl"] == pytest.approx(180.0)
        vertical_stats = j.stats(strategy="vertical")
        assert vertical_stats["closed_trades"] == 1
        assert vertical_stats["total_pnl"] == pytest.approx(100.0)

    def test_log_no_trade_tags_and_filters_by_strategy(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        j.log_no_trade("no vertical setup")                       # defaults to vertical
        j.log_no_trade("no credit setup", strategy="credit")
        assert j.no_trade_count() == 2
        assert j.no_trade_count(strategy="credit") == 1
        assert j.no_trade_count(strategy="vertical") == 1
        assert j.stats(strategy="credit")["no_trade_days"] == 1


class TestPaperBrokerCredit:
    def test_open_and_profit_close(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        broker = PaperBroker(paper_cfg(), j)
        tid, check = broker.open_credit(make_condor(credit=3.0), 1)
        assert tid is not None and check.allowed
        # buy back at mid 1.0 with 0.2 total half-spread -> cost 1.1
        rec = broker.close_credit(tid, 1.0, 0.2)
        assert rec.exit_value == pytest.approx(1.1)
        assert rec.realized_pnl == pytest.approx((3.0 - 1.1) * 100)

    def test_refused_when_over_per_trade_cap(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        broker = PaperBroker(paper_cfg(max_risk_per_trade_pct=0.01), j)
        tid, check = broker.open_credit(make_condor(), 1)   # $2200 > $500
        assert tid is None and not check.allowed
        assert any("per-trade cap" in r for r in check.reasons)

    def test_exit_cost_clamped_to_width(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        broker = PaperBroker(paper_cfg(), j)
        tid, _ = broker.open_credit(make_condor(credit=3.0, put_width=25.0), 1)
        rec = broker.close_credit(tid, 80.0, 1.0)   # absurd quote
        assert rec.exit_value == pytest.approx(25.0)
        assert rec.realized_pnl == pytest.approx(-(25.0 - 3.0) * 100)

    def test_settlement_inside_range_keeps_credit(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        broker = PaperBroker(paper_cfg(), j)
        tid, _ = broker.open_credit(make_condor(credit=3.0), 1)
        rec = broker.settle_expired_credit(tid, 690.0)   # between shorts
        assert rec.exit_value == 0.0
        assert rec.realized_pnl == pytest.approx(300.0)
        assert rec.status == "expired"

    def test_settlement_through_put_side(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        broker = PaperBroker(paper_cfg(), j)
        tid, _ = broker.open_credit(make_condor(credit=3.0, put_width=25.0), 1)
        rec = broker.settle_expired_credit(tid, 500.0)   # far below wing
        assert rec.exit_value == pytest.approx(25.0)


class TestValidatedVariants:
    def test_shipped_parameters(self):
        c = VALIDATED["spy_condor15"]
        assert (c.short_put_delta, c.short_call_delta) == (0.15, 0.15)
        assert c.wing_width_frac == 0.04
        assert c.exit_on_breach is False
        p = VALIDATED["spy_put10"]
        assert p.short_put_delta == 0.10 and p.short_call_delta is None
        assert p.exit_on_breach is False
        assert VALIDATED_UNIVERSE == ["SPY"]

    def test_intrinsic_from_leg_dicts(self):
        legs = [{"type": "put", "strike": 650.0, "side": -1},
                {"type": "put", "strike": 625.0, "side": 1}]
        assert intrinsic_close_cost(legs, 700.0) == 0.0
        assert intrinsic_close_cost(legs, 640.0) == pytest.approx(10.0)
        assert intrinsic_close_cost(legs, 600.0) == pytest.approx(25.0)


class TestLiveLiquidityGate:
    def row(self, **over):
        base = {"bid": 3.4, "ask": 3.5, "open_interest": 500, "volume": 100}
        base.update(over)
        return pd.Series(base)

    def test_good_leg_passes(self):
        assert leg_passes_live_liquidity(self.row())

    def test_zero_bid_fails(self):
        assert not leg_passes_live_liquidity(self.row(bid=0.0))

    def test_low_oi_fails(self):
        assert not leg_passes_live_liquidity(self.row(open_interest=50))

    def test_wide_spread_fails_but_nickel_floor_allowed(self):
        assert not leg_passes_live_liquidity(self.row(bid=3.0, ask=3.6))
        # cheap contract: 5-cent spread is > 10% of mid but under the floor
        assert leg_passes_live_liquidity(self.row(bid=0.20, ask=0.25))


class TestManageMarking:
    def test_mark_position_from_chain(self):
        from scripts.manage_credit import mark_position
        chain = pd.DataFrame([
            {"type": "put", "strike": 650.0, "bid": 2.0, "ask": 2.2},
            {"type": "put", "strike": 625.0, "bid": 0.8, "ask": 1.0},
        ])
        legs = [{"type": "put", "strike": 650.0, "side": -1},
                {"type": "put", "strike": 625.0, "side": 1}]
        cost, half = mark_position(legs, chain)
        assert cost == pytest.approx(2.1 - 0.9)
        assert half == pytest.approx(0.2)

    def test_missing_leg_returns_none(self):
        from scripts.manage_credit import mark_position
        chain = pd.DataFrame([
            {"type": "put", "strike": 650.0, "bid": 2.0, "ask": 2.2}])
        legs = [{"type": "put", "strike": 650.0, "side": -1},
                {"type": "put", "strike": 625.0, "side": 1}]
        assert mark_position(legs, chain) is None


class TestPickExpiration:
    def test_picks_closest_to_target_within_window(self):
        from scripts.scan_credit import pick_expiration
        today = date(2026, 7, 13)
        expirations = ["2026-08-03", "2026-08-10", "2026-08-28", "2026-09-04"]
        # DTE: 21, 28, 46, 53 — window 35-50 keeps only 46; target 45
        picked = pick_expiration(expirations, today, 35, 50, 45)
        assert picked == "2026-08-28"

    def test_no_expiration_in_window_returns_none(self):
        from scripts.scan_credit import pick_expiration
        today = date(2026, 7, 13)
        expirations = ["2026-08-03", "2026-08-10"]   # 21, 28 DTE
        assert pick_expiration(expirations, today, 35, 50, 45) is None


class TestResolveDteWindow:
    def test_weekly_variant_keeps_own_window(self):
        from scripts.scan_credit import resolve_dte_window
        cfg = paper_cfg(min_dte=35, max_dte=50)
        vcfg = VALIDATED["spy_weekly_put10"]
        assert resolve_dte_window(vcfg, cfg, is_weekly=True) == (5, 14)

    def test_long_dte_variant_intersected_with_account_window(self):
        from scripts.scan_credit import resolve_dte_window
        cfg = paper_cfg(min_dte=35, max_dte=50)
        vcfg = VALIDATED["spy_condor15"]   # variant default: 25-50
        assert resolve_dte_window(vcfg, cfg, is_weekly=False) == (35, 50)

    def test_account_window_wider_than_variant_leaves_variant_unchanged(self):
        from scripts.scan_credit import resolve_dte_window
        cfg = paper_cfg(min_dte=1, max_dte=90)
        vcfg = VALIDATED["spy_put10"]   # variant default: 25-50
        assert resolve_dte_window(vcfg, cfg, is_weekly=False) == (25, 50)
