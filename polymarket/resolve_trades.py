#!/usr/bin/env python3
"""
Standalone trade resolution script — run directly on the server.

Looks up every unresolved trade in the SQLite database and resolves it using
the Polymarket Gamma API (outcomePrices field) for settled markets, falling
back to the CLOB API last-trade-price for markets not yet marked resolved.

Strategy (no auth required — all public endpoints):
  1. GET gamma-api.polymarket.com/markets?condition_id=<cid>
     → if resolved=true, outcomePrices["1"/"0"] tells us the winner directly
  2. If not yet settled, GET clob.polymarket.com/last-trade-price?token_id=<up>
     → UP >= 0.90 wins UP; <= 0.10 wins DOWN; older than 30 min → higher side

Usage (on server):
    python3 /root/polymarket/resolve_trades.py

    # Override DB path:
    DB_PATH=~/polymarket.db python3 /root/polymarket/resolve_trades.py

The script is idempotent — safe to run multiple times.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

DB_PATH         = Path(os.getenv("DB_PATH", Path.home() / "polymarket.db")).expanduser()
MIN_AGE_SECONDS = 1200   # skip markets less than 20 min old


def _get(url: str, timeout: int = 12) -> dict | list:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _resolve_from_gamma(cid: str) -> tuple[str | None, float | None, str | None]:
    """
    Query the Gamma API for a market by condition_id.

    Returns (winner, up_price, token_id_up) where winner is "UP" or "DOWN",
    or (None, None, token_id_up) if the market is not yet settled.
    token_id_up may still be returned even when the market is unresolved.
    """
    url = f"{GAMMA_API}/markets?condition_id={urllib.parse.quote(cid)}"
    try:
        data = _get(url)
    except Exception as exc:
        print(f"  [warn] Gamma API error: {exc}")
        return None, None, None

    mkts = data if isinstance(data, list) else data.get("markets", [])
    if not mkts:
        print(f"  [warn] Gamma API: no market found for condition_id {cid[:16]}…")
        return None, None, None

    mkt = mkts[0]

    # Extract token info (for CLOB fallback)
    token_id_up = None
    up_idx      = None
    tokens = mkt.get("tokens", [])
    for i, tok in enumerate(tokens):
        if tok.get("outcome", "").strip().upper() == "UP":
            token_id_up = tok.get("token_id") or tok.get("tokenId")
            up_idx = i
            break

    # Check if the market is already resolved
    if mkt.get("resolved") or mkt.get("resolutionTime"):
        outcome_prices = mkt.get("outcomePrices", [])
        # outcomePrices[i] corresponds to tokens[i]: "1" = winner, "0" = loser
        if up_idx is not None and up_idx < len(outcome_prices):
            try:
                up_price = float(outcome_prices[up_idx])
            except (ValueError, TypeError):
                up_price = None
        else:
            # Fallback: scan for the token with price "1"
            up_price = None
            for i, tok in enumerate(tokens):
                if tok.get("outcome", "").strip().upper() == "UP":
                    try:
                        up_price = float(outcome_prices[i])
                    except Exception:
                        pass
                    break

        if up_price is not None:
            winner = "UP" if up_price >= 0.5 else "DOWN"
            print(f"  Gamma API (resolved): UP={up_price:.4f} → winner={winner}")
            return winner, up_price, token_id_up

    # Market found but not settled yet; return token_id for CLOB fallback
    return None, None, token_id_up


def _clob_last_price(token_id_up: str) -> float | None:
    """Fetch last traded price for the UP token from the public CLOB API."""
    url = f"{CLOB_API}/last-trade-price?token_id={urllib.parse.quote(token_id_up)}"
    try:
        data = _get(url)
        price = data.get("price")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"  [warn] CLOB API error: {exc}")
        return None


def _record_resolution(conn: sqlite3.Connection, cid: str, symbol: str,
                        winner: str, up_price: float) -> int:
    """
    Back-fill trades_executed for this condition_id.

    Polymarket binary market P&L (total payout):
      You spend `size` USDC to buy  size / entry_price  tokens.
      Win:  tokens pay $1 → pnl = size / entry_price  (capital + profit)
      Loss: entire stake forfeited → pnl = -size
      ARB:  total payout minus ~3% round-trip fee
    """
    now  = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT id, outcome, entry_price, size, trigger FROM trades_executed "
        "WHERE condition_id = ? AND resolved_at IS NULL",
        (cid,),
    ).fetchall()

    count = 0
    for row in rows:
        trade_id    = row[0]
        outcome     = row[1]
        entry_price = float(row[2])
        size        = float(row[3])
        trigger     = row[4] or ""

        if trigger == "arb":
            result = "arb"
            pnl    = size / entry_price - 0.03 * size
        elif outcome == winner:
            result = "positive"
            pnl    = size / entry_price
        else:
            result = "negative"
            pnl    = -size

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
        print(f"  {icon} trade #{trade_id}: {outcome} @ {entry_price:.4f} × ${size:.2f} "
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
        cid             = row["condition_id"]
        symbol          = row["symbol"] or "?"
        snap_token_up   = row["token_id_up"]   # from market_snapshots (may be None)

        try:
            age = (now - datetime.fromisoformat(
                row["first_trade_ts"].replace("Z", "+00:00")
            )).total_seconds()
        except Exception:
            age = 99999

        print(f"┌─ {symbol}  {cid[:16]}…  (age: {age/60:.1f} min)")

        if age < MIN_AGE_SECONDS:
            print(f"└─ skip — market is only {age/60:.1f} min old\n")
            continue

        # ── Step 1: Gamma API (primary — works for settled markets) ──────────
        winner, up_price, gamma_token_up = _resolve_from_gamma(cid)

        # Prefer snapshot token_id_up; use Gamma's if we didn't have one
        token_id_up = snap_token_up or gamma_token_up

        # ── Step 2: CLOB fallback for unresolved/active markets ──────────────
        if winner is None:
            if not token_id_up:
                print(f"└─ SKIP: no UP token found for {cid[:12]}…\n")
                continue

            up_price = _clob_last_price(token_id_up)
            if up_price is None:
                print(f"└─ SKIP: CLOB API returned no price\n")
                continue

            print(f"  CLOB last-trade-price: UP={up_price:.4f}")

            if up_price >= 0.90:
                winner = "UP"
            elif up_price <= 0.10:
                winner = "DOWN"
            elif age >= 1800:
                winner = "UP" if up_price >= 0.50 else "DOWN"
                print(f"  Force-resolve (age={age/60:.0f}min): → {winner}")
            else:
                print(f"└─ skip — price still ambiguous ({up_price:.4f}), will retry\n")
                continue

        print(f"│  winner: {winner}")

        n = _record_resolution(conn, cid, symbol, winner, up_price)
        print(f"└─ resolved {n} trade(s)\n")
        total_resolved += n

    print(f"Done. Resolved {total_resolved} trade(s) total.")
    conn.close()


if __name__ == "__main__":
    main()
