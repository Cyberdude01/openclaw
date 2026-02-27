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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

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
)
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

    def __init__(self, state: MarketState, book: PositionBook):
        self.state = state
        self.book  = book

    def generate_signals(self) -> List[TradeSignal]:
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
                    reason       = f"ARB: UP+DOWN={cost_per_contract:.4f} < {ARB_THRESHOLD}",
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
            edge = abs(prob_up - 0.5)
            if edge < MIN_EDGE:
                continue

            # Determine side and token
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

            # Size scales with edge; higher confidence → larger trade
            confidence = 0.5 + edge
            size = MIN_TRADE_SIZE + (MAX_TRADE_SIZE - MIN_TRADE_SIZE) * (edge / 0.5)
            size = min(size, MAX_POSITION - self.book.total_exposure(symbol))
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
                confidence   = round(confidence, 3),
                reason       = (
                    f"{label} trigger: P(UP)={prob_up:.3f}  "
                    f"bucket={snap.vol_bucket.value}+{snap.trend_bucket.value}"
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
        dev_up  = up_p   - 0.50
        dev_dn  = down_p - 0.50

        signals = []
        for dev, outcome, price_attr in [
            (dev_up,  Outcome.UP,   "best_ask"),
            (-dev_dn, Outcome.DOWN, "best_ask"),
        ]:
            if dev < 0.08:     # need at least 8 cent deviation from 50
                continue
            token = mkt.up_token if outcome == Outcome.UP else mkt.down_token
            if not token or not token.order_book:
                continue
            price = getattr(token.order_book, price_attr, 0.5)
            size  = min(
                MIN_TRADE_SIZE + (MAX_TRADE_SIZE - MIN_TRADE_SIZE) * min(dev / 0.25, 1.0),
                MAX_POSITION - self.book.total_exposure(symbol),
            )
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
                reason       = f"HighVol+Trend momentum: {outcome.value} price={price:.3f}",
            ))
        return signals

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def run(self, signal_queue: asyncio.Queue, interval: float = 2.0):
        """
        Continuously generate signals and push them to the queue.
        The trader agent consumes from this queue.
        """
        while True:
            signals = self.generate_signals()
            for sig in signals:
                await signal_queue.put(sig)
            await asyncio.sleep(interval)
