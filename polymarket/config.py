"""
Polymarket 15M Crypto Market Collector - Configuration
"""
import os
from pathlib import Path

# ─── API Endpoints ────────────────────────────────────────────────────────────
GAMMA_API     = "https://gamma-api.polymarket.com"
CLOB_API      = "https://clob.polymarket.com"
DATA_API      = "https://data-api.polymarket.com"
WS_URL        = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ─── Target Markets ───────────────────────────────────────────────────────────
SLUGS = [
    "btc-updown-15m",
    "eth-updown-15m",
    "sol-updown-15m",
    "xrp-updown-15m",
]

SYMBOL_MAP = {
    "btc-updown-15m": "BTC",
    "eth-updown-15m": "ETH",
    "sol-updown-15m": "SOL",
    "xrp-updown-15m": "XRP",
}

# ─── Market Timing ────────────────────────────────────────────────────────────
MARKET_DURATION_SECONDS = 15 * 60   # 15 minutes in seconds
DECISION_THRESHOLDS = {             # % of market elapsed → predict direction
    "60pct": 0.60,                  # 9 min
    "80pct": 0.80,                  # 12 min
    "90pct": 0.90,                  # 13.5 min
}

# ─── Analytics ────────────────────────────────────────────────────────────────
LOOKBACK_BARS       = 60            # 60 one-minute bars for vol/trend
X_THRESHOLDS        = [0.08, 0.12, 0.20]   # % for probability table
MIN_HISTORY_BARS    = 5             # Minimum bars before analytics are valid
ANALYTICS_INTERVAL  = 60           # Recompute analytics every N seconds

# ─── Trading ──────────────────────────────────────────────────────────────────
# Fixed trade size (USDC) — all trades are exactly $5
TRADE_SIZE          = 5.0
MIN_TRADE_SIZE      = 5.0
MAX_TRADE_SIZE      = 5.0
# Max position per market (USDC)
MAX_POSITION        = 100.0
# Minimum probability edge to trigger a trade (above 0.5 implied probability)
MIN_EDGE            = 0.05
# Fee rate (Polymarket charges up to 2%)
FEE_RATE            = 0.02
# Arbitrage threshold: if UP + DOWN < this, flag opportunity
ARB_THRESHOLD       = 0.97

# ─── Trading Credentials (from env) ───────────────────────────────────────────
POLY_PRIVATE_KEY    = os.getenv("POLY_PRIVATE_KEY", "")
POLY_ADDRESS        = os.getenv("POLY_ADDRESS", "")
POLY_API_KEY        = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET     = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE = os.getenv("POLY_API_PASSPHRASE", "")

# ─── Refresh Rates ────────────────────────────────────────────────────────────
REST_REFRESH_SEC    = 5             # REST API poll interval
WS_RECONNECT_SEC    = 3             # WebSocket reconnect delay

# ─── Display ──────────────────────────────────────────────────────────────────
DISPLAY_REFRESH_SEC = 1.0           # Terminal table refresh rate

# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH            = Path(os.getenv("DB_PATH", Path.home() / "polymarket.db"))
DB_RETENTION_HOURS = int(os.getenv("DB_RETENTION_HOURS", "36"))  # 24-48h window
DB_TRIM_INTERVAL   = 3600    # Trim stale rows once per hour
SNAPSHOT_INTERVAL  = 60      # Write market snapshot to DB every 60 seconds

# ─── Auto-restart ─────────────────────────────────────────────────────────────
# Set AUTO_RESTART_HOURS=24 in environment to auto-restart the process daily.
# 0 disables the feature (default).
AUTO_RESTART_HOURS = float(os.getenv("AUTO_RESTART_HOURS", "0"))

# ─── Suppressed Signal Combinations ───────────────────────────────────────────
# (vol_bucket.value, trend_bucket.value, trigger) tuples that are disabled based
# on empirical performance analysis.  Signals matching any entry are dropped
# before reaching the trader.
#
# ACTIVE per bucket (everything NOT listed here fires normally):
#   HighVol+Trend : directional_90pct, forced_coin, forced_edge, pre_open, trend_follow
#   HighVol+Range : directional_90pct, forced_coin, pre_open
#   LowVol+Trend  : forced_coin, forced_edge, pre_open
#   LowVol+Range  : forced_coin, forced_edge, pre_open
SUPPRESSED_SIGNALS: frozenset = frozenset([
    # ── HighVol+Trend ──────────────────────────────────────────────────────
    ("HighVol", "Trend", "directional_60pct"),
    ("HighVol", "Trend", "directional_80pct"),
    ("HighVol", "Trend", "forced"),          # legacy unsplit trigger
    # ── HighVol+Range ──────────────────────────────────────────────────────
    ("HighVol", "Range", "directional_60pct"),
    ("HighVol", "Range", "directional_80pct"),
    ("HighVol", "Range", "forced"),          # legacy unsplit trigger
    ("HighVol", "Range", "forced_edge"),
    # ── LowVol+Trend ───────────────────────────────────────────────────────
    ("LowVol",  "Trend", "directional_60pct"),
    ("LowVol",  "Trend", "directional_80pct"),
    ("LowVol",  "Trend", "directional_90pct"),
    ("LowVol",  "Trend", "forced"),          # legacy unsplit trigger
    # ── LowVol+Range ───────────────────────────────────────────────────────
    ("LowVol",  "Range", "directional_60pct"),
    ("LowVol",  "Range", "directional_80pct"),
    ("LowVol",  "Range", "directional_90pct"),
    ("LowVol",  "Range", "forced"),          # legacy unsplit trigger
])
