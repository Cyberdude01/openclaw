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
    market_end_ts    TEXT,
    slug             TEXT
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

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
        # Schema migrations (add columns that may not exist in older DBs)
        for migration in [
            "ALTER TABLE market_snapshots ADD COLUMN slug TEXT",
            "ALTER TABLE trades_executed ADD COLUMN strategy_version TEXT",
        ]:
            try:
                self._conn.execute(migration)
                self._conn.commit()
            except Exception:
                pass  # Column already exists
        # One-shot data migration: fix pnl values stored as gross proceeds
        # (size / entry_price) — correct to net profit (size / entry_price - size).
        # Guarded by a settings flag so it only runs once.
        if not self._conn.execute(
            "SELECT 1 FROM settings WHERE key = 'pnl_net_migration_done'"
        ).fetchone():
            self._conn.execute(
                """
                UPDATE trades_executed
                SET pnl = pnl - size
                WHERE result IN ('positive', 'arb')
                  AND pnl IS NOT NULL
                  AND entry_price IS NOT NULL
                  AND entry_price > 0
                """
            )
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES ('pnl_net_migration_done', '1')"
            )
            self._conn.commit()
        # Initialise stats_start_ts once (records when the new tracking epoch began)
        existing = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'stats_start_ts'"
        ).fetchone()
        if not existing:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES ('stats_start_ts', ?)",
                [datetime.now(timezone.utc).isoformat()],
            )
            self._conn.commit()
        # Initialise stats_start_ts_v2 independently (fresh epoch for v2 report)
        existing_v2 = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'stats_start_ts_v2'"
        ).fetchone()
        if not existing_v2:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES ('stats_start_ts_v2', ?)",
                [datetime.now(timezone.utc).isoformat()],
            )
            self._conn.commit()
        # Initialise stats_start_ts_v1_prod — V1.0 production epoch.
        # Defaults to the v2 epoch start so the v1_Prod report aligns with v2.
        if not self._conn.execute(
            "SELECT 1 FROM settings WHERE key = 'stats_start_ts_v1_prod'"
        ).fetchone():
            v2_row = self._conn.execute(
                "SELECT value FROM settings WHERE key = 'stats_start_ts_v2'"
            ).fetchone()
            v1_start = v2_row["value"] if v2_row else datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES ('stats_start_ts_v1_prod', ?)",
                [v1_start],
            )
            self._conn.commit()
        # Initialise V2.0 (stats_start_ts_v3) and V3.0 (stats_start_ts_v4) dev epochs.
        for epoch_key in ("stats_start_ts_v3", "stats_start_ts_v4"):
            if not self._conn.execute(
                "SELECT 1 FROM settings WHERE key = ?", [epoch_key]
            ).fetchone():
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    [epoch_key, datetime.now(timezone.utc).isoformat()],
                )
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
        #
        # Polymarket binary market net P&L:
        #   You spend `size` USDC to buy  size / entry_price  tokens.
        #   If you win:  net profit = size / entry_price - size
        #   If you lose: net loss   = -size (full stake forfeited)
        #   ARB:         net profit = size / entry_price - size - 0.03 * size (fee)
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
                                WHEN trigger = 'arb'     THEN size / entry_price - size - 0.03 * size
                                WHEN outcome = ?         THEN size / entry_price - size
                                ELSE                          -size
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

    def all_signals(self, hours: int = 48, limit: int = 200) -> List[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return self._conn.execute(
            "SELECT * FROM decision_signals WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
            [cutoff, limit],
        ).fetchall()

    def all_trades(self) -> List[sqlite3.Row]:
        """All trades joined with resolution data and market window timestamps."""
        return self._conn.execute(
            """
            SELECT t.*,
                   r.winning_outcome  AS market_winner,
                   r.final_up_price,
                   r.final_down_price,
                   (SELECT MIN(s.market_start_ts) FROM market_snapshots s
                    WHERE s.condition_id = t.condition_id) AS window_start,
                   (SELECT MIN(s.market_end_ts)   FROM market_snapshots s
                    WHERE s.condition_id = t.condition_id) AS window_end
            FROM   trades_executed t
            LEFT JOIN market_resolutions r USING (condition_id)
            ORDER BY t.ts DESC
            """,
        ).fetchall()

    def trade_summary(self) -> Dict[str, Any]:
        """Aggregate counts and P&L across all trades (resolved and pending)."""
        row = self._conn.execute(
            """
            SELECT COUNT(*)                                              AS total,
                   SUM(CASE WHEN resolved_at IS NOT NULL  THEN 1 END)  AS resolved,
                   SUM(CASE WHEN result = 'positive'      THEN 1 END)  AS wins,
                   SUM(CASE WHEN result = 'negative'      THEN 1 END)  AS losses,
                   SUM(CASE WHEN result = 'arb'           THEN 1 END)  AS arb_trades,
                   SUM(CASE WHEN resolved_at IS NULL       THEN 1 END)  AS pending,
                   ROUND(SUM(COALESCE(pnl, 0)), 4)                     AS total_pnl
            FROM   trades_executed
            """
        ).fetchone()
        return dict(row) if row else {}

    # ── Trim ──────────────────────────────────────────────────────────────────

    def win_rates_by_trigger(self) -> List[Dict[str, Any]]:
        """
        Return resolved trade win rates grouped by trigger + outcome.
        Used by AdaptiveThresholds to tune edge requirements at runtime.
        """
        rows = self._conn.execute(
            """
            SELECT trigger,
                   outcome,
                   COUNT(*)                                             AS total,
                   SUM(CASE WHEN result = 'positive' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN result = 'negative' THEN 1 ELSE 0 END) AS losses,
                   ROUND(AVG(CASE WHEN result IN ('positive','negative')
                                  THEN pnl END), 4)                    AS avg_pnl
            FROM   trades_executed
            WHERE  resolved_at IS NOT NULL
            GROUP  BY trigger, outcome
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats_start_ts(self) -> str:
        """Return the timestamp from which trigger performance stats are tracked."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'stats_start_ts'"
        ).fetchone()
        return row["value"] if row else datetime.now(timezone.utc).isoformat()

    def has_trade_for_condition(self, condition_id: str) -> bool:
        """Return True if any trade has been recorded for this market window."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades_executed WHERE condition_id = ?",
            [condition_id],
        ).fetchone()
        return (row["n"] if row else 0) > 0

    def trigger_stats_since(self, start_ts: str) -> List[Dict[str, Any]]:
        """
        Return resolved trade counts (wins/losses) grouped by trigger and outcome,
        for trades placed on or after start_ts.
        """
        rows = self._conn.execute(
            """
            SELECT trigger,
                   outcome,
                   COUNT(*)                                              AS total,
                   SUM(CASE WHEN result = 'positive' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN result = 'negative' THEN 1 ELSE 0 END) AS losses
            FROM   trades_executed
            WHERE  ts >= ?
            GROUP  BY trigger, outcome
            ORDER  BY trigger, outcome
            """,
            [start_ts],
        ).fetchall()
        return [dict(r) for r in rows]

    def bucket_stats_since(self, start_ts: str) -> List[Dict[str, Any]]:
        """
        Return resolved trade counts (wins/losses) grouped by vol+trend bucket,
        for trades placed on or after start_ts.  Bucket is looked up from the
        matching decision_signal (same condition_id + trigger + outcome).
        Trades with no matching signal (rare) fall back to 'unknown'.
        """
        rows = self._conn.execute(
            """
            SELECT COALESCE(d.vol_bucket || '+' || d.trend_bucket, 'unknown') AS bucket,
                   t.trigger,
                   t.outcome,
                   COUNT(*)                                               AS total,
                   SUM(CASE WHEN t.result = 'positive' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN t.result = 'negative' THEN 1 ELSE 0 END) AS losses
            FROM   trades_executed t
            LEFT JOIN (
                SELECT condition_id, trigger, outcome,
                       vol_bucket, trend_bucket
                FROM   decision_signals
                GROUP  BY condition_id, trigger, outcome
            ) d ON d.condition_id = t.condition_id
                AND d.trigger    = t.trigger
                AND d.outcome    = t.outcome
            WHERE  t.ts >= ?
            GROUP  BY bucket, t.trigger, t.outcome
            ORDER  BY bucket, t.trigger, t.outcome
            """,
            [start_ts],
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats_start_ts_v2(self) -> str:
        """Return the v2 epoch start timestamp (independent of v1)."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'stats_start_ts_v2'"
        ).fetchone()
        return row["value"] if row else datetime.now(timezone.utc).isoformat()

    def trigger_stats_v2_since(self, start_ts: str) -> List[Dict[str, Any]]:
        """
        Trade stats grouped by symbol + trigger, with P&L totals and pending count.
        Used for the v2 trigger summary report.
        """
        rows = self._conn.execute(
            """
            SELECT symbol,
                   trigger,
                   COUNT(*)                                                AS total,
                   SUM(CASE WHEN result = 'positive'   THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN result = 'negative'   THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN resolved_at IS NULL    THEN 1 ELSE 0 END) AS pending,
                   ROUND(SUM(COALESCE(pnl, 0)), 4)                        AS total_pnl,
                   ROUND(AVG(CASE WHEN result IN ('positive','negative')
                                  THEN pnl END), 4)                       AS avg_pnl
            FROM   trades_executed
            WHERE  ts >= ?
            GROUP  BY symbol, trigger
            ORDER  BY symbol, trigger
            """,
            [start_ts],
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats_start_ts_v1_prod(self) -> str:
        """Return the V1.0 production epoch start timestamp."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'stats_start_ts_v1_prod'"
        ).fetchone()
        return row["value"] if row else datetime.now(timezone.utc).isoformat()

    def get_stats_start_ts_v3(self) -> str:
        """Return the V2.0 dev strategy epoch start timestamp."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'stats_start_ts_v3'"
        ).fetchone()
        return row["value"] if row else datetime.now(timezone.utc).isoformat()

    def get_stats_start_ts_v4(self) -> str:
        """Return the V3.0 dev strategy epoch start timestamp."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'stats_start_ts_v4'"
        ).fetchone()
        return row["value"] if row else datetime.now(timezone.utc).isoformat()

    def trigger_stats_for_strategy(
        self, start_ts: str, strategy: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Trade stats grouped by symbol + trigger, with P&L totals and pending count.
        Optionally filter by strategy_version.
        strategy='v1' also includes NULL (legacy trades pre-dating the column).
        """
        if strategy == "v1":
            where = "WHERE ts >= ? AND (strategy_version IS NULL OR strategy_version = 'v1')"
            params: List = [start_ts]
        elif strategy:
            where  = "WHERE ts >= ? AND strategy_version = ?"
            params = [start_ts, strategy]
        else:
            where  = "WHERE ts >= ?"
            params = [start_ts]
        rows = self._conn.execute(
            f"""
            SELECT symbol,
                   trigger,
                   COUNT(*)                                                AS total,
                   SUM(CASE WHEN result = 'positive'   THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN result = 'negative'   THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN resolved_at IS NULL    THEN 1 ELSE 0 END) AS pending,
                   ROUND(SUM(COALESCE(pnl, 0)), 4)                        AS total_pnl,
                   ROUND(AVG(CASE WHEN result IN ('positive','negative')
                                  THEN pnl END), 4)                       AS avg_pnl
            FROM   trades_executed
            {where}
            GROUP  BY symbol, trigger
            ORDER  BY symbol, trigger
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def market_pnl_summary_for_strategy(
        self, strategy: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Per-market-window P&L summary, optionally filtered by strategy_version.
        strategy='v1' includes NULL (legacy trades pre-dating the column).
        """
        if strategy == "v1":
            where = "WHERE (t.strategy_version IS NULL OR t.strategy_version = 'v1')"
        elif strategy:
            where = f"WHERE t.strategy_version = '{strategy}'"
        else:
            where = ""
        rows = self._conn.execute(
            f"""
            SELECT t.symbol,
                   t.condition_id,
                   MIN(t.ts)                                              AS first_trade_ts,
                   COUNT(*)                                               AS total_trades,
                   SUM(CASE WHEN t.resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                   SUM(CASE WHEN t.result = 'positive'     THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN t.result = 'negative'     THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN t.result = 'arb'          THEN 1 ELSE 0 END) AS arb_trades,
                   ROUND(SUM(COALESCE(t.pnl, 0)), 4)                      AS total_pnl,
                   r.winning_outcome,
                   (SELECT s.slug FROM market_snapshots s
                    WHERE  s.condition_id = t.condition_id
                      AND  s.slug IS NOT NULL
                    LIMIT  1)                                             AS slug
            FROM   trades_executed t
            LEFT JOIN market_resolutions r USING (condition_id)
            {where}
            GROUP  BY t.symbol, t.condition_id
            ORDER  BY t.symbol, first_trade_ts DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def market_pnl_summary(self) -> List[Dict[str, Any]]:
        """
        Per-market-window P&L summary.  Returns one row per (symbol, condition_id)
        with trade counts, wins, losses, total realised P&L, and slug.
        Slug is looked up from market_snapshots (may be NULL if snapshots were trimmed).
        """
        rows = self._conn.execute(
            """
            SELECT t.symbol,
                   t.condition_id,
                   MIN(t.ts)                                              AS first_trade_ts,
                   COUNT(*)                                               AS total_trades,
                   SUM(CASE WHEN t.resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                   SUM(CASE WHEN t.result = 'positive'     THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN t.result = 'negative'     THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN t.result = 'arb'          THEN 1 ELSE 0 END) AS arb_trades,
                   ROUND(SUM(COALESCE(t.pnl, 0)), 4)                      AS total_pnl,
                   r.winning_outcome,
                   (SELECT s.slug FROM market_snapshots s
                    WHERE  s.condition_id = t.condition_id
                      AND  s.slug IS NOT NULL
                    LIMIT  1)                                             AS slug
            FROM   trades_executed t
            LEFT JOIN market_resolutions r USING (condition_id)
            GROUP  BY t.symbol, t.condition_id
            ORDER  BY t.symbol, first_trade_ts DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def has_trade_for_condition_outcome(self, condition_id: str, outcome: str) -> bool:
        """Return True if a trade already exists for this condition_id and outcome."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades_executed "
            "WHERE condition_id = ? AND outcome = ?",
            [condition_id, outcome],
        ).fetchone()
        return (row["n"] if row else 0) > 0

    def has_non_preopen_trade_for_condition(self, condition_id: str) -> bool:
        """Return True if any non-pre_open trade exists for this condition_id.
        Allows forced/directional signals to fire even after pre_open legs filled."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades_executed "
            "WHERE condition_id = ? AND trigger != 'pre_open'",
            [condition_id],
        ).fetchone()
        return (row["n"] if row else 0) > 0

    def snapshots_since(self, symbol: str, since_ts: str) -> List[Dict[str, Any]]:
        """
        Return all market_snapshots for *symbol* with ts > since_ts, ordered by ts.
        Used by the CSV exporter to append only new rows.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM market_snapshots
            WHERE  symbol = ? AND ts > ?
            ORDER  BY ts
            """,
            [symbol, since_ts],
        ).fetchall()
        return [dict(r) for r in rows]

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", [key]
        ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [key, value],
        )
        self._conn.commit()

    def opposing_entries_this_window(self, condition_id: str, outcome: str) -> int:
        """
        Count trades already placed for this condition_id in the OPPOSITE direction.
        Used to prevent simultaneous UP+DOWN bets on the same market window.
        """
        opposite = "DOWN" if outcome == "UP" else "UP"
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades_executed "
            "WHERE condition_id = ? AND outcome = ? AND resolved_at IS NULL",
            [condition_id, opposite],
        ).fetchone()
        return row["n"] if row else 0

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
