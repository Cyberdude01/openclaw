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

# ─── Strategy Version ─────────────────────────────────────────────────────────
# Set STRATEGY_VERSION env var to select the active trading strategy:
#
#   v1  Production baseline — forced_coin, forced_edge, pre_open, arb,
#         directional_90pct (HighVol only), trend_follow (HighVol+Trend)
#
#   v2  Trend/Directional focus — same as v1 but forced_coin + forced_edge
#         fully suppressed; isolates trend_follow + directional_90pct signal
#
STRATEGY_VERSION = os.getenv("STRATEGY_VERSION", "v1")

# ─── Suppressed Signal Combinations ───────────────────────────────────────────
# (vol_bucket.value, trend_bucket.value, trigger) tuples that are disabled.
# Signals matching any entry are dropped before reaching the trader.

# V1.0 — Production baseline
# Active: forced_coin, forced_edge, pre_open, arb, directional_90pct (HighVol),
#         trend_follow (HighVol+Trend)
_SUPPRESSED_V1: frozenset = frozenset([
    # ── HighVol+Trend ──────────────────────────────────────────────────────
    ("HighVol", "Trend", "forced"),          # legacy unsplit trigger
    # ── HighVol+Range ──────────────────────────────────────────────────────
    ("HighVol", "Range", "forced"),          # legacy unsplit trigger
    ("HighVol", "Range", "forced_edge"),
    # ── LowVol+Trend ───────────────────────────────────────────────────────
    ("LowVol",  "Trend", "directional_90pct"),
    ("LowVol",  "Trend", "forced"),          # legacy unsplit trigger
    # ── LowVol+Range ───────────────────────────────────────────────────────
    ("LowVol",  "Range", "directional_90pct"),
    ("LowVol",  "Range", "forced"),          # legacy unsplit trigger
])

# V2.0 — Trend/Directional focus: same as V1 plus forced_coin + forced_edge
# fully suppressed across all buckets, isolating trend_follow + directional_90pct
_SUPPRESSED_V2: frozenset = _SUPPRESSED_V1 | frozenset([
    ("HighVol", "Trend", "forced_coin"),
    ("HighVol", "Trend", "forced_edge"),
    ("HighVol", "Range", "forced_coin"),
    # forced_edge in HighVol+Range already in V1
    ("LowVol",  "Trend", "forced_coin"),
    ("LowVol",  "Trend", "forced_edge"),
    ("LowVol",  "Range", "forced_coin"),
    ("LowVol",  "Range", "forced_edge"),
])

_STRATEGY_SUPPRESSED = {
    "v1": _SUPPRESSED_V1,
    "v2": _SUPPRESSED_V2,
}

SUPPRESSED_SIGNALS: frozenset = _STRATEGY_SUPPRESSED.get(STRATEGY_VERSION, _SUPPRESSED_V1)
