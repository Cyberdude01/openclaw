"""
Polymarket 15M Crypto Market System — Orchestrator

Launches five concurrent tasks:
  Agent 1 - CollectorAgent:  Fetches live market data, order books, trades
  Agent 2 - DecisionEngine:  Analyses data and emits TradeSignals
  Agent 3 - TraderAgent:     Executes orders (paper or live)
  Agent 4 - DataExporter:    Pushes snapshots to GitHub every 5 min
  Task  5 - DB snapshot loop: Writes market state to SQLite every 60 seconds

Additional background tasks:
  - DB trim:   Deletes rows older than DB_RETENTION_HOURS once per hour
  - Resolution checker: Detects expired markets and records outcomes in DB
  - Auto-restart: Restarts the process after AUTO_RESTART_HOURS (if set)

Usage
-----
  # Full pipeline — paper trade (default, no credentials needed):
  python -m polymarket

  # Data collection only (no signals / trades):
  python -m polymarket --data-only

  # Live trading (requires env vars):
  POLY_PRIVATE_KEY=<key> POLY_ADDRESS=<addr> \\
  POLY_API_KEY=<key> POLY_API_SECRET=<secret> POLY_API_PASSPHRASE=<pass> \\
  python -m polymarket

Environment Variables
---------------------
  POLY_PRIVATE_KEY     Ethereum private key (0x-prefixed)
  POLY_ADDRESS         Polygon wallet address
  POLY_API_KEY         Polymarket CLOB API key
  POLY_API_SECRET      CLOB API secret (base64)
  POLY_API_PASSPHRASE  CLOB API passphrase

  GITHUB_TOKEN         Token with write scope for Bob repo export
  EXPORT_REPO          Target repo URL  (default: Cyberdude01/Bob)
  EXPORT_INTERVAL      Push cadence in seconds (default: 300)

  DB_PATH              SQLite file path (default: ~/polymarket.db)
  DB_RETENTION_HOURS   How many hours of snapshots to keep locally (default: 36)
  AUTO_RESTART_HOURS   Restart process after this many hours; 0 = disabled
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console

from .collector import CollectorAgent, MarketState, run_display
from .config import (
    AUTO_RESTART_HOURS,
    DB_PATH,
    DB_RETENTION_HOURS,
    DB_TRIM_INTERVAL,
    SNAPSHOT_INTERVAL,
    SLUGS,
    SYMBOL_MAP,
)
from .database import Database
from .decision  import DecisionEngine, PositionBook
from .exporter  import DataExporter
from .trader    import TraderAgent

console = Console()


# ─── Background tasks ─────────────────────────────────────────────────────────

async def _snapshot_loop(state: MarketState, db: Database) -> None:
    """Write a market snapshot to SQLite every SNAPSHOT_INTERVAL seconds."""
    while True:
        await asyncio.sleep(SNAPSHOT_INTERVAL)
        try:
            ts = datetime.now(timezone.utc).isoformat()
            for slug in SLUGS:
                symbol = SYMBOL_MAP.get(slug, slug)
                mkt    = state.get_current_market(slug)
                snap   = state.analytics.get(symbol).last_snapshot
                if not mkt:
                    continue

                # Count UP vs DOWN trades for this window
                trades    = state.recent_trades.get(slug, [])
                up_count  = sum(1 for t in trades if hasattr(t, "outcome") and t.outcome and t.outcome.value == "UP")
                dn_count  = sum(1 for t in trades if hasattr(t, "outcome") and t.outcome and t.outcome.value == "DOWN")

                up_ob   = mkt.up_token.order_book   if mkt.up_token   else None
                dn_ob   = mkt.down_token.order_book if mkt.down_token else None

                row = {
                    "ts":               ts,
                    "symbol":           symbol,
                    "condition_id":     mkt.condition_id,
                    "token_id_up":      mkt.up_token.token_id   if mkt.up_token   else None,
                    "token_id_down":    mkt.down_token.token_id if mkt.down_token else None,
                    # Prices
                    "up_price":         mkt.up_token.price   if mkt.up_token   else None,
                    "down_price":       mkt.down_token.price if mkt.down_token else None,
                    "up_best_bid":      up_ob.best_bid   if up_ob else None,
                    "up_best_ask":      up_ob.best_ask   if up_ob else None,
                    "down_best_bid":    dn_ob.best_bid   if dn_ob else None,
                    "down_best_ask":    dn_ob.best_ask   if dn_ob else None,
                    "up_spread":        up_ob.spread     if up_ob else None,
                    "down_spread":      dn_ob.spread     if dn_ob else None,
                    "up_bid_depth":     up_ob.bid_depth  if up_ob else None,
                    "up_ask_depth":     up_ob.ask_depth  if up_ob else None,
                    "down_bid_depth":   dn_ob.bid_depth  if dn_ob else None,
                    "down_ask_depth":   dn_ob.ask_depth  if dn_ob else None,
                    # Window state
                    "elapsed_pct":      round(mkt.elapsed_pct, 4),
                    "remaining_sec":    round(mkt.remaining_seconds, 1),
                    "arb_profit":       mkt.arb_opportunity,
                    # Trade counts
                    "up_trade_count":   up_count,
                    "down_trade_count": dn_count,
                    "total_volume":     mkt.volume,
                    # Analytics
                    "vol_bucket":       snap.vol_bucket.value   if snap else None,
                    "trend_bucket":     snap.trend_bucket.value if snap else None,
                    "rv60":             round(snap.rv60,  6)    if snap else None,
                    "eff60":            round(snap.eff60, 4)    if snap else None,
                    "prob_up":          round(snap.dir_60pct, 4) if snap else None,
                    "dir_60pct":        round(snap.dir_60pct, 4) if snap else None,
                    "dir_80pct":        round(snap.dir_80pct, 4) if snap else None,
                    "dir_90pct":        round(snap.dir_90pct, 4) if snap else None,
                    "prob_008":         round(snap.prob_008, 4) if snap else None,
                    "prob_012":         round(snap.prob_012, 4) if snap else None,
                    "prob_020":         round(snap.prob_020, 4) if snap else None,
                    "market_start_ts":  mkt.start_time.isoformat(),
                    "market_end_ts":    mkt.end_time.isoformat(),
                }
                db.insert_snapshot(row)
        except Exception as exc:
            console.log(f"[yellow]Snapshot loop error: {exc}[/yellow]")


async def _resolution_loop(state: MarketState, db: Database) -> None:
    """
    Check for expired markets every 60 seconds and record their outcome.

    Resolution heuristic: once a market's end_time has passed, if the UP token
    price is ≥ 0.80 we call it UP; if ≤ 0.20 we call it DOWN.  Ambiguous
    markets (0.20 < price < 0.80) are skipped until prices settle.
    """
    seen: set[str] = set()
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(timezone.utc)
            for slug in SLUGS:
                symbol = SYMBOL_MAP.get(slug, slug)
                for mkt in state.markets.get(slug, []):
                    if mkt.end_time > now:
                        continue  # still active
                    if mkt.condition_id in seen:
                        continue  # already recorded

                    up_price = mkt.up_token.price if mkt.up_token else None
                    dn_price = mkt.down_token.price if mkt.down_token else None
                    if up_price is None:
                        continue

                    if up_price >= 0.80:
                        winner = "UP"
                    elif up_price <= 0.20:
                        winner = "DOWN"
                    else:
                        continue  # price not settled yet

                    db.record_resolution(
                        condition_id    = mkt.condition_id,
                        symbol          = symbol,
                        winning_outcome = winner,
                        final_up_price  = up_price,
                        final_down_price= dn_price or (1.0 - up_price),
                    )
                    seen.add(mkt.condition_id)
                    console.log(
                        f"[cyan]Resolution recorded: {symbol} {mkt.condition_id[:8]}… "
                        f"→ {winner} (UP={up_price:.3f})[/cyan]"
                    )
        except Exception as exc:
            console.log(f"[yellow]Resolution loop error: {exc}[/yellow]")


async def _trim_loop(db: Database) -> None:
    """Trim old market_snapshots and decision_signals once per hour."""
    while True:
        await asyncio.sleep(DB_TRIM_INTERVAL)
        try:
            deleted = db.trim(DB_RETENTION_HOURS)
            if deleted:
                console.log(f"[dim]DB trim: removed {deleted} old snapshot rows[/dim]")
        except Exception as exc:
            console.log(f"[yellow]DB trim error: {exc}[/yellow]")


async def _auto_restart_loop(hours: float) -> None:
    """
    Restart the process after `hours` hours.

    This guards against long-running state drift (e.g. stale WebSocket,
    memory growth, or analytics warm-up issues).  Systemd will bring it
    back up automatically via the Restart=always policy.
    """
    if hours <= 0:
        return
    console.log(f"[dim]Auto-restart scheduled in {hours}h[/dim]")
    await asyncio.sleep(hours * 3600)
    console.log(f"[yellow]Auto-restart triggered after {hours}h — restarting…[/yellow]")
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ─── Main entry point ─────────────────────────────────────────────────────────

async def main(data_only: bool = False):
    console.print("[bold cyan]Polymarket 15M Market System starting…[/bold cyan]")

    # ── Database ──────────────────────────────────────────────────────────────
    db = Database(DB_PATH)
    console.print(f"[dim]SQLite: {DB_PATH}  (retention={DB_RETENTION_HOURS}h)[/dim]")

    # ── Shared State ──────────────────────────────────────────────────────────
    state        = MarketState()
    signal_queue = asyncio.Queue(maxsize=64)

    # ── Agent 1: Data Collector ───────────────────────────────────────────────
    collector = CollectorAgent(state)

    if data_only:
        console.print("[dim]Data-only mode — trading agents disabled[/dim]")
        exporter = DataExporter(state, db=db)
        try:
            await asyncio.gather(
                collector.run(),
                run_display(state),
                exporter.run(),
                _snapshot_loop(state, db),
                _trim_loop(db),
                _auto_restart_loop(AUTO_RESTART_HOURS),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            db.close()
        return

    # ── Agent 2: Decision Engine ───────────────────────────────────────────────
    book     = PositionBook(initial_balance=0.0)
    decision = DecisionEngine(state, book, db=db)

    # ── Agent 3: Trade Executor ────────────────────────────────────────────────
    trader = TraderAgent(state, book, signal_queue, db=db)

    # ── Agent 4: GitHub Data Exporter ─────────────────────────────────────────
    exporter = DataExporter(
        state,
        book       = book,
        signal_log = decision.signal_log,
        exec_log   = trader.execution_log,
        db         = db,
    )

    console.print(
        "[bold]Agents:[/bold]\n"
        "  [green]1[/green] CollectorAgent  — live market data + order books\n"
        "  [green]2[/green] DecisionEngine  — signal generation (paper trade)\n"
        "  [green]3[/green] TraderAgent     — order execution\n"
        "  [green]4[/green] DataExporter    — GitHub snapshot push every 5 min\n"
        "  [green]5[/green] DB snapshot     — SQLite write every 60 s\n"
        "  [green]6[/green] Resolution      — market outcome detection\n"
        "  [green]7[/green] DB trim         — prune data older than "
        f"{DB_RETENTION_HOURS}h every hour\n"
    )

    try:
        await asyncio.gather(
            collector.run(),
            decision.run(signal_queue, interval=2.0),
            trader.run(),
            run_display(
                state,
                signal_log = decision.signal_log,
                exec_log   = trader.execution_log,
                book       = book,
            ),
            exporter.run(),
            _snapshot_loop(state, db),
            _resolution_loop(state, db),
            _trim_loop(db),
            _auto_restart_loop(AUTO_RESTART_HOURS),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        trader.print_summary()
        db.close()


def cli():
    data_only = "--data-only" in sys.argv
    asyncio.run(main(data_only=data_only))


if __name__ == "__main__":
    cli()
