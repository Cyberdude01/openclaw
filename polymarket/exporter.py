"""
Polymarket 15M — GitHub Data Exporter

Writes live market snapshots, signals, and trade fills to a cloned GitHub
repository (Cyberdude01/Bob) and pushes every EXPORT_INTERVAL seconds.

Three distinct reports are generated under reports/:
  data_collector.md   — Raw + calculated data log (last 500 snapshots)
  decision_summary.md — Analysis leading to every signal emitted
  decision_tracker.md — Every trade taken, with resolution and P&L

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
from datetime import datetime, timezone
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


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _auth_url() -> str:
    """Inject GITHUB_TOKEN into the HTTPS remote URL."""
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


# ─── Exporter ─────────────────────────────────────────────────────────────────

class DataExporter:
    """
    Snapshots MarketState + trading data to JSON files inside EXPORT_DIR
    and pushes them to the configured GitHub repository.

    Three Markdown reports are also generated under reports/ and pushed
    alongside the raw JSON data.
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

    # ── Market snapshot data ───────────────────────────────────────────────────

    def _market_snapshot(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for slug in SLUGS:
            sym  = SYMBOL_MAP.get(slug, slug)
            mkt  = self.state.get_current_market(slug)
            snap = self.state.analytics.get(sym).last_snapshot
            row: Dict[str, Any] = {"symbol": sym}
            if mkt:
                row.update({
                    "condition_id":  mkt.condition_id,
                    "elapsed_pct":   round(mkt.elapsed_pct, 4),
                    "remaining_sec": round(mkt.remaining_seconds, 1),
                    "up_price":      mkt.up_token.price   if mkt.up_token   else None,
                    "down_price":    mkt.down_token.price if mkt.down_token else None,
                    "arb":           mkt.arb_opportunity,
                })
            if snap:
                row.update({
                    "rv60":         round(snap.rv60,   6),
                    "eff60":        round(snap.eff60,  4),
                    "vol_bucket":   snap.vol_bucket.value,
                    "trend_bucket": snap.trend_bucket.value,
                    "up_price_ob":  round(snap.up_price,   4),
                    "down_price_ob":round(snap.down_price, 4),
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
        lines = [
            "# Data Collector Report",
            f"\n> **Updated:** `{ts}` &nbsp;|&nbsp; Last 500 market snapshots from SQLite\n",
            "| Time (UTC) | Symbol | UP Price | DOWN Price | Spread | Elapsed% | "
            "Vol Bucket | Trend | RV60 | Eff60 | P(UP)@60% | UP Trades | DN Trades | ARB |",
            "|------------|--------|----------|------------|--------|----------|"
            "-----------|-------|------|-------|-----------|-----------|-----------|-----|",
        ]

        rows = self.db.recent_snapshots(hours=48, limit=500) if self.db else []
        for r in rows:
            arb = f"{r['arb_profit']:.4f}" if r["arb_profit"] else "—"
            lines.append(
                f"| `{str(r['ts'])[:19]}` | {r['symbol']} "
                f"| {r['up_price']:.4f}   " if r["up_price"]   else "| —         "
                f"| {r['down_price']:.4f} " if r["down_price"] else "| —         "
                f"| {r['up_spread']:.4f}  " if r["up_spread"]  else "| —         "
                f"| {r['elapsed_pct']*100:.0f}% "
                f"| {r['vol_bucket'] or '—'} "
                f"| {r['trend_bucket'] or '—'} "
                f"| {r['rv60']:.5f}    "  if r["rv60"]   else "| —         "
                f"| {r['eff60']:.3f}   "  if r["eff60"]  else "| —      "
                f"| {r['dir_60pct']*100:.1f}% " if r["dir_60pct"] else "| —         "
                f"| {r['up_trade_count'] or 0} "
                f"| {r['down_trade_count'] or 0} "
                f"| {arb} |"
            )

        if not rows:
            lines.append("| — | No data yet | — | — | — | — | — | — | — | — | — | — | — | — |")

        lines += [
            "",
            "---",
            "_Auto-generated by [openclaw](https://github.com/Cyberdude01/openclaw)_",
        ]
        return "\n".join(lines) + "\n"

    # ── Report 2: Decision Engine Summary ──────────────────────────────────────

    def _build_decision_summary_report(self, ts: str) -> str:
        rows = self.db.all_signals(hours=48) if self.db else []

        # Aggregate by trigger type
        by_trigger: Dict[str, int] = {}
        for r in rows:
            t = r["trigger"] or "unknown"
            by_trigger[t] = by_trigger.get(t, 0) + 1

        lines = [
            "# Decision Engine Summary",
            f"\n> **Updated:** `{ts}` &nbsp;|&nbsp; All signals from the last 48 hours\n",
            "## Signal Distribution",
            "| Trigger | Count |",
            "|---------|-------|",
        ]
        for trigger, count in sorted(by_trigger.items(), key=lambda x: -x[1]):
            lines.append(f"| `{trigger}` | {count} |")

        lines += [
            "",
            "## Signal Log",
            "| Time (UTC) | Symbol | Outcome | Trigger | Confidence | P(UP) | Bucket | Elapsed% | Reasoning |",
            "|------------|--------|---------|---------|------------|-------|--------|----------|-----------|",
        ]
        for r in rows:
            bucket = f"{r['vol_bucket']}+{r['trend_bucket']}" if r["vol_bucket"] else "—"
            prob   = f"{r['prob_up']*100:.1f}%" if r["prob_up"] is not None else "—"
            el     = f"{r['elapsed_pct']*100:.0f}%" if r["elapsed_pct"] is not None else "—"
            # Truncate reasoning for table display; full text is preserved in DB
            reason = (r["reasoning"] or "")[:120].replace("|", "\\|")
            lines.append(
                f"| `{str(r['ts'])[:19]}` | {r['symbol']} | **{r['outcome']}** "
                f"| `{r['trigger'] or '—'}` | {r['confidence']:.3f} "
                f"| {prob} | {bucket} | {el} | {reason} |"
            )

        if not rows:
            lines.append("| — | No signals yet | — | — | — | — | — | — | — |")

        lines += [
            "",
            "---",
            "_Auto-generated by [openclaw](https://github.com/Cyberdude01/openclaw)_",
        ]
        return "\n".join(lines) + "\n"

    # ── Report 3: Decision Tracker ─────────────────────────────────────────────

    def _build_decision_tracker_report(self, ts: str) -> str:
        rows   = self.db.all_trades()  if self.db else []
        summary = self.db.trade_summary() if self.db else {}

        total = summary.get("total", 0) or 0
        wins  = summary.get("wins",  0) or 0
        losses= summary.get("losses",0) or 0
        arbs  = summary.get("arb_trades", 0) or 0
        pnl   = summary.get("total_pnl", 0.0) or 0.0
        win_rate = f"{wins/total*100:.1f}%" if total > 0 else "—"

        lines = [
            "# Decision Tracker",
            f"\n> **Updated:** `{ts}` &nbsp;|&nbsp; Historical log of every trade taken\n",
            "## Summary",
            "| Total Trades | Wins | Losses | ARB | Win Rate | Total P&L |",
            "|-------------|------|--------|-----|----------|-----------|",
            f"| {total} | {wins} | {losses} | {arbs} | {win_rate} | "
            f"{'+'if pnl>=0 else ''}${pnl:.4f} |",
            "",
            "## Trade Log",
            "| Entry Time | Symbol | Outcome | Trigger | Entry Price | Size | "
            "Mode | Resolved At | Winner | Result | P&L | Reasoning |",
            "|------------|--------|---------|---------|-------------|------|"
            "------|-------------|--------|--------|-----|-----------|",
        ]

        for r in rows:
            res_time = str(r["resolved_at"] or "")[:19] or "pending"
            winner   = r["resolution"] or "—"
            result   = r["result"]     or "pending"
            pnl_cell = f"{'+'if (r['pnl'] or 0)>=0 else ''}${(r['pnl'] or 0):.4f}" \
                       if r["pnl"] is not None else "pending"
            result_fmt = {
                "positive": "✅ +",
                "negative": "❌ −",
                "arb":      "💰 arb",
                "pending":  "⏳",
            }.get(result, result)
            reason = (r["reasoning"] or "")[:100].replace("|", "\\|")
            lines.append(
                f"| `{str(r['ts'])[:19]}` | {r['symbol']} | **{r['outcome']}** "
                f"| `{r['trigger'] or '—'}` | {r['entry_price']:.4f} "
                f"| ${r['size']:.2f} | {r['mode']} "
                f"| `{res_time}` | {winner} | {result_fmt} | {pnl_cell} | {reason} |"
            )

        if not rows:
            lines.append("| — | No trades yet | — | — | — | — | — | — | — | — | — | — |")

        lines += [
            "",
            "---",
            "_Auto-generated by [openclaw](https://github.com/Cyberdude01/openclaw)_",
        ]
        return "\n".join(lines) + "\n"

    # ── README ─────────────────────────────────────────────────────────────────

    def _build_readme(self, ts: str, markets: dict, trades: list, portfolio: dict) -> str:
        mode = "LIVE" if POLY_API_KEY else "PAPER"
        lines = [
            "# Polymarket 15M Data Feed",
            f"\n> **Mode:** {mode} &nbsp;|&nbsp; **Updated:** `{ts}`\n",
            "## Live Markets",
            "| Symbol | UP | DOWN | Elapsed | Bucket | Dir@60% | Dir@80% | Dir@90% |",
            "|--------|----|------|---------|--------|---------|---------|---------|",
        ]
        for sym, d in markets.items():
            up   = f"{d['up_price']:.3f}"   if d.get("up_price")   is not None else "—"
            dn   = f"{d['down_price']:.3f}" if d.get("down_price") is not None else "—"
            el   = f"{d.get('elapsed_pct',0)*100:.0f}%" if "elapsed_pct" in d else "—"
            bk   = f"{d.get('vol_bucket','—')}+{d.get('trend_bucket','—')}" if "vol_bucket" in d else "—"
            d60  = f"{d.get('dir_60pct',0.5)*100:.1f}%" if "dir_60pct" in d else "—"
            d80  = f"{d.get('dir_80pct',0.5)*100:.1f}%" if "dir_80pct" in d else "—"
            d90  = f"{d.get('dir_90pct',0.5)*100:.1f}%" if "dir_90pct" in d else "—"
            lines.append(f"| **{sym}** | {up} | {dn} | {el} | {bk} | {d60} | {d80} | {d90} |")

        if portfolio:
            pnl_color = "+" if portfolio.get("realized_pnl", 0) >= 0 else ""
            lines += [
                "\n## Portfolio",
                f"| Balance | Realized P&L |",
                f"|---------|-------------|",
                f"| ${portfolio.get('balance',0):.2f} | {pnl_color}${portfolio.get('realized_pnl',0):.4f} |",
            ]

        if trades:
            lines += [
                "\n## Recent Fills (last 10)",
                "| Time (UTC) | Symbol | Outcome | Side | Size | Price | Trigger | Mode |",
                "|------------|--------|---------|------|------|-------|---------|------|",
            ]
            for t in trades[-10:]:
                lines.append(
                    f"| `{t.get('ts','')[:19]}` | {t.get('symbol','')} | "
                    f"{t.get('outcome','')} | {t.get('side','')} | "
                    f"${t.get('size',0):.2f} | {t.get('price',0):.4f} | "
                    f"`{t.get('trigger','—')}` | **{t.get('mode','').upper()}** |"
                )

        lines += [
            "",
            "## Reports",
            "| Report | Description |",
            "|--------|-------------|",
            "| [Data Collector](reports/data_collector.md) | Raw + calculated data log (last 48h) |",
            "| [Decision Summary](reports/decision_summary.md) | Analysis behind every signal |",
            "| [Decision Tracker](reports/decision_tracker.md) | Full trade history with P&L |",
            "",
            "---",
            "_Auto-generated by [openclaw](https://github.com/Cyberdude01/openclaw)_",
        ]
        return "\n".join(lines) + "\n"

    # ── Snapshot + push ───────────────────────────────────────────────────────

    def _snapshot(self) -> None:
        ts       = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        markets  = self._market_snapshot()
        trades   = list(self.exec_log)[-50:]
        signals  = [
            {
                "symbol":     s.symbol,
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
            {"balance": round(self.book.balance, 2), "realized_pnl": round(self.book.realized_pnl, 4)}
            if self.book else {}
        )

        d = EXPORT_DIR / "data_exports"
        (d / "markets.json").write_text(json.dumps({"updated": ts, "data": markets}, indent=2))
        (d / "trades.json").write_text(json.dumps({"updated": ts, "data": trades}, indent=2))
        (d / "signals.json").write_text(json.dumps({"updated": ts, "data": signals}, indent=2))
        (d / "portfolio.json").write_text(json.dumps({"updated": ts, **portfolio}, indent=2))

        # Three distinct Markdown reports (read from DB if available)
        r = EXPORT_DIR / "reports"
        if self.db:
            (r / "data_collector.md").write_text(self._build_data_collector_report(ts))
            (r / "decision_summary.md").write_text(self._build_decision_summary_report(ts))
            (r / "decision_tracker.md").write_text(self._build_decision_tracker_report(ts))

        (EXPORT_DIR / "README.md").write_text(self._build_readme(ts, markets, trades, portfolio))

    def _push(self) -> None:
        _git(["add", "-A"])
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
