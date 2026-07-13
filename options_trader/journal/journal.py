"""SQLite trade journal.

Every recommendation, fill, and exit is recorded with the filter values that
justified it at entry. The risk manager reads live state (open positions,
today's realized P&L, consecutive losses) from here, and the periodic review
loop — human or agent — queries it to find out which setups actually work.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    underlying TEXT NOT NULL,
    expiration TEXT NOT NULL,
    kind TEXT NOT NULL,
    long_strike REAL NOT NULL,
    short_strike REAL NOT NULL,
    width REAL NOT NULL,
    contracts INTEGER NOT NULL,
    entry_debit REAL NOT NULL,          -- per share, after slippage
    max_loss REAL NOT NULL,             -- total dollars for the position
    max_profit REAL NOT NULL,
    p_win REAL,
    ev_after_costs REAL,                -- per contract, at entry
    candidate_json TEXT,                -- full candidate snapshot at entry
    status TEXT NOT NULL DEFAULT 'open',  -- open | closed | expired
    exit_value REAL,                    -- per share credit received at exit
    realized_pnl REAL,                  -- total dollars
    closed_at TEXT,
    notes TEXT,
    strategy TEXT NOT NULL DEFAULT 'vertical',  -- vertical | credit
    legs_json TEXT                      -- credit positions: all legs
);
"""

# Additive migrations for databases created before a column existed. Applied
# via ALTER TABLE on open, so an old journal.db keeps working untouched.
MIGRATION_COLUMNS = {
    "strategy": "TEXT NOT NULL DEFAULT 'vertical'",
    "legs_json": "TEXT",
}


@dataclass
class TradeRecord:
    id: int
    opened_at: str
    underlying: str
    expiration: str
    kind: str
    long_strike: float
    short_strike: float
    width: float
    contracts: int
    entry_debit: float
    max_loss: float
    max_profit: float
    p_win: float | None
    ev_after_costs: float | None
    status: str
    exit_value: float | None
    realized_pnl: float | None
    closed_at: str | None
    notes: str | None
    strategy: str = "vertical"
    legs_json: str | None = None


class Journal:
    def __init__(self, path: str | Path = "journal.db"):
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(trades)")}
        for name, decl in MIGRATION_COLUMNS.items():
            if name not in cols:
                self._conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self._conn.close()

    # --- writes ---

    def record_entry(self, candidate: dict, contracts: int,
                     entry_debit: float, notes: str = "") -> int:
        cur = self._conn.execute(
            """INSERT INTO trades
               (opened_at, underlying, expiration, kind, long_strike,
                short_strike, width, contracts, entry_debit, max_loss,
                max_profit, p_win, ev_after_costs, candidate_json, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(timespec="seconds"),
                candidate["underlying"],
                candidate["expiration"],
                candidate["kind"],
                candidate["long_strike"],
                candidate["short_strike"],
                candidate["width"],
                contracts,
                entry_debit,
                entry_debit * 100.0 * contracts,
                (candidate["width"] - entry_debit) * 100.0 * contracts,
                candidate.get("p_win"),
                candidate.get("ev_after_costs"),
                json.dumps(candidate),
                notes,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_credit_entry(self, position: dict, contracts: int,
                            notes: str = "") -> int:
        """Record a premium-selling position (put credit spread or iron
        condor) built by signals/credit.py. `position` is
        CreditPosition.to_dict(): entry_debit stores the per-share CREDIT
        received (after slippage); record_exit flips the P&L sign for
        strategy='credit' rows."""
        legs = position["legs"]
        widths: dict[str, float] = {}
        for opt_type in ("put", "call"):
            ks = [l["strike"] for l in legs if l["type"] == opt_type]
            if len(ks) == 2:
                widths[opt_type] = abs(ks[0] - ks[1])
        max_width = max(widths.values())
        credit = position["credit"]
        # Legacy strike columns carry the put side (call side for call-only
        # structures) so old queries stay meaningful; legs_json is canonical.
        side = "put" if "put" in widths else "call"
        side_legs = {l["side"]: l for l in legs if l["type"] == side}
        cur = self._conn.execute(
            """INSERT INTO trades
               (opened_at, underlying, expiration, kind, long_strike,
                short_strike, width, contracts, entry_debit, max_loss,
                max_profit, candidate_json, notes, strategy, legs_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(timespec="seconds"),
                position["underlying"],
                position["expiration"],
                position["variant"],
                side_legs[1]["strike"],
                side_legs[-1]["strike"],
                max_width,
                contracts,
                credit,
                (max_width - credit) * 100.0 * contracts,
                credit * 100.0 * contracts,
                json.dumps(position),
                notes,
                "credit",
                json.dumps(legs),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_exit(self, trade_id: int, exit_value: float,
                    status: str = "closed", notes: str = "") -> TradeRecord:
        row = self._get_row(trade_id)
        if row is None:
            raise KeyError(f"No trade with id {trade_id}")
        if row["status"] != "open":
            raise ValueError(f"Trade {trade_id} is already {row['status']}")
        if row["strategy"] == "credit":
            # entry_debit holds the credit received; exit_value is the cost
            # paid to close (or intrinsic at settlement).
            pnl = (row["entry_debit"] - exit_value) * 100.0 * row["contracts"]
        else:
            pnl = (exit_value - row["entry_debit"]) * 100.0 * row["contracts"]
        self._conn.execute(
            """UPDATE trades SET status=?, exit_value=?, realized_pnl=?,
               closed_at=?, notes=COALESCE(NULLIF(notes,'') || ' | ', '') || ?
               WHERE id=?""",
            (status, exit_value, pnl,
             datetime.now().isoformat(timespec="seconds"), notes, trade_id),
        )
        self._conn.commit()
        return self.get(trade_id)

    # --- reads ---

    def _get_row(self, trade_id: int):
        return self._conn.execute(
            "SELECT * FROM trades WHERE id=?", (trade_id,)
        ).fetchone()

    def get(self, trade_id: int) -> TradeRecord:
        row = self._get_row(trade_id)
        if row is None:
            raise KeyError(f"No trade with id {trade_id}")
        return _to_record(row)

    def open_positions(self) -> list[TradeRecord]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY opened_at"
        ).fetchall()
        return [_to_record(r) for r in rows]

    def open_credit_positions(self) -> list[TradeRecord]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE status='open' AND strategy='credit' "
            "ORDER BY opened_at"
        ).fetchall()
        return [_to_record(r) for r in rows]

    def candidate(self, trade_id: int) -> dict | None:
        """The full entry-time candidate/position snapshot, if recorded."""
        row = self._get_row(trade_id)
        if row is None or not row["candidate_json"]:
            return None
        return json.loads(row["candidate_json"])

    def open_risk(self) -> float:
        """Total max loss currently at risk across open positions."""
        val = self._conn.execute(
            "SELECT COALESCE(SUM(max_loss), 0) FROM trades WHERE status='open'"
        ).fetchone()[0]
        return float(val)

    def realized_pnl_on(self, day: str) -> float:
        val = self._conn.execute(
            """SELECT COALESCE(SUM(realized_pnl), 0) FROM trades
               WHERE status != 'open' AND substr(closed_at, 1, 10) = ?""",
            (day,),
        ).fetchone()[0]
        return float(val)

    def consecutive_losses(self) -> int:
        rows = self._conn.execute(
            """SELECT realized_pnl FROM trades WHERE status != 'open'
               ORDER BY closed_at DESC, id DESC"""
        ).fetchall()
        n = 0
        for r in rows:
            if r["realized_pnl"] is not None and r["realized_pnl"] < 0:
                n += 1
            else:
                break
        return n

    def stats(self) -> dict:
        rows = self._conn.execute(
            "SELECT realized_pnl FROM trades WHERE status != 'open'"
        ).fetchall()
        pnls = [r["realized_pnl"] for r in rows if r["realized_pnl"] is not None]
        no_trade = self.no_trade_count()
        if not pnls:
            return {"closed_trades": 0, "no_trade_days": no_trade}

        wins = [p for p in pnls if p > 0]
        running, peak, max_dd = 0.0, 0.0, 0.0
        for p in pnls:
            running += p
            peak = max(peak, running)
            max_dd = min(max_dd, running - peak)
        return {
            "closed_trades": len(pnls),
            "no_trade_days": no_trade,
            "win_rate": round(len(wins) / len(pnls), 4),
            "total_pnl": round(sum(pnls), 2),
            "expectancy_per_trade": round(sum(pnls) / len(pnls), 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(
                sum(p for p in pnls if p <= 0) / max(1, len(pnls) - len(wins)), 2
            ),
            "max_drawdown": round(max_dd, 2),
        }

    # --- PR#3-inspired no-trade logging (adapted to existing SQLite schema) ---
    def log_no_trade(self, thesis: str = "", date: str | None = None) -> None:
        """Record a 'no qualifying trade' day for statistics and discipline.
        Uses a lightweight row (status='no_trade')."""
        from datetime import date as _date
        day = date or _date.today().isoformat()
        self._conn.execute(
            """INSERT INTO trades
               (opened_at, underlying, expiration, kind, long_strike, short_strike,
                width, contracts, entry_debit, max_loss, max_profit, status, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'no_trade', ?)""",
            (day, "", "", "no_trade", 0, 0, 0, 0, 0, 0, 0, f"NO TRADE: {thesis}"),
        )
        self._conn.commit()

    def no_trade_count(self) -> int:
        val = self._conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='no_trade'"
        ).fetchone()[0]
        return int(val)
        wins = [p for p in pnls if p > 0]
        running, peak, max_dd = 0.0, 0.0, 0.0
        for p in pnls:
            running += p
            peak = max(peak, running)
            max_dd = min(max_dd, running - peak)
        return {
            "closed_trades": len(pnls),
            "win_rate": round(len(wins) / len(pnls), 4),
            "total_pnl": round(sum(pnls), 2),
            "expectancy_per_trade": round(sum(pnls) / len(pnls), 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(
                sum(p for p in pnls if p <= 0) / max(1, len(pnls) - len(wins)), 2
            ),
            "max_drawdown": round(max_dd, 2),
        }


def _to_record(row: sqlite3.Row) -> TradeRecord:
    d = dict(row)
    d.pop("candidate_json", None)
    return TradeRecord(**d)
