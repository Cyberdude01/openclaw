"""
Polymarket 15M Decision Engine

Agent 2 of 3 — responsible for:
  - Reading market state and analytics snapshots
  - Detecting arbitrage opportunities
  - Generating high-conviction TradeSignals
  - Filtering signals by risk rules (size, edge, position limits)

The decision engine is deliberately separate from the executor so that:
  - Signal logic can be backtested without touching exchange APIs
  - Risk parameters can be tuned independently
  - The executor can paper-trade or live-trade without changing this module
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

from .analytics import AnalyticsRegistry
from .collector import MarketState
from .config import (
    ARB_THRESHOLD,
    DECISION_THRESHOLDS,
    FEE_RATE,
    MARKET_DURATION_SECONDS,
    MAX_POSITION,
    MAX_TRADE_SIZE,
    MIN_EDGE,
    MIN_TRADE_SIZE,
    SLUGS,
    SYMBOL_MAP,
    TRADE_SIZE,
)
from .database import Database
from .feedback import AdaptiveThresholds
from .models import (
    AnalyticsSnapshot,
    MarketInfo,
    MarketStatus,
    Outcome,
    Side,
    TradeSignal,
    VolBucket,
    TrendBucket,
)


# ─── Position Tracker ─────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol:       str
    condition_id: str
    outcome:      Outcome
    size:         float    # USDC notional
    avg_price:    float
    pnl:          float = 0.0

    def update(self, fill_price: float, fill_size: float, side: Side):
        if side == Side.BUY:
            old_cost   = self.avg_price * self.size
            self.size += fill_size
            self.avg_price = (old_cost + fill_price * fill_size) / self.size if self.size else fill_price
        else:  # SELL — reduce position
            self.pnl  += (fill_price - self.avg_price) * fill_size
            self.size  = max(0.0, self.size - fill_size)


class PositionBook:
    """Tracks open positions and USDC balance."""

    def __init__(self, initial_balance: float = 0.0):
        self._positions: Dict[str, Position] = {}   # key = condition_id+outcome
        self.balance    = initial_balance
        self.realized_pnl = 0.0

    def _key(self, condition_id: str, outcome: Outcome) -> str:
        return f"{condition_id}:{outcome.value}"

    def get_position(self, condition_id: str, outcome: Outcome) -> Optional[Position]:
        return self._positions.get(self._key(condition_id, outcome))

    def position_size(self, condition_id: str, outcome: Outcome) -> float:
        p = self.get_position(condition_id, outcome)
        return p.size if p else 0.0

    def total_exposure(self, symbol: str) -> float:
        return sum(p.size for p in self._positions.values() if p.symbol == symbol)

    def record_fill(self, signal: TradeSignal, fill_price: float):
        key = self._key(signal.condition_id, signal.outcome)
        if key not in self._positions:
            self._positions[key] = Position(
                symbol       = signal.symbol,
                condition_id = signal.condition_id,
                outcome      = signal.outcome,
                size         = 0.0,
                avg_price    = fill_price,
            )
        self._positions[key].update(fill_price, signal.size, signal.side)
        if signal.side == Side.BUY:
            self.balance -= signal.size * fill_price
        else:
            self.balance += signal.size * fill_price
            self.realized_pnl += self._positions[key].pnl

    def summary(self) -> str:
        lines = [f"Balance: ${self.balance:.2f}  Realized PnL: ${self.realized_pnl:.4f}"]
        for k, p in self._positions.items():
            if p.size > 0:
                lines.append(f"  {p.symbol} {p.outcome.value}: {p.size:.2f} @ {p.avg_price:.4f}")
        return "\n".join(lines)


# ─── Signal Generators ────────────────────────────────────────────────────────

class DecisionEngine:
    """
    Analyses a MarketState snapshot and produces TradeSignals.

    Strategy overview
    -----------------
    1. Arbitrage (highest priority)
       If UP_ask + DOWN_ask < ARB_THRESHOLD, buy both legs.
       Near-zero risk; execute immediately.

    2. Directional at timing thresholds (60 / 80 / 90% elapsed)
       Use the direction_probability from analytics:
         - If prob_up > 0.5 + MIN_EDGE → buy UP
         - If prob_up < 0.5 - MIN_EDGE → buy DOWN (i.e., the DOWN token)
       Confidence scales trade size (MIN_TRADE_SIZE … MAX_TRADE_SIZE).

    3. Trend-following inside HighVol+Trend bucket
       When up_price deviates strongly from 0.5 and bucket = HighVol+Trend,
       enter in the direction of the trend regardless of timing threshold.
    """

    def __init__(self, state: MarketState, book: PositionBook, db: Optional[Database] = None):
        self.state      = state
        self.book       = book
        self.db         = db
        self.signal_log: Deque[TradeSignal] = deque(maxlen=50)
        self.adaptive   = AdaptiveThresholds(db) if db else None

    def generate_signals(self) -> List[TradeSignal]:
        # Refresh adaptive thresholds from DB if interval has elapsed
        if self.adaptive:
            self.adaptive.maybe_refresh()

        signals: List[TradeSignal] = []
        for slug in SLUGS:
            symbol = SYMBOL_MAP.get(slug, slug)
            mkt    = self.state.get_current_market(slug)
            if not mkt or not mkt.is_active:
                continue
            snap = self.state.analytics.get(symbol).last_snapshot

            signals.extend(self._arb_signals(mkt, symbol))
            if snap:
                signals.extend(self._directional_signals(mkt, symbol, snap))
                signals.extend(self._trend_signals(mkt, symbol, snap))
                signals.extend(self._forced_trade_signals(mkt, symbol, snap))
        return signals

    # ── Arbitrage ─────────────────────────────────────────────────────────────

    def _arb_signals(self, mkt: MarketInfo, symbol: str) -> List[TradeSignal]:
        arb = mkt.arb_opportunity
        if not arb:
            return []
        signals = []
        cost_per_contract = (
            (mkt.up_token.order_book.best_ask   if mkt.up_token   and mkt.up_token.order_book   else 0.5) +
            (mkt.down_token.order_book.best_ask if mkt.down_token and mkt.down_token.order_book else 0.5)
        )
        # Max we can spend while not exceeding position cap
        available = min(MAX_TRADE_SIZE, MAX_POSITION - self.book.total_exposure(symbol))
        if available < MIN_TRADE_SIZE:
            return []
        # USDC to invest: splits between both legs
        size = available / 2

        for token, outcome, price in [
            (mkt.up_token,   Outcome.UP,   mkt.up_token.order_book.best_ask   if mkt.up_token   and mkt.up_token.order_book   else 0.5),
            (mkt.down_token, Outcome.DOWN, mkt.down_token.order_book.best_ask if mkt.down_token and mkt.down_token.order_book else 0.5),
        ]:
            if token:
                signals.append(TradeSignal(
                    symbol       = symbol,
                    market_id    = mkt.market_id,
                    condition_id = mkt.condition_id,
                    token_id     = token.token_id,
                    outcome      = outcome,
                    side         = Side.BUY,
                    size         = size,
                    price        = price,
                    confidence   = 0.99,
                    trigger      = "arb",
                    reason       = (
                        f"ARBITRAGE — both legs cost {cost_per_contract:.4f} total "
                        f"(threshold {ARB_THRESHOLD}), guaranteeing "
                        f"{1.0 - cost_per_contract:.4f} profit per dollar. "
                        f"Buying {outcome.value} leg @ {price:.4f}."
                    ),
                ))
        return signals

    # ── Directional ───────────────────────────────────────────────────────────

    def _directional_signals(
        self, mkt: MarketInfo, symbol: str, snap: AnalyticsSnapshot,
    ) -> List[TradeSignal]:
        signals = []
        pct = mkt.elapsed_pct

        # Evaluate at each timing threshold (only trigger once per threshold window)
        for label, threshold in DECISION_THRESHOLDS.items():
            # Within 30 seconds of the threshold → fire signal
            delta = abs(pct - threshold)
            if delta > (30 / MARKET_DURATION_SECONDS):
                continue

            dir_probs = {
                "60pct": snap.dir_60pct,
                "80pct": snap.dir_80pct,
                "90pct": snap.dir_90pct,
            }
            prob_up = dir_probs.get(label, 0.5)

            # Determine direction before adaptive checks
            if prob_up > 0.5:
                outcome = Outcome.UP
                token   = mkt.up_token
                price   = token.order_book.best_ask if token and token.order_book else 0.5
            else:
                outcome = Outcome.DOWN
                token   = mkt.down_token
                price   = token.order_book.best_ask if token and token.order_book else 0.5

            if not token:
                continue

            trigger_tag = f"directional_{label}"

            # Adaptive: skip if historically suppressed
            if self.adaptive and self.adaptive.is_suppressed(trigger_tag, outcome.value):
                continue

            # Adaptive: use per-(trigger, direction) edge threshold
            min_edge_req = (
                self.adaptive.edge_for(trigger_tag, outcome.value)
                if self.adaptive else MIN_EDGE
            )

            edge = abs(prob_up - 0.5)
            if edge < min_edge_req:
                continue

            # Opposing-entry guard: don't bet both sides of the same window
            if self.adaptive and self.adaptive.opposing_entry_exists(mkt.condition_id, outcome.value):
                continue

            # Fixed trade size: $5 per trade
            confidence = 0.5 + edge
            size = min(TRADE_SIZE, MAX_POSITION - self.book.total_exposure(symbol))
            if size < MIN_TRADE_SIZE:
                continue

            direction_word = "UP" if prob_up > 0.5 else "DOWN"
            adaptive_note  = (
                f" [adaptive edge={min_edge_req:.3f}]" if self.adaptive else ""
            )
            signals.append(TradeSignal(
                symbol       = symbol,
                market_id    = mkt.market_id,
                condition_id = mkt.condition_id,
                token_id     = token.token_id,
                outcome      = outcome,
                side         = Side.BUY,
                size         = round(size, 2),
                price        = round(price, 4),
                confidence   = round(confidence, 3),
                trigger      = trigger_tag,
                reason       = (
                    f"DIRECTIONAL at {label} ({pct*100:.0f}% elapsed) — "
                    f"P(UP)={prob_up:.3f} gives edge={edge:.3f} toward {direction_word}. "
                    f"Bucket={snap.vol_bucket.value}+{snap.trend_bucket.value} "
                    f"(RV60={snap.rv60:.5f}, Eff60={snap.eff60:.3f}). "
                    f"Fixed $5 USDC stake @ conf={round(confidence, 3):.3f}."
                    f"{adaptive_note}"
                ),
            ))
        return signals

    # ── Trend Following ───────────────────────────────────────────────────────

    def _trend_signals(
        self, mkt: MarketInfo, symbol: str, snap: AnalyticsSnapshot,
    ) -> List[TradeSignal]:
        """
        In a HighVol+Trend bucket, if the price has already deviated
        significantly from 0.5, follow the momentum.
        """
        if not (snap.vol_bucket == VolBucket.HIGH and snap.trend_bucket == TrendBucket.TREND):
            return []
        # Only enter in the first 70% of the market
        if mkt.elapsed_pct > 0.70:
            return []

        up_p    = snap.up_price
        down_p  = snap.down_price

        # Stale-price guard: in a binary market UP + DOWN ≈ 1.0.
        # If both tokens show a high price (e.g. both 0.99), order books are stale.
        if self.adaptive and self.adaptive.stale_prices(up_p, down_p):
            return []

        dev_up  = up_p   - 0.50   # positive when UP  is the winning side (>0.50)
        dev_dn  = down_p - 0.50   # positive when DOWN is the winning side (>0.50)

        signals = []
        for dev, outcome, price_attr in [
            (dev_up, Outcome.UP,   "best_ask"),
            (dev_dn, Outcome.DOWN, "best_ask"),   # only fires when DOWN > 0.58
        ]:
            if dev < 0.08:     # need at least 8 cent deviation from 50
                continue
            token = mkt.up_token if outcome == Outcome.UP else mkt.down_token
            if not token or not token.order_book:
                continue

            # Adaptive: skip if historically suppressed
            if self.adaptive and self.adaptive.is_suppressed("trend_follow", outcome.value):
                continue

            # Opposing-entry guard: don't bet both sides of the same window
            if self.adaptive and self.adaptive.opposing_entry_exists(mkt.condition_id, outcome.value):
                continue

            price = getattr(token.order_book, price_attr, 0.5)
            size  = min(TRADE_SIZE, MAX_POSITION - self.book.total_exposure(symbol))
            if size < MIN_TRADE_SIZE:
                continue

            signals.append(TradeSignal(
                symbol       = symbol,
                market_id    = mkt.market_id,
                condition_id = mkt.condition_id,
                token_id     = token.token_id,
                outcome      = outcome,
                side         = Side.BUY,
                size         = round(size, 2),
                price        = round(price, 4),
                confidence   = round(0.50 + dev, 3),
                trigger      = "trend_follow",
                reason       = (
                    f"TREND FOLLOW (HighVol+Trend, {mkt.elapsed_pct*100:.0f}% elapsed) — "
                    f"{outcome.value} token at {price:.3f} deviates {dev:.3f} from 0.50. "
                    f"Momentum continuation strategy: buying the already-winning leg. "
                    f"Fixed $5 USDC stake."
                ),
            ))
        return signals

    # ── Forced Trade (every market must be entered) ───────────────────────────

    def _forced_trade_signals(
        self, mkt: MarketInfo, symbol: str, snap: AnalyticsSnapshot,
    ) -> List[TradeSignal]:
        """
        Guarantee at least one trade per market window.

        Fires at the 60% elapsed mark if no trade has yet been placed for this
        condition_id (checked in both the in-memory PositionBook and the DB).
        Direction is chosen by whichever side has the higher probability estimate.
        Size is always TRADE_SIZE ($5).
        """
        # Only fire within 30 s of the 60% threshold
        delta = abs(mkt.elapsed_pct - DECISION_THRESHOLDS["60pct"])
        if delta > (30 / MARKET_DURATION_SECONDS):
            return []

        # Skip if already in a position in memory
        for outcome in (Outcome.UP, Outcome.DOWN):
            if self.book.position_size(mkt.condition_id, outcome) > 0:
                return []

        # Skip if the DB already has a trade for this market window
        if self.db and self.db.has_trade_for_condition(mkt.condition_id):
            return []

        # Choose direction based on the 60% probability estimate
        prob_up = snap.dir_60pct if snap else 0.5
        if prob_up >= 0.5:
            outcome = Outcome.UP
            token   = mkt.up_token
        else:
            outcome = Outcome.DOWN
            token   = mkt.down_token

        if not token or not token.order_book:
            return []

        # Stale-price guard
        if self.adaptive and snap and self.adaptive.stale_prices(snap.up_price, snap.down_price):
            return []

        price = token.order_book.best_ask
        size  = min(TRADE_SIZE, MAX_POSITION - self.book.total_exposure(symbol))
        if size < MIN_TRADE_SIZE:
            return []

        return [TradeSignal(
            symbol       = symbol,
            market_id    = mkt.market_id,
            condition_id = mkt.condition_id,
            token_id     = token.token_id,
            outcome      = outcome,
            side         = Side.BUY,
            size         = round(size, 2),
            price        = round(price, 4),
            confidence   = round(0.5 + abs(prob_up - 0.5), 3),
            trigger      = "forced_edge" if abs(prob_up - 0.5) > MIN_EDGE else "forced_coin",
            reason       = (
                f"FORCED TRADE at 60% elapsed — no prior signal for this window. "
                f"P(UP)={prob_up:.3f}, choosing {outcome.value}. "
                f"Fixed $5 USDC stake (every market must be entered)."
            ),
        )]

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def run(self, signal_queue: asyncio.Queue, interval: float = 2.0):
        """
        Continuously generate signals, push them to the trader queue,
        and persist them to the database.
        """
        while True:
            signals = self.generate_signals()
            for sig in signals:
                self.signal_log.append(sig)
                await signal_queue.put(sig)
                if self.db:
                    try:
                        # Fetch analytics snapshot for extra context
                        snap = self.state.analytics.get(sig.symbol).last_snapshot
                        slug = next((k for k, v in SYMBOL_MAP.items() if v == sig.symbol), None)
                        mkt  = self.state.get_current_market(slug) if slug else None
                        self.db.insert_signal({
                            "ts":           datetime.now(timezone.utc).isoformat(),
                            "symbol":       sig.symbol,
                            "condition_id": sig.condition_id,
                            "token_id":     sig.token_id,
                            "outcome":      sig.outcome.value,
                            "side":         sig.side.value,
                            "size":         sig.size,
                            "price":        sig.price,
                            "confidence":   sig.confidence,
                            "trigger":      sig.trigger,
                            "reasoning":    sig.reason,
                            "vol_bucket":   snap.vol_bucket.value   if snap else None,
                            "trend_bucket": snap.trend_bucket.value if snap else None,
                            "prob_up":      snap.dir_60pct          if snap else None,
                            "elapsed_pct":  mkt.elapsed_pct         if mkt  else None,
                            "arb_profit":   mkt.arb_opportunity      if mkt  else None,
                        })
                    except Exception:
                        pass  # Never let DB errors interrupt signal flow
            await asyncio.sleep(interval)
