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
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from rich.console import Console

from .collector import CollectorAgent, MarketState, run_display
from .config import (
    AUTO_RESTART_HOURS,
    CLOB_API,
    DATA_API,
    DB_PATH,
    DB_RETENTION_HOURS,
    DB_TRIM_INTERVAL,
    GAMMA_API,
    POLY_API_KEY,
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

                # Use analytics snapshot prices (computed from order books) when
                # available; fall back to token prices only when snap is missing.
                up_p = (snap.up_price   if snap else None) or \
                       (mkt.up_token.price   if mkt.up_token   else None)
                dn_p = (snap.down_price if snap else None) or \
                       (mkt.down_token.price if mkt.down_token else None)
                # If DOWN mirrors UP (stale order book), derive it from UP.
                # In a binary market: UP + DOWN ≈ 1.0
                if up_p and dn_p and abs(up_p - dn_p) < 0.005:
                    dn_p = round(1.0 - up_p, 4)

                row = {
                    "ts":               ts,
                    "symbol":           symbol,
                    "condition_id":     mkt.condition_id,
                    "token_id_up":      mkt.up_token.token_id   if mkt.up_token   else None,
                    "token_id_down":    mkt.down_token.token_id if mkt.down_token else None,
                    # Prices (corrected)
                    "up_price":         round(up_p, 4) if up_p else None,
                    "down_price":       round(dn_p, 4) if dn_p else None,
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
                    "slug":             slug,
                }
                db.insert_snapshot(row)
        except Exception as exc:
            console.log(f"[yellow]Snapshot loop error: {exc}[/yellow]")


async def _resolution_loop(state: MarketState, db: Database) -> None:
    """
    Check for expired markets every 30 seconds and record their outcome.

    Resolution tiers (applied in order):
      1. Clear settlement  — UP ≥ 0.85 or UP ≤ 0.15  (price firmly resolved)
      2. Strong signal     — UP ≥ 0.70 or UP ≤ 0.30  (only after 2 min expiry)
      3. Force resolve     — take whichever side is higher after 5 min
         (guards against stale order books that never fully settle)
    All tiers are skipped while the market is still active.
    """
    seen: set[str] = set()
    while True:
        await asyncio.sleep(30)
        try:
            now = datetime.now(timezone.utc)
            for slug in SLUGS:
                symbol = SYMBOL_MAP.get(slug, slug)
                for mkt in list(state.markets.get(slug, [])):
                    if mkt.end_time > now:
                        continue  # still active
                    if mkt.condition_id in seen:
                        continue  # already recorded

                    up_price = mkt.up_token.price  if mkt.up_token  else None
                    dn_price = mkt.down_token.price if mkt.down_token else None
                    if up_price is None:
                        continue

                    expired_secs = (now - mkt.end_time).total_seconds()
                    winner: Optional[str] = None

                    if up_price >= 0.85:
                        winner = "UP"
                    elif up_price <= 0.15:
                        winner = "DOWN"
                    elif expired_secs >= 120 and up_price >= 0.70:
                        winner = "UP"
                    elif expired_secs >= 120 and up_price <= 0.30:
                        winner = "DOWN"
                    elif expired_secs >= 300:
                        # Force-resolve: whichever token has the higher price wins
                        winner = "UP" if up_price >= 0.50 else "DOWN"

                    if winner is None:
                        continue

                    final_dn = dn_price if (dn_price and abs(dn_price - up_price) > 0.01) \
                               else round(1.0 - up_price, 4)
                    db.record_resolution(
                        condition_id     = mkt.condition_id,
                        symbol           = symbol,
                        winning_outcome  = winner,
                        final_up_price   = up_price,
                        final_down_price = final_dn,
                    )
                    seen.add(mkt.condition_id)
                    tier = ("clear" if up_price >= 0.85 or up_price <= 0.15 else
                            "strong" if expired_secs < 300 else "forced")
                    console.log(
                        f"[cyan]Resolution [{tier}]: {symbol} [{slug}] "
                        f"{mkt.condition_id[:12]}… → {winner} "
                        f"(UP={up_price:.3f}, expired {expired_secs:.0f}s ago)[/cyan]"
                    )
        except Exception as exc:
            console.log(f"[yellow]Resolution loop error: {exc}[/yellow]")


async def _api_resolution_loop(db: Database) -> None:
    """
    Every 2 minutes, resolve any trades still pending in SQLite.

    Strategy (all public endpoints, no auth needed):
    1. Query unresolved trades older than 20 min.
    2. GET gamma-api.polymarket.com/markets?condition_id=<cid>
       - If resolved=true, outcomePrices directly gives the winner.
       - Also extracts token_id_up for the CLOB fallback.
    3. Fallback: GET clob.polymarket.com/last-trade-price?token_id=<up>
       - UP >= 0.90 → UP wins; <= 0.10 → DOWN wins.
       - After 30 min: force-resolve to whichever side is higher.
    """
    while True:
        await asyncio.sleep(120)
        try:
            rows = db._conn.execute(
                """
                SELECT DISTINCT t.condition_id,
                       t.symbol,
                       MIN(t.ts)   AS first_trade_ts,
                       (SELECT s.token_id_up FROM market_snapshots s
                        WHERE  s.condition_id = t.condition_id
                          AND  s.token_id_up IS NOT NULL
                        LIMIT 1)  AS token_id_up
                FROM   trades_executed t
                WHERE  t.resolved_at IS NULL
                GROUP  BY t.condition_id
                """
            ).fetchall()

            if not rows:
                continue

            async with aiohttp.ClientSession() as session:
                for row in rows:
                    cid           = row["condition_id"]
                    symbol        = row["symbol"] or "?"
                    snap_token_up = row["token_id_up"]

                    try:
                        age = (
                            datetime.now(timezone.utc) -
                            datetime.fromisoformat(
                                row["first_trade_ts"].replace("Z", "+00:00")
                            )
                        ).total_seconds()
                    except Exception:
                        age = 9999

                    if age < 1200:
                        continue

                    winner      = None
                    up_price    = None
                    token_id_up = snap_token_up

                    # ── Step 1: Gamma API — outcomePrices for settled markets ─
                    try:
                        url = f"{GAMMA_API}/markets?condition_id={cid}"
                        async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=10)
                        ) as r:
                            data = await r.json()
                        mkts = data if isinstance(data, list) else data.get("markets", [])
                        if mkts:
                            mkt    = mkts[0]
                            tokens = mkt.get("tokens", [])
                            # Always grab token_id_up if we don't have it
                            if not token_id_up:
                                for tok in tokens:
                                    if tok.get("outcome", "").upper() == "UP":
                                        token_id_up = tok.get("token_id") or tok.get("tokenId")
                                        break
                            # Check for settlement
                            if mkt.get("resolved") or mkt.get("resolutionTime"):
                                outcome_prices = mkt.get("outcomePrices", [])
                                for i, tok in enumerate(tokens):
                                    if tok.get("outcome", "").upper() == "UP" and i < len(outcome_prices):
                                        up_price = float(outcome_prices[i])
                                        winner   = "UP" if up_price >= 0.5 else "DOWN"
                                        break
                    except Exception as exc:
                        console.log(f"[yellow]Gamma API ({cid[:8]}…): {exc}[/yellow]")

                    # ── Step 2: CLOB last-trade-price fallback ────────────────
                    if winner is None and token_id_up:
                        try:
                            url = f"{CLOB_API}/last-trade-price?token_id={token_id_up}"
                            async with session.get(
                                url, timeout=aiohttp.ClientTimeout(total=10)
                            ) as r:
                                data     = await r.json()
                                up_price = float(data.get("price", 0))

                            if up_price >= 0.90:
                                winner = "UP"
                            elif up_price <= 0.10:
                                winner = "DOWN"
                            elif age >= 1800:
                                winner = "UP" if up_price >= 0.50 else "DOWN"
                        except Exception as exc:
                            console.log(f"[yellow]CLOB price ({cid[:8]}…): {exc}[/yellow]")

                    if winner is None:
                        continue

                    db.record_resolution(
                        condition_id     = cid,
                        symbol           = symbol,
                        winning_outcome  = winner,
                        final_up_price   = up_price if up_price else 0.0,
                        final_down_price = round(1.0 - (up_price or 0.0), 4),
                    )
                    console.log(
                        f"[green]API resolution: {symbol} {cid[:12]}… "
                        f"→ {winner} (UP={up_price:.4f}, age={age:.0f}s)[/green]"
                    )
        except Exception as exc:
            console.log(f"[yellow]API resolution loop error: {exc}[/yellow]")


async def _fetch_clob_balance(session: aiohttp.ClientSession, _l2_headers) -> Optional[float]:
    """
    Query GET /balance on the CLOB API and return the USDC balance as a float.

    Returns None if the request fails or credentials are not set.
    The endpoint returns JSON like {"balance": "13.210000"}.
    """
    try:
        headers = {
            "Content-Type": "application/json",
            **_l2_headers("GET", "/balance"),
        }
        async with session.get(
            f"{CLOB_API}/balance",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                console.log(f"[yellow]Balance fetch returned {r.status}[/yellow]")
                return None
            data = await r.json()
            # Response may be {"balance": "13.21"} or just a string/number
            if isinstance(data, dict):
                raw = data.get("balance") or data.get("USDC") or data.get("usdc")
            else:
                raw = data
            return float(raw) if raw is not None else None
    except Exception as exc:
        console.log(f"[yellow]Balance fetch error: {exc}[/yellow]")
        return None


async def _redeem_loop(book: Optional["PositionBook"] = None) -> None:
    """
    Every 10 minutes, redeem all redeemable winning positions via the CLOB API
    and sync the PositionBook balance with the actual on-chain USDC balance.

    Only active in live mode (POLY_API_KEY must be set).  In paper mode the
    coroutine exits immediately so it does not take up an asyncio slot.

    Polymarket endpoints:
        GET  https://clob.polymarket.com/redeemable-positions
        POST https://clob.polymarket.com/redeem-positions
             Body: {"conditionIds": ["0x…", …]}
        GET  https://clob.polymarket.com/balance
    Uses the same L2 HMAC-SHA256 auth headers as order submission.
    """
    if not POLY_API_KEY:
        console.log("[dim]Redeem loop: paper mode — auto-redeem disabled[/dim]")
        return

    from .trader import _l2_headers  # reuse existing HMAC auth helper

    REDEEM_INTERVAL = 600   # 10 minutes

    while True:
        try:
            # Ask the CLOB for positions that are currently redeemable
            async with aiohttp.ClientSession() as session:
                headers_get = {
                    "Content-Type": "application/json",
                    **_l2_headers("GET", "/redeemable-positions"),
                }
                async with session.get(
                    f"{CLOB_API}/redeemable-positions",
                    headers=headers_get,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    ok = r.status == 200
                    if not ok:
                        console.log(f"[yellow]Redeem: /redeemable-positions returned {r.status}[/yellow]")
                    redeemable = await r.json() if ok else []

                if ok:
                    # redeemable is a list of objects; extract conditionIds
                    if isinstance(redeemable, dict):
                        redeemable = redeemable.get("data", []) or redeemable.get("positions", [])

                    condition_ids = list({
                        item.get("conditionId") or item.get("condition_id")
                        for item in (redeemable or [])
                        if item.get("conditionId") or item.get("condition_id")
                    })

                    if not condition_ids:
                        # No positions to redeem — still sync balance so portfolio stays current
                        if book is not None:
                            bal = await _fetch_clob_balance(session, _l2_headers)
                            if bal is not None:
                                book.balance = bal
                    else:
                        console.log(f"[cyan]Redeem: {len(condition_ids)} condition_id(s) redeemable[/cyan]")

                        body_s  = json.dumps({"conditionIds": condition_ids})
                        headers_post = {
                            "Content-Type": "application/json",
                            **_l2_headers("POST", "/redeem-positions", body_s),
                        }
                        async with session.post(
                            f"{CLOB_API}/redeem-positions",
                            data=body_s,
                            headers=headers_post,
                            timeout=aiohttp.ClientTimeout(total=20),
                        ) as r:
                            resp = await r.json()
                            if r.status == 200:
                                console.log(f"[green]Redeem successful: {resp}[/green]")
                                # Sync the local PositionBook with the actual on-chain balance
                                if book is not None:
                                    bal = await _fetch_clob_balance(session, _l2_headers)
                                    if bal is not None:
                                        old = book.balance
                                        book.balance = bal
                                        console.log(
                                            f"[green]Portfolio balance updated: "
                                            f"${old:.2f} → ${bal:.2f}[/green]"
                                        )
                            else:
                                console.log(f"[yellow]Redeem POST returned {r.status}: {resp}[/yellow]")

        except Exception as exc:
            console.log(f"[yellow]Redeem loop error: {exc}[/yellow]")

        await asyncio.sleep(REDEEM_INTERVAL)


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


async def _feedback_log_loop(decision: "DecisionEngine") -> None:
    """
    Every 5 minutes, log the current adaptive threshold state so operators
    can see which trigger+direction combos have been suppressed or adjusted.
    """
    from .config import AUTO_RESTART_HOURS   # avoid circular at module level
    while True:
        await asyncio.sleep(300)
        try:
            if decision.adaptive:
                for line in decision.adaptive.summary_lines():
                    console.log(line)
        except Exception as exc:
            console.log(f"[yellow]Feedback log error: {exc}[/yellow]")


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
                _resolution_loop(state, db),
                _api_resolution_loop(db),
                _redeem_loop(book=None),
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
            _api_resolution_loop(db),
            _redeem_loop(book=book),
            _trim_loop(db),
            _auto_restart_loop(AUTO_RESTART_HOURS),
            _feedback_log_loop(decision),
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
