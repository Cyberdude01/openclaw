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
import csv
import io
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
                  "_Auto-generated by **Bob the builder**_"]
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
                  "_Auto-generated by **Bob the builder**_"]
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
                  "_Auto-generated by **Bob the builder**_"]
        return "\n".join(lines) + "\n"

    # ── Report 4: Trigger Performance Summary ─────────────────────────────────

    def _build_trigger_summary_report(self, ts: str) -> str:
        if not self.db:
            return "# Trigger Performance Summary\n\nNo data available.\n"

        start_ts = self.db.get_stats_start_ts()
        rows     = self.db.trigger_stats_since(start_ts)
        bkt_rows = self.db.bucket_stats_since(start_ts)
        start_et = _to_et(start_ts)

        # Organise by trigger → {UP: {...}, DOWN: {...}}
        by_trigger: Dict[str, Dict[str, Dict[str, int]]] = {}
        for r in rows:
            t = r["trigger"] or "unknown"
            if t not in by_trigger:
                by_trigger[t] = {}
            by_trigger[t][r["outcome"]] = {
                "total":  r["total"]  or 0,
                "wins":   r["wins"]   or 0,
                "losses": r["losses"] or 0,
            }

        lines = [
            "# Trigger Performance Summary",
            f"\n> **Updated:** `{ts}` &nbsp;|&nbsp; **Tracking from:** `{start_et}`\n",
            "> Counts only trades placed after the tracking start date. "
            "Historical trades are excluded. Unresolved trades are not counted in wins/losses.\n",
            _row(["Trigger", "UP Trades", "UP Wins", "UP Losses",
                  "DOWN Trades", "DOWN Wins", "DOWN Losses", "Total", "Win Rate"]),
            _row(["-"*22, "-"*9, "-"*7, "-"*9,
                  "-"*11, "-"*9, "-"*11, "-"*5, "-"*8]),
        ]

        grand_total = grand_wins = grand_losses = 0
        for trigger in sorted(by_trigger.keys()):
            up = by_trigger[trigger].get("UP",   {"total": 0, "wins": 0, "losses": 0})
            dn = by_trigger[trigger].get("DOWN", {"total": 0, "wins": 0, "losses": 0})
            total  = up["total"]  + dn["total"]
            wins   = up["wins"]   + dn["wins"]
            losses = up["losses"] + dn["losses"]
            grand_total  += total
            grand_wins   += wins
            grand_losses += losses
            wr = f"{wins/(wins+losses)*100:.1f}%" if (wins + losses) > 0 else "—"
            lines.append(_row([
                f"`{trigger}`",
                str(up["total"]), str(up["wins"]), str(up["losses"]),
                str(dn["total"]), str(dn["wins"]), str(dn["losses"]),
                str(total), wr,
            ]))

        if not by_trigger:
            lines.append(_row(["—"] * 9))
            lines.append("\n> ℹ️ No resolved trades recorded since tracking started.")
        else:
            overall_wr = (
                f"{grand_wins/(grand_wins+grand_losses)*100:.1f}%"
                if (grand_wins + grand_losses) > 0 else "—"
            )
            lines.append(_row([
                "**TOTAL**",
                "—", "—", "—", "—", "—", "—",
                f"**{grand_total}**", f"**{overall_wr}**",
            ]))

        # ── Bucket breakdown ──────────────────────────────────────────────────
        # Aggregate bkt_rows by (bucket, trigger) regardless of UP/DOWN outcome
        BUCKET_ORDER = ["HighVol+Trend", "HighVol+Range", "LowVol+Trend", "LowVol+Range", "unknown"]

        # bucket → trigger → {total, wins, losses}
        by_bucket: Dict[str, Dict[str, Dict[str, int]]] = {}
        for r in bkt_rows:
            bkt = r["bucket"] or "unknown"
            trg = r["trigger"] or "unknown"
            if bkt not in by_bucket:
                by_bucket[bkt] = {}
            if trg not in by_bucket[bkt]:
                by_bucket[bkt][trg] = {"total": 0, "wins": 0, "losses": 0}
            by_bucket[bkt][trg]["total"]  += r["total"]  or 0
            by_bucket[bkt][trg]["wins"]   += r["wins"]   or 0
            by_bucket[bkt][trg]["losses"] += r["losses"] or 0

        if by_bucket:
            lines += [
                "",
                "## Performance by Market Bucket",
                "",
                "> `HighVol+Trend` — high realized volatility, strongly trending. "
                "`HighVol+Range` — high volatility, mean-reverting. "
                "`LowVol+Trend` — quiet but directional. "
                "`LowVol+Range` — quiet and choppy.\n",
                _row(["Bucket", "Trigger", "Trades", "Wins", "Losses", "Win Rate"]),
                _row(["-"*14, "-"*22, "-"*6, "-"*4, "-"*6, "-"*8]),
            ]

            # Bucket keys match vol_bucket.value + '+' + trend_bucket.value from models.py
            # e.g. "HighVol+Trend", "HighVol+Range", "LowVol+Trend", "LowVol+Range"
            _BKT_DISPLAY = {
                "HighVol+Trend": "HighVol+Trend",
                "HighVol+Range": "HighVol+Range",
                "LowVol+Trend":  "LowVol+Trend",
                "LowVol+Range":  "LowVol+Range",
            }

            bkt_grand_total = bkt_grand_wins = bkt_grand_losses = 0
            for bkt_key in BUCKET_ORDER:
                if bkt_key not in by_bucket:
                    continue
                display = _BKT_DISPLAY.get(bkt_key, bkt_key)
                bkt_total = bkt_wins = bkt_losses = 0
                first = True
                for trg in sorted(by_bucket[bkt_key].keys()):
                    d = by_bucket[bkt_key][trg]
                    bkt_total  += d["total"]
                    bkt_wins   += d["wins"]
                    bkt_losses += d["losses"]
                    wr = f"{d['wins']/(d['wins']+d['losses'])*100:.1f}%" if (d["wins"] + d["losses"]) > 0 else "—"
                    lines.append(_row([
                        f"**{display}**" if first else "",
                        f"`{trg}`",
                        str(d["total"]), str(d["wins"]), str(d["losses"]), wr,
                    ]))
                    first = False
                # Bucket subtotal
                bkt_wr = f"{bkt_wins/(bkt_wins+bkt_losses)*100:.1f}%" if (bkt_wins + bkt_losses) > 0 else "—"
                lines.append(_row(["", f"*subtotal*", f"*{bkt_total}*", f"*{bkt_wins}*", f"*{bkt_losses}*", f"*{bkt_wr}*"]))
                bkt_grand_total  += bkt_total
                bkt_grand_wins   += bkt_wins
                bkt_grand_losses += bkt_losses

            bkt_overall_wr = (
                f"{bkt_grand_wins/(bkt_grand_wins+bkt_grand_losses)*100:.1f}%"
                if (bkt_grand_wins + bkt_grand_losses) > 0 else "—"
            )
            lines.append(_row([
                "**TOTAL**", "",
                f"**{bkt_grand_total}**", f"**{bkt_grand_wins}**",
                f"**{bkt_grand_losses}**", f"**{bkt_overall_wr}**",
            ]))

        lines += ["", "---",
                  "_Auto-generated by **Bob the builder**_"]
        return "\n".join(lines) + "\n"

    # ── Report 4b: Trigger Performance Summary v2 ─────────────────────────────

    def _build_trigger_summary_v2_report(self, ts: str) -> str:
        if not self.db:
            return "# Trigger Performance Summary v2\n\nNo data available.\n"

        start_ts = self.db.get_stats_start_ts_v2()
        rows     = self.db.trigger_stats_v2_since(start_ts)
        start_et = _to_et(start_ts)

        SYMBOL_ORDER = ["BTC", "ETH", "SOL", "XRP"]

        # Organise: symbol → trigger → stats
        by_sym: Dict[str, Dict[str, Dict]] = {s: {} for s in SYMBOL_ORDER}
        for r in rows:
            sym = r["symbol"]
            if sym not in by_sym:
                by_sym[sym] = {}
            by_sym[sym][r["trigger"] or "unknown"] = r

        lines = [
            "# Trigger Performance Summary v2",
            f"\n> **Updated:** `{ts}` &nbsp;|&nbsp; **Tracking from:** `{start_et}`\n",
            "> Fresh epoch — tracks only trades placed after the v2 start date. "
            "Includes realised P&L per trigger.\n",
        ]

        grand_total = grand_wins = grand_losses = grand_pnl = 0.0

        for sym in SYMBOL_ORDER:
            sym_data = by_sym.get(sym, {})
            if not sym_data:
                continue

            lines += [
                f"## {sym}",
                "",
                _row(["Trigger", "Trades", "Wins", "Losses", "Win Rate",
                      "Total P&L", "Avg P&L"]),
                _row(["-"*22, "-"*6, "-"*4, "-"*6, "-"*8, "-"*10, "-"*8]),
            ]

            sym_total = sym_wins = sym_losses = sym_pnl = 0
            for trigger in sorted(sym_data.keys()):
                d      = sym_data[trigger]
                total  = d["total"]  or 0
                wins   = d["wins"]   or 0
                losses = d["losses"] or 0
                tpnl   = d["total_pnl"] or 0.0
                apnl   = d["avg_pnl"]
                wr     = f"{wins/(wins+losses)*100:.1f}%" if (wins + losses) > 0 else "—"
                tpnl_s = f"{'+'if tpnl>=0 else ''}${tpnl:.4f}"
                apnl_s = f"{'+'if (apnl or 0)>=0 else ''}${(apnl or 0):.4f}" if apnl is not None else "—"
                lines.append(_row([
                    f"`{trigger}`",
                    str(total), str(wins), str(losses), wr, tpnl_s, apnl_s,
                ]))
                sym_total  += total
                sym_wins   += wins
                sym_losses += losses
                sym_pnl    += tpnl

            sym_wr  = f"{sym_wins/(sym_wins+sym_losses)*100:.1f}%" if (sym_wins + sym_losses) > 0 else "—"
            sym_pnl_s = f"{'+'if sym_pnl>=0 else ''}${sym_pnl:.4f}"
            lines.append(_row([
                f"**{sym} TOTAL**",
                f"**{sym_total}**", f"**{sym_wins}**", f"**{sym_losses}**",
                f"**{sym_wr}**", f"**{sym_pnl_s}**", "",
            ]))
            lines.append("")

            grand_total  += sym_total
            grand_wins   += sym_wins
            grand_losses += sym_losses
            grand_pnl    += sym_pnl

        if grand_total == 0:
            lines.append("> ℹ️ No resolved trades recorded since tracking started.")
        else:
            grand_wr  = f"{grand_wins/(grand_wins+grand_losses)*100:.1f}%" if (grand_wins + grand_losses) > 0 else "—"
            grand_pnl_s = f"{'+'if grand_pnl>=0 else ''}${grand_pnl:.4f}"
            lines += [
                "---",
                _row(["**GRAND TOTAL**",
                      f"**{int(grand_total)}**", f"**{int(grand_wins)}**",
                      f"**{int(grand_losses)}**", f"**{grand_wr}**",
                      f"**{grand_pnl_s}**", ""]),
            ]

        lines += ["", "---",
                  "_Auto-generated by **Bob the builder**_"]
        return "\n".join(lines) + "\n"

    # ── Report 5: Market P&L Summary ──────────────────────────────────────────

    def _build_market_pnl_report(self, ts: str) -> str:
        if not self.db:
            return "# Market P&L Summary\n\nNo data available.\n"

        rows = self.db.market_pnl_summary()

        # Group by symbol preserving DB order (most-recent first within symbol)
        SYMBOL_ORDER = ["BTC", "ETH", "SOL", "XRP"]
        by_symbol: Dict[str, List[Dict]] = {s: [] for s in SYMBOL_ORDER}
        for r in rows:
            sym = r["symbol"]
            if sym not in by_symbol:
                by_symbol[sym] = []
            by_symbol[sym].append(r)

        lines = [
            "# Market P&L Summary",
            f"\n> **Updated:** `{ts}`\n",
            "> One row per 15-minute market window. "
            "P&L shown only for resolved markets. "
            "Unresolved trades count toward Bets but not Wins/Losses.\n",
        ]

        grand_bets = grand_wins = grand_losses = grand_pnl = 0

        for sym in SYMBOL_ORDER:
            windows = by_symbol.get(sym, [])
            if not windows:
                continue

            lines += [
                f"## {sym}",
                "",
                _row(["Window (ET)", "Market Slug", "Bets", "Wins", "Losses", "Win Rate",
                      "P&L (USDC)", "Outcome"]),
                _row(["-"*19, "-"*28, "-"*4, "-"*4, "-"*6, "-"*8, "-"*10, "-"*7]),
            ]

            sym_bets = sym_wins = sym_losses = sym_pnl = 0
            for r in windows:
                bets   = r["total_trades"] or 0
                wins   = r["wins"]         or 0
                losses = r["losses"]       or 0
                pnl    = r["total_pnl"]    or 0.0
                # Use full slug if available, else fall back to short condition_id
                slug_display = r.get("slug") or ((r["condition_id"] or "")[:10] + "…")
                ts_et  = _to_et(r["first_trade_ts"]) if r["first_trade_ts"] else "—"
                wr     = f"{wins/(wins+losses)*100:.0f}%" if (wins + losses) > 0 else "—"
                pnl_s  = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
                winner = r["winning_outcome"] or "pending"

                lines.append(_row([
                    f"`{ts_et}`", f"`{slug_display}`",
                    str(bets), str(wins), str(losses), wr,
                    f"`{pnl_s}`", winner,
                ]))
                sym_bets   += bets
                sym_wins   += wins
                sym_losses += losses
                sym_pnl    += pnl

            sym_wr  = f"{sym_wins/(sym_wins+sym_losses)*100:.0f}%" if (sym_wins + sym_losses) > 0 else "—"
            sym_pnl_s = f"+{sym_pnl:.2f}" if sym_pnl >= 0 else f"{sym_pnl:.2f}"
            lines.append(_row([
                f"**{sym} TOTAL**", "",
                f"**{sym_bets}**", f"**{sym_wins}**", f"**{sym_losses}**", f"**{sym_wr}**",
                f"**`{sym_pnl_s}`**", "",
            ]))
            lines.append("")

            grand_bets   += sym_bets
            grand_wins   += sym_wins
            grand_losses += sym_losses
            grand_pnl    += sym_pnl

        if grand_bets == 0:
            lines.append("> ℹ️ No trades recorded yet.")
        else:
            grand_wr  = f"{grand_wins/(grand_wins+grand_losses)*100:.0f}%" if (grand_wins + grand_losses) > 0 else "—"
            grand_pnl_s = f"+{grand_pnl:.2f}" if grand_pnl >= 0 else f"{grand_pnl:.2f}"
            lines += [
                "---",
                _row(["**GRAND TOTAL**", "", f"**{grand_bets}**",
                      f"**{grand_wins}**", f"**{grand_losses}**", f"**{grand_wr}**",
                      f"**`{grand_pnl_s}`**", ""]),
            ]

        lines += ["", "---",
                  "_Auto-generated by **Bob the builder**_"]
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
            _row(["[Trigger Summary](reports/trigger_summary.md)",
                  "UP/DOWN trades, wins and losses by trigger (current epoch only)"]),
            _row(["[Trigger Summary v2](reports/trigger_summary_v2.md)",
                  "Trigger P&L by symbol — fresh epoch, clean baseline"]),
            _row(["[Market P&L](reports/market_pnl.md)",
                  "Bets and P&L per market window, grouped by symbol"]),
            "",
            "---",
            "_Auto-generated by **Bob the builder**_",
        ]
        return "\n".join(lines) + "\n"

    # ── CSV export (append-only, one file per symbol) ─────────────────────────

    # All columns from market_snapshots in display order
    _CSV_FIELDS = [
        "ts", "symbol", "slug", "condition_id", "token_id_up", "token_id_down",
        "up_price", "down_price",
        "up_best_bid", "up_best_ask", "down_best_bid", "down_best_ask",
        "up_spread", "down_spread",
        "up_bid_depth", "up_ask_depth", "down_bid_depth", "down_ask_depth",
        "elapsed_pct", "remaining_sec", "arb_profit",
        "up_trade_count", "down_trade_count", "total_volume",
        "vol_bucket", "trend_bucket", "rv60", "eff60",
        "prob_up", "dir_60pct", "dir_80pct", "dir_90pct",
        "prob_008", "prob_012", "prob_020",
        "market_start_ts", "market_end_ts",
    ]

    def _update_csv_exports(self, d: Path) -> None:
        """
        Append new market_snapshots rows to per-symbol CSV files.
        Tracks the last-exported timestamp per symbol in the DB settings table
        so only genuinely new rows are written on each cycle.
        The CSV files accumulate full history in the Bob repo.
        """
        if not self.db:
            return
        for sym in ("BTC", "ETH", "SOL", "XRP"):
            key      = f"csv_last_ts_{sym}"
            since_ts = self.db.get_setting(key, "1970-01-01T00:00:00+00:00")
            new_rows = self.db.snapshots_since(sym, since_ts)
            if not new_rows:
                continue

            csv_path = d / f"{sym}.csv"
            write_header = not csv_path.exists()

            buf = io.StringIO()
            writer = csv.DictWriter(
                buf,
                fieldnames=self._CSV_FIELDS,
                extrasaction="ignore",
                lineterminator="\n",
            )
            if write_header:
                writer.writeheader()
            for row in new_rows:
                writer.writerow({f: row.get(f, "") for f in self._CSV_FIELDS})

            with csv_path.open("a", encoding="utf-8", newline="") as fh:
                fh.write(buf.getvalue())

            # Advance the cursor to the latest exported timestamp
            self.db.set_setting(key, new_rows[-1]["ts"])

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

        # CSV files (append-only, one per symbol)
        self._update_csv_exports(d)

        # Markdown reports (read from DB)
        if self.db:
            r = EXPORT_DIR / "reports"
            (r / "data_collector.md").write_text(self._build_data_collector_report(ts))
            (r / "decision_summary.md").write_text(self._build_decision_summary_report(ts))
            (r / "decision_tracker.md").write_text(self._build_decision_tracker_report(ts))
            (r / "trigger_summary.md").write_text(self._build_trigger_summary_report(ts))
            (r / "trigger_summary_v2.md").write_text(self._build_trigger_summary_v2_report(ts))
            (r / "market_pnl.md").write_text(self._build_market_pnl_report(ts))

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
