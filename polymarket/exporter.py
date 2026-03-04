"""
Polymarket 15M — GitHub Data Exporter

Writes live market snapshots, signals, and trade fills to a cloned GitHub
repository (Cyberdude01/Bob) and pushes every EXPORT_INTERVAL seconds.

Three distinct reports are generated under reports/:
  data_collector.md   — Raw + calculated data log (last 500 snapshots)
  decision_summary.md — Analysis leading to every signal emitted
  decision_tracker.md — Every trade taken, with resolution and P&L

All timestamps displayed in Eastern Time (America/New_York).

Environment variables
---------------------
GITHUB_TOKEN     Personal access token with repo write scope (required)
EXPORT_REPO      HTTPS URL of target repo (default: Cyberdude01/Bob)
EXPORT_DIR       Local clone path            (default: ~/bob)
EXPORT_INTERVAL  Push cadence in seconds     (default: 300 = 5 min)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from rich.console import Console

from .config import POLY_API_KEY, SLUGS, SYMBOL_MAP
from .database import Database

console = Console()

EXPORT_REPO     = os.getenv("EXPORT_REPO",     "https://github.com/Cyberdude01/Bob.git")
GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN",    "")
EXPORT_DIR      = Path(os.getenv("EXPORT_DIR", str(Path.home() / "bob")))
EXPORT_INTERVAL = int(os.getenv("EXPORT_INTERVAL", "300"))

# Reverse SYMBOL_MAP: "BTC" → "btc-updown-15m"
_SLUG_FOR = {v: k for k, v in SYMBOL_MAP.items()}


# ─── Eastern Time helpers ─────────────────────────────────────────────────────

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
    def _to_et(ts: str) -> str:
        """Convert a UTC ISO timestamp string to an ET-formatted string."""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.astimezone(_ET).strftime("%Y-%m-%d %I:%M %p ET")
        except Exception:
            return ts
    def _now_et() -> str:
        return datetime.now(_ET).strftime("%Y-%m-%d %I:%M:%S %p ET")
    def _now_et_iso() -> str:
        return datetime.now(_ET).strftime("%Y-%m-%dT%H:%M:%S ET")
except Exception:
    # Python < 3.9 / missing tzdata: fall back to fixed UTC-5 (EST)
    _EST = timezone(timedelta(hours=-5))
    def _to_et(ts: str) -> str:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.astimezone(_EST).strftime("%Y-%m-%d %I:%M %p EST")
        except Exception:
            return ts
    def _now_et() -> str:
        return datetime.now(_EST).strftime("%Y-%m-%d %I:%M:%S %p EST")
    def _now_et_iso() -> str:
        return datetime.now(_EST).strftime("%Y-%m-%dT%H:%M:%S EST")


# ─── Git helpers ──────────────────────────────────────────────────────────────

def _auth_url() -> str:
    if GITHUB_TOKEN and EXPORT_REPO.startswith("https://"):
        return EXPORT_REPO.replace("https://", f"https://{GITHUB_TOKEN}@", 1)
    return EXPORT_REPO


def _git(args: List[str], **kw) -> bool:
    try:
        subprocess.run(["git"] + args, check=True, capture_output=True, cwd=EXPORT_DIR, **kw)
        return True
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        console.log(f"[red]Exporter git error (git {args[0]}): {stderr[:300]}[/red]")
        return False


# ─── Cell formatters (used in all three reports) ──────────────────────────────

def _f(val, fmt: str, suffix: str = "") -> str:
    """Format val with fmt if not None/falsy, else return '—'."""
    if val is None:
        return "—"
    try:
        return format(val, fmt) + suffix
    except Exception:
        return str(val)


def _pct(val) -> str:
    return f"{val*100:.1f}%" if val is not None else "—"


def _price(val) -> str:
    return f"{val:.4f}" if val is not None else "—"


def _row(cells: List[str]) -> str:
    return "| " + " | ".join(cells) + " |"


# ─── Exporter ─────────────────────────────────────────────────────────────────

class DataExporter:
    """
    Snapshots MarketState + trading data to JSON files inside EXPORT_DIR
    and pushes them to the configured GitHub repository every EXPORT_INTERVAL s.

    Three Markdown reports are generated under reports/:
      data_collector.md   — all raw + calculated snapshot data
      decision_summary.md — every signal with full reasoning
      decision_tracker.md — every trade with entry, resolution, and P&L
    """

    def __init__(
        self,
        state,
        book=None,
        signal_log: Optional[Deque] = None,
        exec_log:   Optional[List]  = None,
        db:         Optional[Database] = None,
    ):
        self.state      = state
        self.book       = book
        self.signal_log = signal_log if signal_log is not None else deque()
        self.exec_log   = exec_log   if exec_log   is not None else []
        self.db         = db
        self._ready     = False

    # ── Repo setup ────────────────────────────────────────────────────────────

    def _ensure_repo(self) -> bool:
        if self._ready:
            return True
        if not GITHUB_TOKEN:
            console.log("[yellow]Exporter: GITHUB_TOKEN not set — export disabled[/yellow]")
            return False

        if not EXPORT_DIR.exists():
            console.log(f"[cyan]Exporter: cloning {EXPORT_REPO} → {EXPORT_DIR}[/cyan]")
            try:
                subprocess.run(
                    ["git", "clone", _auth_url(), str(EXPORT_DIR)],
                    check=True, capture_output=True,
                )
            except subprocess.CalledProcessError as exc:
                console.log(f"[red]Exporter: clone failed — {exc.stderr.decode()[:200]}[/red]")
                return False
        else:
            _git(["remote", "set-url", "origin", _auth_url()])
            _git(["pull", "--rebase", "origin", "HEAD"])

        _git(["config", "user.email", "polymarket-feed@bot"])
        _git(["config", "user.name",  "Polymarket Feed"])
        (EXPORT_DIR / "data_exports").mkdir(exist_ok=True)
        (EXPORT_DIR / "reports").mkdir(exist_ok=True)
        self._ready = True
        console.log("[green]Exporter: repo ready[/green]")
        return True

    # ── Market snapshot (for JSON + README) ───────────────────────────────────

    def _market_snapshot(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for slug in SLUGS:
            sym  = SYMBOL_MAP.get(slug, slug)
            mkt  = self.state.get_current_market(slug)
            snap = self.state.analytics.get(sym).last_snapshot
            row: Dict[str, Any] = {"symbol": sym, "slug": slug}

            if mkt:
                # Use analytics snap prices (computed directly from order books)
                # and fall back to mkt token prices only when snap is unavailable.
                up_p  = (snap.up_price   if snap else None) or (mkt.up_token.price   if mkt.up_token   else None)
                dn_p  = (snap.down_price if snap else None) or (mkt.down_token.price if mkt.down_token else None)

                # If DOWN price equals UP (stale order book at expiry), derive it
                # from UP: in a binary market UP + DOWN ≈ 1.0
                if up_p is not None and dn_p is not None and abs(up_p - dn_p) < 0.005:
                    dn_p = round(1.0 - up_p, 4)

                row.update({
                    "condition_id":  mkt.condition_id,
                    "elapsed_pct":   round(mkt.elapsed_pct, 4),
                    "remaining_sec": round(mkt.remaining_seconds, 1),
                    "up_price":      round(up_p, 4) if up_p is not None else None,
                    "down_price":    round(dn_p, 4) if dn_p is not None else None,
                    "arb":           mkt.arb_opportunity,
                })

            if snap:
                row.update({
                    "rv60":         round(snap.rv60,   6),
                    "eff60":        round(snap.eff60,  4),
                    "vol_bucket":   snap.vol_bucket.value,
                    "trend_bucket": snap.trend_bucket.value,
                    "spread":       round(snap.spread,     4),
                    "dir_60pct":    round(snap.dir_60pct,  4),
                    "dir_80pct":    round(snap.dir_80pct,  4),
                    "dir_90pct":    round(snap.dir_90pct,  4),
                    "prob_008":     round(snap.prob_008,   4),
                    "prob_012":     round(snap.prob_012,   4),
                    "prob_020":     round(snap.prob_020,   4),
                })
            out[sym] = row
        return out

    # ── Report 1: Data Collector ───────────────────────────────────────────────

    def _build_data_collector_report(self, ts: str) -> str:
        rows = self.db.recent_snapshots(hours=48, limit=500) if self.db else []

        lines = [
            "# Data Collector Report",
            f"\n> **Updated:** `{ts}` &nbsp;|&nbsp; Last 500 market snapshots (48 h)\n",
            _row(["Time (ET)", "Symbol", "Slug", "UP Price", "DOWN Price",
                  "UP Spread", "DN Spread", "Elapsed%", "Vol Bucket", "Trend",
                  "RV60", "Eff60", "P(UP)@60%", "UP Trades", "DN Trades", "ARB"]),
            _row(["-"*15, "-"*6, "-"*17, "-"*8, "-"*8,
                  "-"*8, "-"*8, "-"*8, "-"*9, "-"*5,
                  "-"*7, "-"*5, "-"*9, "-"*9, "-"*9, "-"*6]),
        ]

        for r in rows:
            slug  = _SLUG_FOR.get(r["symbol"], r["symbol"])
            up_p  = r["up_price"]
            dn_p  = r["down_price"]
            # Correct rows where DOWN was stored as the same as UP (stale OB)
            if up_p is not None and dn_p is not None and abs(up_p - dn_p) < 0.005:
                dn_p = round(1.0 - up_p, 4)
            cells = [
                f"`{_to_et(r['ts'])}`",
                r["symbol"] or "—",
                slug,
                _price(up_p),
                _price(dn_p),
                _price(r["up_spread"]),
                _price(r["down_spread"]),
                _pct(r["elapsed_pct"]),
                r["vol_bucket"]   or "—",
                r["trend_bucket"] or "—",
                _f(r["rv60"],  ".5f"),
                _f(r["eff60"], ".3f"),
                _pct(r["dir_60pct"]),
                str(r["up_trade_count"]   or 0),
                str(r["down_trade_count"] or 0),
                _f(r["arb_profit"], ".4f") if r["arb_profit"] else "—",
            ]
            lines.append(_row(cells))

        if not rows:
            lines.append(_row(["—"] * 16))

        lines += ["", "---",
                  "_Auto-generated by [openclaw](https://github.com/Cyberdude01/openclaw)_"]
        return "\n".join(lines) + "\n"

    # ── Report 2: Decision Engine Summary ─────────────────────────────────────

    def _build_decision_summary_report(self, ts: str) -> str:
        rows = self.db.all_signals(hours=48) if self.db else []

        by_trigger: Dict[str, int] = {}
        for r in rows:
            t = r["trigger"] or "unknown"
            by_trigger[t] = by_trigger.get(t, 0) + 1

        lines = [
            "# Decision Engine Summary",
            f"\n> **Updated:** `{ts}` &nbsp;|&nbsp; All signals from the last 48 hours\n",
            "## Signal Distribution",
            _row(["Trigger", "Count"]),
            _row(["-"*20, "-"*5]),
        ]
        for trigger, count in sorted(by_trigger.items(), key=lambda x: -x[1]):
            lines.append(_row([f"`{trigger}`", str(count)]))

        lines += [
            "",
            "## Signal Log",
            _row(["Time (ET)", "Symbol", "Slug", "Outcome", "Trigger",
                  "Confidence", "P(UP)", "Bucket", "Elapsed%", "Reasoning"]),
            _row(["-"*15, "-"*6, "-"*17, "-"*7, "-"*20,
                  "-"*10, "-"*5, "-"*14, "-"*8, "-"*50]),
        ]
        for r in rows:
            slug   = _SLUG_FOR.get(r["symbol"], r["symbol"])
            bucket = f"{r['vol_bucket']}+{r['trend_bucket']}" if r["vol_bucket"] else "—"
            reason = (r["reasoning"] or "")[:120].replace("|", "\\|")
            lines.append(_row([
                f"`{_to_et(r['ts'])}`",
                r["symbol"],
                slug,
                f"**{r['outcome']}**",
                f"`{r['trigger'] or '—'}`",
                _f(r["confidence"], ".3f"),
                _pct(r["prob_up"]),
                bucket,
                _pct(r["elapsed_pct"]),
                reason,
            ]))

        if not rows:
            lines.append(_row(["—"] * 10))

        lines += ["", "---",
                  "_Auto-generated by [openclaw](https://github.com/Cyberdude01/openclaw)_"]
        return "\n".join(lines) + "\n"

    # ── Report 3: Decision Tracker ─────────────────────────────────────────────

    def _build_decision_tracker_report(self, ts: str) -> str:
        rows    = self.db.all_trades()    if self.db else []
        summary = self.db.trade_summary() if self.db else {}

        total    = int(summary.get("total",      0) or 0)
        resolved = int(summary.get("resolved",   0) or 0)
        wins     = int(summary.get("wins",       0) or 0)
        losses   = int(summary.get("losses",     0) or 0)
        arbs     = int(summary.get("arb_trades", 0) or 0)
        pending  = int(summary.get("pending",    0) or 0)
        pnl      = float(summary.get("total_pnl", 0.0) or 0.0)
        # Win-rate is against resolved (non-arb) trades only
        non_arb_resolved = wins + losses
        win_rate = f"{wins/non_arb_resolved*100:.1f}%" if non_arb_resolved > 0 else "—"
        pnl_str  = f"{'+'if pnl>=0 else ''}${pnl:.4f}"

        lines = [
            "# Decision Tracker",
            f"\n> **Updated:** `{ts}` &nbsp;|&nbsp; Full trade history — refreshed every 5 minutes\n",
            "## Summary",
            _row(["Total", "Resolved", "Wins", "Losses", "ARB", "Pending", "Win Rate", "Realised P&L"]),
            _row(["-"*5,   "-"*8,      "-"*4,  "-"*6,    "-"*3, "-"*7,    "-"*8,      "-"*12]),
            _row([str(total), str(resolved), str(wins), str(losses), str(arbs),
                  str(pending), win_rate, pnl_str]),
            "",
            "## Trade Log",
            "> Each row: Entry Time · Market Slug · Condition ID (first 12 chars) · 15-min Window\n",
            _row(["Entry Time (ET)", "Market & Window",
                  "Outcome", "Trigger", "Entry $", "Size", "Mode",
                  "Resolved (ET)", "Winner", "Result", "P&L", "Reasoning"]),
            _row(["-"*17, "-"*40,
                  "-"*7, "-"*22, "-"*7, "-"*6, "-"*5,
                  "-"*17, "-"*6, "-"*8, "-"*9, "-"*50]),
        ]

        _RESULT_ICON = {
            "positive": "✅ Win",
            "negative": "❌ Loss",
            "arb":      "💰 ARB",
        }

        for r in rows:
            slug      = _SLUG_FOR.get(r["symbol"], r["symbol"])
            cond_id   = (r["condition_id"] or "")
            cond_short = cond_id[:12] + "…" if len(cond_id) > 12 else cond_id

            # Market window — from snapshot JOIN (may be None before first snapshot)
            win_start = _to_et(r["window_start"]) if r["window_start"] else "—"
            win_end   = _to_et(r["window_end"])   if r["window_end"]   else "—"
            # Compact: "09:00 AM → 09:15 AM ET" (strip date + timezone from second)
            def _compact_window(s: str, e: str) -> str:
                if s == "—":
                    return "—"
                # strip "ET" from start, keep only time+am/pm for end
                s_t = s.split(" ")[1] + " " + s.split(" ")[2] if len(s.split(" ")) >= 3 else s
                e_t = e.split(" ")[1] + " " + e.split(" ")[2] if len(e.split(" ")) >= 3 else e
                return f"{s_t} → {e_t}"

            market_cell = f"**{slug}** `{cond_short}`<br>{_compact_window(win_start, win_end)}"

            res_time = _to_et(r["resolved_at"]) if r["resolved_at"] else "⏳ Pending"
            winner   = r["market_winner"] or "—"
            result   = r["result"]        or "pending"
            result_s = _RESULT_ICON.get(result, "⏳ Pending")
            pnl_v    = r["pnl"]
            pnl_cell = f"{'+'if (pnl_v or 0)>=0 else ''}${(pnl_v or 0):.4f}" \
                       if pnl_v is not None else "⏳"
            reason   = (r["reasoning"] or "")[:100].replace("|", "\\|")

            lines.append(_row([
                f"`{_to_et(r['ts'])}`",
                market_cell,
                f"**{r['outcome']}**",
                f"`{r['trigger'] or '—'}`",
                _price(r["entry_price"]),
                f"${r['size']:.2f}",
                r["mode"] or "—",
                res_time,
                winner,
                result_s,
                pnl_cell,
                reason,
            ]))

        if not rows:
            lines.append(
                "> ℹ️ No trades recorded yet. Trades are generated at the 60%, 80%, "
                "and 90% elapsed marks of each 15-minute market window."
            )

        lines += ["", "---",
                  "_Auto-generated by [openclaw](https://github.com/Cyberdude01/openclaw)_"]
        return "\n".join(lines) + "\n"

    # ── README ────────────────────────────────────────────────────────────────

    def _build_readme(self, ts: str, markets: dict, trades: list, portfolio: dict) -> str:
        mode = "LIVE" if POLY_API_KEY else "PAPER"
        lines = [
            "# Polymarket 15M Data Feed",
            f"\n> **Mode:** {mode} &nbsp;|&nbsp; **Updated:** `{ts}`\n",
            "## Live Markets",
            _row(["Symbol", "Slug", "UP", "DOWN", "Elapsed", "Remaining",
                  "Bucket", "Dir@60%", "Dir@80%", "Dir@90%", "ARB"]),
            _row(["-"*6, "-"*17, "-"*6, "-"*6, "-"*7, "-"*9,
                  "-"*14, "-"*7, "-"*7, "-"*7, "-"*5]),
        ]
        for sym, d in markets.items():
            slug = d.get("slug", _SLUG_FOR.get(sym, sym))
            up   = _price(d.get("up_price"))
            dn   = _price(d.get("down_price"))
            el   = _pct(d.get("elapsed_pct"))
            rem  = f"{d.get('remaining_sec', 0):.0f}s" if "remaining_sec" in d else "—"
            bk   = (f"{d.get('vol_bucket')}+{d.get('trend_bucket')}"
                    if "vol_bucket" in d else "—")
            d60  = _pct(d.get("dir_60pct"))
            d80  = _pct(d.get("dir_80pct"))
            d90  = _pct(d.get("dir_90pct"))
            arb  = f"${d['arb']:.4f}" if d.get("arb") else "—"
            lines.append(_row([f"**{sym}**", slug, up, dn, el, rem, bk, d60, d80, d90, arb]))

        if portfolio:
            pnl_sign = "+" if portfolio.get("realized_pnl", 0) >= 0 else ""
            lines += [
                "\n## Portfolio",
                _row(["Balance", "Realized P&L"]),
                _row(["-"*9, "-"*13]),
                _row([f"${portfolio.get('balance', 0):.2f}",
                      f"{pnl_sign}${portfolio.get('realized_pnl', 0):.4f}"]),
            ]

        if trades:
            lines += [
                "\n## Recent Fills (last 10)",
                _row(["Time (ET)", "Symbol", "Outcome", "Side", "Size", "Price", "Trigger", "Mode"]),
                _row(["-"*15, "-"*6, "-"*7, "-"*4, "-"*6, "-"*6, "-"*20, "-"*5]),
            ]
            for t in trades[-10:]:
                lines.append(_row([
                    f"`{_to_et(t.get('ts', ''))}`",
                    t.get("symbol", ""),
                    t.get("outcome", ""),
                    t.get("side", ""),
                    f"${t.get('size', 0):.2f}",
                    f"{t.get('price', 0):.4f}",
                    f"`{t.get('trigger', '—')}`",
                    f"**{t.get('mode', '').upper()}**",
                ]))

        lines += [
            "",
            "## Reports",
            _row(["Report", "Description"]),
            _row(["-"*30, "-"*40]),
            _row(["[Data Collector](reports/data_collector.md)",
                  "Raw + calculated data log (last 48 h)"]),
            _row(["[Decision Summary](reports/decision_summary.md)",
                  "Analysis behind every signal"]),
            _row(["[Decision Tracker](reports/decision_tracker.md)",
                  "Full trade history with entry, resolution and P&L"]),
            "",
            "---",
            "_Auto-generated by [openclaw](https://github.com/Cyberdude01/openclaw)_",
        ]
        return "\n".join(lines) + "\n"

    # ── Snapshot + push ───────────────────────────────────────────────────────

    def _snapshot(self) -> None:
        ts      = _now_et()
        markets = self._market_snapshot()
        trades  = list(self.exec_log)[-50:]
        signals = [
            {
                "symbol":     s.symbol,
                "slug":       _SLUG_FOR.get(s.symbol, s.symbol),
                "outcome":    s.outcome.value,
                "side":       s.side.value,
                "size":       s.size,
                "price":      s.price,
                "confidence": s.confidence,
                "trigger":    s.trigger,
                "reason":     s.reason,
            }
            for s in list(self.signal_log)[-20:]
        ]
        portfolio = (
            {"balance": round(self.book.balance, 2),
             "realized_pnl": round(self.book.realized_pnl, 4)}
            if self.book else {}
        )

        d = EXPORT_DIR / "data_exports"
        (d / "markets.json").write_text(json.dumps({"updated": ts, "data": markets}, indent=2))
        (d / "trades.json").write_text(json.dumps({"updated": ts, "data": trades}, indent=2))
        (d / "signals.json").write_text(json.dumps({"updated": ts, "data": signals}, indent=2))
        (d / "portfolio.json").write_text(json.dumps({"updated": ts, **portfolio}, indent=2))

        # Three Markdown reports (read from DB)
        if self.db:
            r = EXPORT_DIR / "reports"
            (r / "data_collector.md").write_text(self._build_data_collector_report(ts))
            (r / "decision_summary.md").write_text(self._build_decision_summary_report(ts))
            (r / "decision_tracker.md").write_text(self._build_decision_tracker_report(ts))

        (EXPORT_DIR / "README.md").write_text(self._build_readme(ts, markets, trades, portfolio))

    def _push(self) -> None:
        _git(["add", "-A"])
        ts = _now_et_iso()
        _git(["commit", "--allow-empty", "-m", f"data: {ts}"])
        ok = _git(["push", "origin", "HEAD"])
        if ok:
            console.log(f"[green]Exporter: pushed snapshot at {ts}[/green]")
        else:
            console.log("[yellow]Exporter: push failed (will retry next interval)[/yellow]")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._ensure_repo():
            return
        while True:
            try:
                self._snapshot()
                self._push()
            except Exception as exc:
                console.log(f"[red]Exporter error: {exc}[/red]")
            await asyncio.sleep(EXPORT_INTERVAL)
