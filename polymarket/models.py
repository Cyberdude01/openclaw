"""
Polymarket 15M Crypto Market Collector - Data Models
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ─── Enums ────────────────────────────────────────────────────────────────────

class VolBucket(str, Enum):
    LOW  = "LowVol"
    HIGH = "HighVol"

class TrendBucket(str, Enum):
    RANGE = "Range"
    TREND = "Trend"

class Side(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"

class Outcome(str, Enum):
    UP   = "UP"
    DOWN = "DOWN"

class MarketStatus(str, Enum):
    CURRENT = "CURRENT"
    NEXT    = "NEXT"
    EXPIRED = "EXPIRED"


# ─── Order Book ───────────────────────────────────────────────────────────────

@dataclass
class Level:
    price: float
    size: float

@dataclass
class OrderBook:
    token_id:        str
    timestamp:       float
    bids:            list[Level] = field(default_factory=list)   # sorted desc
    asks:            list[Level] = field(default_factory=list)   # sorted asc
    last_trade_price: float = 0.0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid_price(self) -> float:
        if self.bids and self.asks:
            return (self.best_bid + self.best_ask) / 2
        return self.last_trade_price

    @property
    def spread(self) -> float:
        if self.bids and self.asks:
            return self.best_ask - self.best_bid
        return 0.0

    @property
    def bid_depth(self) -> float:
        return sum(lvl.size for lvl in self.bids[:5])

    @property
    def ask_depth(self) -> float:
        return sum(lvl.size for lvl in self.asks[:5])


# ─── Token / Market ───────────────────────────────────────────────────────────

@dataclass
class TokenInfo:
    token_id:   str
    outcome:    Outcome
    price:      float = 0.0
    order_book: Optional[OrderBook] = None


@dataclass
class MarketInfo:
    """Represents one 15-minute prediction market window."""
    market_id:    str
    condition_id: str
    slug:         str
    title:        str
    symbol:       str          # BTC / ETH / SOL / XRP
    start_time:   datetime
    end_time:     datetime
    status:       MarketStatus
    up_token:     Optional[TokenInfo] = None
    down_token:   Optional[TokenInfo] = None
    volume:       float = 0.0
    liquidity:    float = 0.0

    @property
    def is_active(self) -> bool:
        now = datetime.now(timezone.utc)
        return self.start_time <= now <= self.end_time

    @property
    def elapsed_pct(self) -> float:
        """Fraction of the 15-minute window that has elapsed (0.0 – 1.0)."""
        now = datetime.now(timezone.utc)
        if now < self.start_time:
            return 0.0
        if now > self.end_time:
            return 1.0
        total = (self.end_time - self.start_time).total_seconds()
        elapsed = (now - self.start_time).total_seconds()
        return elapsed / total if total > 0 else 0.0

    @property
    def elapsed_seconds(self) -> float:
        now = datetime.now(timezone.utc)
        return max(0.0, (now - self.start_time).total_seconds())

    @property
    def remaining_seconds(self) -> float:
        now = datetime.now(timezone.utc)
        return max(0.0, (self.end_time - now).total_seconds())

    @property
    def arb_opportunity(self) -> Optional[float]:
        """
        Returns the net profit per dollar if an arbitrage exists, else None.
        Arbitrage: UP_ask + DOWN_ask < 1  (buy both → guaranteed $1 payout).
        """
        if self.up_token and self.down_token:
            up_ob   = self.up_token.order_book
            down_ob = self.down_token.order_book
            if up_ob and down_ob and up_ob.best_ask > 0 and down_ob.best_ask > 0:
                total = up_ob.best_ask + down_ob.best_ask
                if total < 0.97:
                    return round(1.0 - total, 4)
        return None


# ─── Market Row (display) ─────────────────────────────────────────────────────

@dataclass
class MarketRow:
    """Flat row for the live data table."""
    market_id:    str
    condition_id: str
    title:        str
    timestamp:    str    # human-readable UTC
    side:         Side
    asset:        str    # token_id
    size:         float  # top-of-book size
    price:        float
    outcome:      Outcome
    spread:       float
    bid_depth:    float
    ask_depth:    float


# ─── Analytics ────────────────────────────────────────────────────────────────

@dataclass
class MinuteBar:
    timestamp: float
    price:     float
    volume:    float = 0.0


@dataclass
class BucketLabel:
    vol:   VolBucket
    trend: TrendBucket

    def __str__(self) -> str:
        return f"{self.vol.value}+{self.trend.value}"

    def __hash__(self) -> int:
        return hash((self.vol, self.trend))

    def __eq__(self, other) -> bool:
        return isinstance(other, BucketLabel) and self.vol == other.vol and self.trend == other.trend


@dataclass
class AnalyticsSnapshot:
    symbol:       str
    timestamp:    float
    rv60:         float          # Realized volatility (60 1-min bars)
    eff60:        float          # Efficiency ratio
    vol_bucket:   VolBucket
    trend_bucket: TrendBucket
    spread:       float
    bid_depth:    float
    ask_depth:    float
    up_price:     float
    down_price:   float
    # Probability that |Rrem| <= X at the 12-minute mark
    prob_008:     float = 0.0    # X = 0.08%
    prob_012:     float = 0.0    # X = 0.12%
    prob_020:     float = 0.0    # X = 0.20%
    # Direction probability at 60/80/90% of market
    dir_60pct:    float = 0.50   # Prob of UP at 60% elapsed
    dir_80pct:    float = 0.50   # Prob of UP at 80% elapsed
    dir_90pct:    float = 0.50   # Prob of UP at 90% elapsed


# ─── Trade Records ────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    id:           str
    market:       str   # condition_id
    asset_id:     str   # token_id
    side:         Side
    size:         float
    price:        float
    outcome:      Outcome
    match_time:   str
    status:       str


# ─── Signal (from Decision Agent) ─────────────────────────────────────────────

@dataclass
class TradeSignal:
    symbol:      str
    market_id:   str
    condition_id: str
    token_id:    str
    outcome:     Outcome
    side:        Side
    size:        float        # USDC size
    price:       float        # limit price
    confidence:  float        # 0.5 – 1.0
    reason:      str
    timestamp:   float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
