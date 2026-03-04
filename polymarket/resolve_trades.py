#!/usr/bin/env python3
"""
Standalone trade resolution script — run directly on the server.

Looks up every unresolved trade in the SQLite database, fetches the final
settlement price from the Polymarket DATA API (with a Gamma API fallback for
condition_ids that have no snapshot rows), then writes the result and P&L.

Usage (on server):
    python3 /root/polymarket/resolve_trades.py

    # Override DB path:
    DB_PATH=~/polymarket.db python3 /root/polymarket/resolve_trades.py

The script is idempotent — safe to run multiple times.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import urllib.request
import urllib.parse
import json
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"

DB_PATH   = Path(os.getenv("DB_PATH", Path.home() / "polymarket.db")).expanduser()

# Age threshold: only resolve markets older than this many seconds
MIN_AGE_SECONDS = 1200   # 20 minutes


def _get(url: str, timeout: int = 12) -> dict | list:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _find_up_token_from_gamma(cid: str) -> str | None:
    """Query Gamma API for the UP outcome token_id of a condition."""
    url = f"{GAMMA_API}/markets?condition_id={urllib.parse.quote(cid)}"
    try:
        data = _get(url)
    except Exception as exc:
        print(f"  [warn] Gamma API error for {cid[:12]}…: {exc}")
        return None

    mkts = data if isinstance(data, list) else data.get("markets", [])
    for mkt in mkts:
        for tok in mkt.get("tokens", []):
            if tok.get("outcome", "").strip().upper() in ("UP",):
                return tok.get("token_id") or tok.get("tokenId")
    return None


def _last_trade_price(token_id: str) -> float | None:
    """Fetch the last traded price for a token from the DATA API."""
    url = f"{DATA_API}/last-trade-price?token_id={urllib.parse.quote(token_id)}"
    try:
        data = _get(url)
        price = data.get("price")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"  [warn] DATA API error ({token_id[:12]}…): {exc}")
        return None


def _record_resolution(conn: sqlite3.Connection, cid: str, symbol: str,
                        winner: str, up_price: float) -> int:
    """
    Back-fill trades_executed for this condition_id.

    P&L formula (Polymarket binary markets):
      Win:  profit = (1.0 - entry_price) * size
      Loss: loss   = -entry_price * size
      ARB:  profit = (1.0 - entry_price - 0.03) * size  (round-trip fee deducted)
    """
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "SELECT id, outcome, entry_price, size, trigger FROM trades_executed "
        "WHERE condition_id = ? AND resolved_at IS NULL",
        (cid,),
    )
    rows = cur.fetchall()
    count = 0
    for row in rows:
        trade_id    = row[0]
        outcome     = row[1]          # "UP" or "DOWN"
        entry_price = float(row[2])
        size        = float(row[3])
        trigger     = row[4] or ""

        if trigger == "arb":
            result = "arb"
            pnl    = (1.0 - entry_price - 0.03) * size
        elif outcome == winner:
            result = "positive"
            pnl    = (1.0 - entry_price) * size
        else:
            result = "negative"
            pnl    = -entry_price * size

        conn.execute(
            """UPDATE trades_executed
               SET resolved_at   = ?,
                   market_winner = ?,
                   result        = ?,
                   pnl           = ?
               WHERE id = ?""",
            (now, winner, result, round(pnl, 6), trade_id),
        )
        pnl_str = f"+${pnl:.4f}" if pnl >= 0 else f"-${abs(pnl):.4f}"
        icon = "✅" if result == "positive" else ("💰" if result == "arb" else "❌")
        print(f"  {icon}  trade #{trade_id}: {outcome} @ {entry_price:.4f} × ${size:.2f} "
              f"→ {result.upper()} {pnl_str}")
        count += 1

    conn.commit()
    return count


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # ── Find unresolved trades ─────────────────────────────────────────────────
    rows = conn.execute(
        """
        SELECT DISTINCT
               t.condition_id,
               t.symbol,
               MIN(t.ts) AS first_trade_ts,
               (SELECT s.token_id_up FROM market_snapshots s
                WHERE  s.condition_id = t.condition_id
                  AND  s.token_id_up IS NOT NULL
                LIMIT  1) AS token_id_up
        FROM   trades_executed t
        WHERE  t.resolved_at IS NULL
        GROUP  BY t.condition_id
        """
    ).fetchall()

    if not rows:
        print("Nothing to resolve — all trades are already settled.")
        conn.close()
        return

    now = datetime.now(timezone.utc)
    print(f"Found {len(rows)} unresolved condition_id(s).\n")

    total_resolved = 0

    for row in rows:
        cid         = row["condition_id"]
        symbol      = row["symbol"] or "?"
        token_id_up = row["token_id_up"]

        # Compute age
        try:
            age = (now - datetime.fromisoformat(
                row["first_trade_ts"].replace("Z", "+00:00")
            )).total_seconds()
        except Exception:
            age = 99999

        print(f"┌─ {symbol}  {cid[:16]}…  (age: {age/60:.1f} min)")

        if age < MIN_AGE_SECONDS:
            print(f"└─ skipping — market is only {age/60:.1f} min old (< 20 min)\n")
            continue

        # ── Get UP token id ────────────────────────────────────────────────────
        if token_id_up:
            print(f"│  token_id_up from DB snapshot: {token_id_up[:20]}…")
        else:
            print(f"│  No snapshot row — querying Gamma API…")
            token_id_up = _find_up_token_from_gamma(cid)
            if not token_id_up:
                print(f"└─ SKIP: could not find UP token for {cid[:12]}…\n")
                continue
            print(f"│  token_id_up from Gamma API: {token_id_up[:20]}…")

        # ── Get last traded price ──────────────────────────────────────────────
        up_price = _last_trade_price(token_id_up)
        if up_price is None:
            print(f"└─ SKIP: DATA API returned no price\n")
            continue

        print(f"│  last trade price: UP={up_price:.4f}  DOWN≈{1-up_price:.4f}")

        # ── Determine winner ──────────────────────────────────────────────────
        if up_price >= 0.90:
            winner = "UP"
            reason = "UP ≥ 0.90 (clear settlement)"
        elif up_price <= 0.10:
            winner = "DOWN"
            reason = "UP ≤ 0.10 (clear settlement)"
        else:
            # Force-resolve for old trades — take the higher side
            winner = "UP" if up_price >= 0.50 else "DOWN"
            reason = f"age={age/60:.0f}min, force-resolve ({up_price:.4f} → {winner})"

        print(f"│  winner: {winner}  ({reason})")

        # ── Write to DB ────────────────────────────────────────────────────────
        n = _record_resolution(conn, cid, symbol, winner, up_price)
        print(f"└─ resolved {n} trade(s)\n")
        total_resolved += n

    print(f"\nDone. Resolved {total_resolved} trade(s) total.")
    conn.close()


if __name__ == "__main__":
    main()
