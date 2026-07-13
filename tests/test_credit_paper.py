"""Credit paper-trading plumbing: journal migration, broker round trips,
validated variants, live liquidity gate, manage-script marking."""

import json
import sqlite3

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
