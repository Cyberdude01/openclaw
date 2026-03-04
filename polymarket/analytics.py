"""
Polymarket 15M Analytics Engine

Implements:
  - 1-minute log returns and realized volatility (RV60)
  - Efficiency ratio for trend detection (Eff60)
  - 4-bucket classification: LowVol/HighVol × Range/Trend
  - Historical probability table  P_B(X) = Pr(|R_rem| ≤ X | bucket B)
  - Direction probability at 60%, 80%, 90% of the 15-minute market
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from .config import (
    LOOKBACK_BARS,
    MIN_HISTORY_BARS,
    X_THRESHOLDS,
    DECISION_THRESHOLDS,
    MARKET_DURATION_SECONDS,
)
from .models import (
    AnalyticsSnapshot,
    BucketLabel,
    MarketInfo,
    MinuteBar,
    Outcome,
    TrendBucket,
    VolBucket,
)


# ─── Historical Candle Storage ────────────────────────────────────────────────

class CandleRecord:
    """One completed 15-minute candle with its bucket label and R_rem."""
    __slots__ = ("bucket", "rrem", "p12", "close")

    def __init__(self, bucket: BucketLabel, rrem: float, p12: float, close: float):
        self.bucket = bucket
        self.rrem   = rrem    # (close - p12) / p12
        self.p12    = p12
        self.close  = close


# ─── Per-Symbol Analytics State ───────────────────────────────────────────────

class MarketAnalytics:
    """
    Tracks 1-minute price history for one symbol and computes:
      - Realized volatility (RV60)
      - Efficiency ratio (Eff60)
      - Vol/Trend bucket classification
      - Probability table from historical candle data
      - Direction probability (UP vs DOWN) at key timepoints
    """

    def __init__(self, symbol: str, max_history: int = 120):
        self.symbol = symbol

        # Rolling 1-minute price bars (keep 2× LOOKBACK for safety)
        self._bars: Deque[MinuteBar] = deque(maxlen=max_history)

        # Historical RV60 / Eff60 values for computing medians
        self._rv60_history:  Deque[float] = deque(maxlen=2000)
        self._eff60_history: Deque[float] = deque(maxlen=2000)

        # Historical candle outcomes for the probability table
        self._candles: List[CandleRecord] = []

        # Last computed analytics
        self._last_snapshot: Optional[AnalyticsSnapshot] = None

    # ── Data Ingestion ─────────────────────────────────────────────────────────

    def add_price(self, ts: float, price: float, volume: float = 0.0):
        """Add a 1-minute bar. ts is a UNIX timestamp (seconds)."""
        if price <= 0:
            return
        # Avoid duplicate timestamps
        if self._bars and abs(self._bars[-1].timestamp - ts) < 30:
            # Update the most recent bar in-place
            self._bars[-1] = MinuteBar(ts, price, volume)
        else:
            self._bars.append(MinuteBar(ts, price, volume))

    def record_candle(self, bucket: BucketLabel, p12: float, close: float):
        """Record a completed 15-minute candle for the probability table."""
        if p12 > 0:
            rrem = (close - p12) / p12
            self._candles.append(CandleRecord(bucket, rrem, p12, close))

    # ── Core Computations ──────────────────────────────────────────────────────

    def _log_returns(self) -> List[float]:
        prices = [b.price for b in self._bars]
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0 and prices[i] > 0:
                returns.append(math.log(prices[i] / prices[i - 1]))
        return returns

    def compute_rv60(self) -> Optional[float]:
        """
        Realized volatility over the last 60 one-minute bars:
          RV60 = sqrt( Σ r_t² )
        """
        returns = self._log_returns()
        if len(returns) < MIN_HISTORY_BARS:
            return None
        window = returns[-LOOKBACK_BARS:]
        return math.sqrt(sum(r * r for r in window))

    def compute_eff60(self) -> Optional[float]:
        """
        Efficiency ratio over the last 60 one-minute bars:
          Eff60 = |P_now - P_60m_ago| / Σ|P_k - P_{k-1}|
        Returns 0.0 when price is flat; 1.0 for perfectly trending.
        """
        prices = [b.price for b in self._bars]
        if len(prices) < MIN_HISTORY_BARS + 1:
            return None
        window = prices[-(LOOKBACK_BARS + 1):]
        net_move  = abs(window[-1] - window[0])
        total_path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
        return net_move / total_path if total_path > 0 else 0.0

    # ── Bucket Classification ─────────────────────────────────────────────────

    def _median(self, values: Deque[float]) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        mid = n // 2
        return sorted_vals[mid] if n % 2 else (sorted_vals[mid - 1] + sorted_vals[mid]) / 2

    def _vol_bucket(self, rv60: float) -> VolBucket:
        if not self._rv60_history:
            # Bootstrap: typical realized vol for crypto token probabilities ~0.003
            return VolBucket.LOW if rv60 < 0.003 else VolBucket.HIGH
        return VolBucket.LOW if rv60 <= self._median(self._rv60_history) else VolBucket.HIGH

    def _trend_bucket(self, eff60: float) -> TrendBucket:
        if not self._eff60_history:
            # Bootstrap: efficiency > 0.30 is generally trending
            return TrendBucket.RANGE if eff60 <= 0.30 else TrendBucket.TREND
        return TrendBucket.RANGE if eff60 <= self._median(self._eff60_history) else TrendBucket.TREND

    def get_buckets(self) -> Optional[Tuple[BucketLabel, float, float]]:
        """
        Returns (bucket_label, rv60, eff60) or None if not enough data.
        Side-effect: updates RV60/Eff60 history deques.
        """
        rv60  = self.compute_rv60()
        eff60 = self.compute_eff60()
        if rv60 is None or eff60 is None:
            return None
        self._rv60_history.append(rv60)
        self._eff60_history.append(eff60)
        vol   = self._vol_bucket(rv60)
        trend = self._trend_bucket(eff60)
        return BucketLabel(vol, trend), rv60, eff60

    # ── Probability Table ─────────────────────────────────────────────────────

    # Bootstrap priors (theory-based, used before ≥30 historical candles)
    _PRIORS: Dict[Tuple[VolBucket, TrendBucket], Dict[float, float]] = {
        (VolBucket.LOW,  TrendBucket.RANGE): {0.08: 0.65, 0.12: 0.74, 0.20: 0.87},
        (VolBucket.LOW,  TrendBucket.TREND): {0.08: 0.52, 0.12: 0.63, 0.20: 0.80},
        (VolBucket.HIGH, TrendBucket.RANGE): {0.08: 0.44, 0.12: 0.56, 0.20: 0.71},
        (VolBucket.HIGH, TrendBucket.TREND): {0.08: 0.36, 0.12: 0.47, 0.20: 0.63},
    }

    def probability_rrem(self, bucket: BucketLabel, x_pct: float) -> float:
        """
        P_B(X) = Pr(|R_rem| ≤ X% | bucket B)
        Uses historical data when ≥30 candles in bucket; falls back to priors.
        """
        matching = [c for c in self._candles
                    if c.bucket.vol == bucket.vol and c.bucket.trend == bucket.trend]
        if len(matching) < 30:
            return self._PRIORS.get((bucket.vol, bucket.trend), {}).get(x_pct, 0.50)
        within = sum(1 for c in matching if abs(c.rrem * 100) <= x_pct)
        return within / len(matching)

    def probability_table(self, bucket: BucketLabel) -> Dict[float, float]:
        """Returns {x_pct: probability} for all configured X thresholds."""
        return {x: self.probability_rrem(bucket, x) for x in X_THRESHOLDS}

    # ── Direction Probability at 60 / 80 / 90% ───────────────────────────────

    def direction_probability(
        self,
        up_price: float,
        bucket: BucketLabel,
        elapsed_pct: float,
    ) -> float:
        """
        Estimate the probability that the market resolves UP.

        Base signal: the UP token price is already the market's best estimate.
        Adjustments:
          - HighVol shrinks conviction toward 0.5 (more uncertainty)
          - Trend bucket strengthens conviction (momentum continuation)
          - Range bucket weakens conviction (mean-reversion expected)
          - Later in the market → stronger signal from current price
        Returns a value in [0, 1] where >0.5 favours UP.
        """
        if up_price <= 0 or up_price >= 1:
            return up_price

        # Distance from 50%
        edge = up_price - 0.50

        # Timing multiplier: signal strengthens as market nears close
        time_mult = 0.5 + 0.5 * elapsed_pct   # 0.5 at open → 1.0 at close

        # Bucket adjustment
        if bucket.vol == VolBucket.HIGH and bucket.trend == TrendBucket.TREND:
            bucket_mult = 1.10   # momentum – follow the price
        elif bucket.vol == VolBucket.LOW and bucket.trend == TrendBucket.TREND:
            bucket_mult = 1.05
        elif bucket.vol == VolBucket.LOW and bucket.trend == TrendBucket.RANGE:
            bucket_mult = 0.90   # reversion likely
        else:  # HighVol + Range
            bucket_mult = 0.80   # unpredictable

        adjusted_edge = edge * time_mult * bucket_mult
        return max(0.0, min(1.0, 0.50 + adjusted_edge))

    # ── Full Snapshot ─────────────────────────────────────────────────────────

    def compute_snapshot(
        self,
        market: MarketInfo,
    ) -> Optional[AnalyticsSnapshot]:
        """
        Compute and return a full AnalyticsSnapshot for the given market.
        Returns None if there isn't enough price history.
        """
        result = self.get_buckets()
        if result is None:
            return None
        bucket, rv60, eff60 = result

        up_price   = market.up_token.order_book.mid_price   if (market.up_token   and market.up_token.order_book)   else 0.5
        down_price = market.down_token.order_book.mid_price if (market.down_token and market.down_token.order_book) else 0.5

        up_ob   = market.up_token.order_book   if market.up_token   else None
        down_ob = market.down_token.order_book if market.down_token else None

        spread    = (up_ob.spread    if up_ob   else 0.0) + (down_ob.spread    if down_ob else 0.0)
        bid_depth = (up_ob.bid_depth if up_ob   else 0.0) + (down_ob.bid_depth if down_ob else 0.0)
        ask_depth = (up_ob.ask_depth if up_ob   else 0.0) + (down_ob.ask_depth if down_ob else 0.0)

        pct = market.elapsed_pct
        prob_tbl = self.probability_table(bucket)

        snap = AnalyticsSnapshot(
            symbol       = self.symbol,
            timestamp    = time.time(),
            rv60         = rv60,
            eff60        = eff60,
            vol_bucket   = bucket.vol,
            trend_bucket = bucket.trend,
            spread       = spread,
            bid_depth    = bid_depth,
            ask_depth    = ask_depth,
            up_price     = up_price,
            down_price   = down_price,
            prob_008     = prob_tbl.get(0.08, 0.0),
            prob_012     = prob_tbl.get(0.12, 0.0),
            prob_020     = prob_tbl.get(0.20, 0.0),
            dir_60pct    = self.direction_probability(up_price, bucket, 0.60),
            dir_80pct    = self.direction_probability(up_price, bucket, 0.80),
            dir_90pct    = self.direction_probability(up_price, bucket, 0.90),
        )
        self._last_snapshot = snap
        return snap

    @property
    def last_snapshot(self) -> Optional[AnalyticsSnapshot]:
        return self._last_snapshot

    def bar_count(self) -> int:
        return len(self._bars)

    def candle_count(self) -> int:
        return len(self._candles)


# ─── Registry ─────────────────────────────────────────────────────────────────

class AnalyticsRegistry:
    """One MarketAnalytics instance per symbol, shared across all agents."""

    def __init__(self):
        self._store: Dict[str, MarketAnalytics] = {}

    def get(self, symbol: str) -> MarketAnalytics:
        if symbol not in self._store:
            self._store[symbol] = MarketAnalytics(symbol)
        return self._store[symbol]

    def all_snapshots(self) -> Dict[str, Optional[AnalyticsSnapshot]]:
        return {sym: eng.last_snapshot for sym, eng in self._store.items()}
