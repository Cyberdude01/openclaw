"""
Polymarket 15M — Adaptive Feedback / Reinforcement Learning

Reads historical trade outcomes from the database and adjusts the
DecisionEngine's entry thresholds in real-time, so the system learns
from its own mistakes without human intervention.

Strategy
--------
After every REFRESH_INTERVAL seconds (default 5 min), re-read the
trades_executed table and compute per-(trigger, outcome) win rates.

Threshold adjustment rules (applied once MIN_SAMPLES resolved trades exist):

  Win rate < 40 %  → suppress this trigger+direction entirely
  Win rate 40–50 % → raise edge requirement by 50 % (more cautious)
  Win rate 50–65 % → use base edge (no change)
  Win rate  > 65 % → lower edge requirement by 25 % (more aggressive)

Opposing-entry guard
--------------------
If the engine already has an open (unresolved) trade for a condition_id
in one direction, the opposite direction is blocked for the same window.
This prevents the paired UP+DOWN cancellation observed in trend_follow.

Stale price guard
-----------------
In a binary market UP + DOWN ≈ 1.0.  When both tokens show the same
high price (e.g., both 0.99 due to stale order books), the combined
cost exceeds 1.1 — an impossible state.  Signals generated from such
stale data are blocked.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, Set

from .config import MIN_EDGE

if TYPE_CHECKING:
    from .database import Database


# ── Tuning constants ──────────────────────────────────────────────────────────

REFRESH_INTERVAL = 300      # Recompute thresholds every 5 minutes
MIN_SAMPLES      = 10       # Need at least this many resolved trades to adapt
SUPPRESS_BELOW   = 0.40     # Suppress trigger+direction if win rate < 40%
CAUTIOUS_BELOW   = 0.50     # Raise edge 50% if win rate 40-50%
AGGRESSIVE_ABOVE = 0.65     # Lower edge 25% if win rate > 65%
MAX_COMBINED_PRICE = 1.10   # UP + DOWN price cap (stale-book guard)


# ── AdaptiveThresholds ────────────────────────────────────────────────────────

class AdaptiveThresholds:
    """
    Singleton-style object owned by the DecisionEngine.
    Call refresh() periodically; consult edge_for() before each signal.
    """

    def __init__(self, db: "Database"):
        self._db: "Database"         = db
        self._edges: Dict[str, float] = {}   # "trigger:outcome" → min edge
        self._suppressed: Set[str]   = set()
        self._last_refresh: float    = 0.0
        self.stats: Dict[str, dict]  = {}    # for logging / export

    # ── Public API ────────────────────────────────────────────────────────────

    def maybe_refresh(self) -> None:
        """Refresh thresholds if REFRESH_INTERVAL has elapsed."""
        if time.time() - self._last_refresh >= REFRESH_INTERVAL:
            self.refresh()

    def refresh(self) -> None:
        """Force a threshold recomputation from the database."""
        rows = self._db.win_rates_by_trigger()
        new_edges: Dict[str, float] = {}
        new_suppressed: Set[str]    = set()
        new_stats: Dict[str, dict]  = {}

        for row in rows:
            trigger = row.get("trigger") or ""
            outcome = row.get("outcome") or ""
            wins    = int(row.get("wins",   0) or 0)
            losses  = int(row.get("losses", 0) or 0)
            resolved = wins + losses
            avg_pnl  = float(row.get("avg_pnl") or 0.0)

            key = f"{trigger}:{outcome}"
            new_stats[key] = {
                "wins": wins, "losses": losses,
                "resolved": resolved, "avg_pnl": avg_pnl,
                "win_rate": wins / resolved if resolved else None,
            }

            if resolved < MIN_SAMPLES:
                # Not enough data — use base edge, don't suppress
                new_edges[key] = MIN_EDGE
                continue

            win_rate = wins / resolved

            if win_rate < SUPPRESS_BELOW:
                new_suppressed.add(key)
                new_edges[key] = float("inf")      # effectively blocked
            elif win_rate < CAUTIOUS_BELOW:
                new_edges[key] = MIN_EDGE * 1.5    # require stronger signal
            elif win_rate > AGGRESSIVE_ABOVE:
                new_edges[key] = MIN_EDGE * 0.75   # accept weaker signal
            else:
                new_edges[key] = MIN_EDGE           # no change

        self._edges      = new_edges
        self._suppressed = new_suppressed
        self.stats       = new_stats
        self._last_refresh = time.time()

    def edge_for(self, trigger: str, outcome: str) -> float:
        """Return the current minimum edge required for this trigger+direction."""
        return self._edges.get(f"{trigger}:{outcome}", MIN_EDGE)

    def is_suppressed(self, trigger: str, outcome: str) -> bool:
        """True if this trigger+direction is blocked by poor historical performance."""
        return f"{trigger}:{outcome}" in self._suppressed

    def opposing_entry_exists(self, condition_id: str, outcome: str) -> bool:
        """
        True if an open trade exists for the opposite direction on the same
        market window.  Prevents simultaneous UP+DOWN bets.
        """
        return self._db.opposing_entries_this_window(condition_id, outcome) > 0

    def stale_prices(self, up_price: float, down_price: float) -> bool:
        """
        True when UP + DOWN implies an impossible combined cost.
        Signals generated from stale order books are blocked.
        """
        return (up_price + down_price) > MAX_COMBINED_PRICE

    def summary_lines(self) -> list[str]:
        """Return human-readable lines for dashboard / log output."""
        lines = ["[bold]Adaptive Thresholds[/bold]"]
        for key, s in sorted(self.stats.items()):
            wr = s["win_rate"]
            edge = self._edges.get(key, MIN_EDGE)
            suppressed = key in self._suppressed
            wr_str   = f"{wr*100:.0f}%" if wr is not None else "—"
            flag     = " [red]SUPPRESSED[/red]" if suppressed else ""
            lines.append(
                f"  {key:<30}  wins={s['wins']:>3}  losses={s['losses']:>3}"
                f"  wr={wr_str:>5}  edge={edge:.3f}{flag}"
            )
        if not self.stats:
            lines.append("  [dim]No resolved trades yet — using defaults[/dim]")
        return lines
