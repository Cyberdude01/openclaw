"""
Polymarket 15M — SQLite Persistence Layer

Tables
------
market_snapshots   Raw + calculated data per market, written every ~60 seconds.
decision_signals   Every signal emitted by the DecisionEngine.
trades_executed    Every order placed (or paper-traded), with resolution filled
                   in later when the market expires.
market_resolutions Market outcome once resolved (which side won: UP or DOWN).

Retention
---------
Rows older than DB_RETENTION_HOURS are trimmed automatically.  The full
long-term history lives in the GitHub reports that the DataExporter pushes.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DB_PATH, DB_RETENTION_HOURS

# ─── Schema ───────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    condition_id     TEXT,
    token_id_up      TEXT,
    token_id_down    TEXT,
    -- Prices from order books
    up_price         REAL,
    down_price       REAL,
    up_best_bid      REAL,
    up_best_ask      REAL,
    down_best_bid    REAL,
    down_best_ask    REAL,
    up_spread        REAL,
    down_spread      REAL,
    up_bid_depth     REAL,
    up_ask_depth     REAL,
    down_bid_depth   REAL,
    down_ask_depth   REAL,
    -- Market window state
    elapsed_pct      REAL,
    remaining_sec    REAL,
    arb_profit       REAL,
    -- Trade counts for this market window
    up_trade_count   INTEGER,
    down_trade_count INTEGER,
    total_volume     REAL,
    -- Analytics
    vol_bucket       TEXT,
    trend_bucket     TEXT,
    rv60             REAL,
    eff60            REAL,
    prob_up          REAL,
    dir_60pct        REAL,
    dir_80pct        REAL,
    dir_90pct        REAL,
    prob_008         REAL,
    prob_012         REAL,
    prob_020         REAL,
    -- Market window timestamps
    market_start_ts  TEXT,
    market_end_ts    TEXT
);

CREATE TABLE IF NOT EXISTS decision_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    condition_id TEXT,
    token_id     TEXT,
    outcome      TEXT,
    side         TEXT,
    size         REAL,
    price        REAL,
    confidence   REAL,
    trigger      TEXT,    -- arb | directional_60pct | directional_80pct | directional_90pct | trend_follow
    reasoning    TEXT,    -- Full human-readable explanation
    vol_bucket   TEXT,
    trend_bucket TEXT,
    prob_up      REAL,
    elapsed_pct  REAL,
    arb_profit   REAL
);

CREATE TABLE IF NOT EXISTS trades_executed (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,   -- entry timestamp
    symbol       TEXT    NOT NULL,
    condition_id TEXT,
    token_id     TEXT,
    outcome      TEXT,               -- UP | DOWN
    side         TEXT,               -- BUY | SELL
    size         REAL,               -- USDC notional
    entry_price  REAL,
    confidence   REAL,
    trigger      TEXT,               -- same tag as decision_signals
    reasoning    TEXT,               -- full explanation from signal
    mode         TEXT,               -- paper | live
    order_id     TEXT,
    -- Filled in when the market resolves
    resolved_at  TEXT,
    resolution   TEXT,               -- UP | DOWN (which outcome won)
    result       TEXT,               -- positive | negative | arb
    pnl          REAL                -- estimated USDC gained / lost
);

CREATE TABLE IF NOT EXISTS market_resolutions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id     TEXT    UNIQUE,
    symbol           TEXT    NOT NULL,
    resolved_at      TEXT,
    winning_outcome  TEXT,           -- UP | DOWN
    final_up_price   REAL,
    final_down_price REAL
);

CREATE INDEX IF NOT EXISTS idx_snap_ts     ON market_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snap_sym    ON market_snapshots(symbol);
CREATE INDEX IF NOT EXISTS idx_sig_ts      ON decision_signals(ts);
CREATE INDEX IF NOT EXISTS idx_sig_sym     ON decision_signals(symbol);
CREATE INDEX IF NOT EXISTS idx_trade_ts    ON trades_executed(ts);
CREATE INDEX IF NOT EXISTS idx_trade_cond  ON trades_executed(condition_id);
CREATE INDEX IF NOT EXISTS idx_res_cond    ON market_resolutions(condition_id);
"""


# ─── Database class ───────────────────────────────────────────────────────────

class Database:
    """
    Thin asyncio-compatible SQLite wrapper.

    All operations are synchronous (SQLite is fast for this workload) and safe
    to call from a single-threaded asyncio event loop.  If you ever move to a
    multi-threaded executor, wrap calls in loop.run_in_executor().
    """

    def __init__(self, path: Path = DB_PATH):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── Snapshot writes ───────────────────────────────────────────────────────

    def insert_snapshot(self, row: Dict[str, Any]) -> None:
        cols = ", ".join(row.keys())
        phs  = ", ".join("?" * len(row))
        self._conn.execute(
            f"INSERT INTO market_snapshots ({cols}) VALUES ({phs})",
            list(row.values()),
        )
        self._conn.commit()

    # ── Signal writes ─────────────────────────────────────────────────────────

    def insert_signal(self, row: Dict[str, Any]) -> None:
        cols = ", ".join(row.keys())
        phs  = ", ".join("?" * len(row))
        self._conn.execute(
            f"INSERT INTO decision_signals ({cols}) VALUES ({phs})",
            list(row.values()),
        )
        self._conn.commit()

    # ── Trade writes ──────────────────────────────────────────────────────────

    def insert_trade(self, row: Dict[str, Any]) -> int:
        """Insert executed trade and return its rowid."""
        cols = ", ".join(row.keys())
        phs  = ", ".join("?" * len(row))
        cur  = self._conn.execute(
            f"INSERT INTO trades_executed ({cols}) VALUES ({phs})",
            list(row.values()),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    # ── Resolution writes ─────────────────────────────────────────────────────

    def record_resolution(
        self,
        condition_id:    str,
        symbol:          str,
        winning_outcome: str,
        final_up_price:  float,
        final_down_price: float,
    ) -> None:
        """
        Store the market resolution and back-fill result / pnl on all open
        trades for that condition_id.
        """
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO market_resolutions
                (condition_id, symbol, resolved_at, winning_outcome,
                 final_up_price, final_down_price)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(condition_id) DO UPDATE SET
                resolved_at      = excluded.resolved_at,
                winning_outcome  = excluded.winning_outcome,
                final_up_price   = excluded.final_up_price,
                final_down_price = excluded.final_down_price
            """,
            [condition_id, symbol, ts, winning_outcome, final_up_price, final_down_price],
        )
        # Back-fill the trades_executed table for this market.
        # For a straight BUY of the winning side:
        #   positive → pnl ≈ (1 - entry_price) * size
        #   negative → pnl ≈ -entry_price * size
        # For ARB signals both legs are counted individually; the engine already
        # labels them 'arb' in the trigger column.
        self._conn.execute(
            """
            UPDATE trades_executed
            SET resolved_at = ?,
                resolution  = ?,
                result      = CASE
                                WHEN trigger = 'arb'     THEN 'arb'
                                WHEN outcome = ?         THEN 'positive'
                                ELSE                          'negative'
                              END,
                pnl         = CASE
                                WHEN trigger = 'arb'     THEN (1.0 - entry_price - 0.03) * size
                                WHEN outcome = ?         THEN (1.0 - entry_price) * size
                                ELSE                          -entry_price * size
                              END
            WHERE condition_id = ? AND resolved_at IS NULL
            """,
            [ts, winning_outcome, winning_outcome, winning_outcome, condition_id],
        )
        self._conn.commit()

    # ── Queries ───────────────────────────────────────────────────────────────

    def recent_snapshots(self, hours: int = 24, limit: int = 500) -> List[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self._conn.execute(
            "SELECT * FROM market_snapshots WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
            [cutoff, limit],
        ).fetchall()

    def all_signals(self, hours: int = 48) -> List[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self._conn.execute(
            "SELECT * FROM decision_signals WHERE ts >= ? ORDER BY ts DESC",
            [cutoff],
        ).fetchall()

    def all_trades(self) -> List[sqlite3.Row]:
        """All trades joined with resolution data (NULL if not yet resolved)."""
        return self._conn.execute(
            """
            SELECT t.*,
                   r.winning_outcome AS market_winner,
                   r.final_up_price,
                   r.final_down_price
            FROM   trades_executed t
            LEFT JOIN market_resolutions r USING (condition_id)
            ORDER BY t.ts DESC
            """,
        ).fetchall()

    def trade_summary(self) -> Dict[str, Any]:
        """Aggregate P&L and win-rate across all resolved trades."""
        row = self._conn.execute(
            """
            SELECT COUNT(*)                                    AS total,
                   SUM(CASE WHEN result='positive' THEN 1 END) AS wins,
                   SUM(CASE WHEN result='negative' THEN 1 END) AS losses,
                   SUM(CASE WHEN result='arb'      THEN 1 END) AS arb_trades,
                   ROUND(SUM(COALESCE(pnl, 0)), 4)             AS total_pnl
            FROM   trades_executed
            WHERE  resolved_at IS NOT NULL
            """
        ).fetchone()
        return dict(row) if row else {}

    # ── Trim ──────────────────────────────────────────────────────────────────

    def trim(self, retention_hours: int = DB_RETENTION_HOURS) -> int:
        """
        Delete market_snapshots and decision_signals older than retention_hours.
        trades_executed and market_resolutions are kept indefinitely
        (they are the audit log — GitHub reports archive them externally).
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=retention_hours)
        ).isoformat()
        cur = self._conn.execute(
            "DELETE FROM market_snapshots WHERE ts < ?", [cutoff]
        )
        deleted = cur.rowcount
        self._conn.execute("DELETE FROM decision_signals WHERE ts < ?", [cutoff])
        self._conn.commit()
        return deleted
