"""
Polymarket 15M Crypto Market Collector - Configuration
"""
import os

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
# Minimum trade size (USDC)
MIN_TRADE_SIZE      = 2.0
MAX_TRADE_SIZE      = 50.0
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
