# Polymarket 15M Crypto Market System

Real-time data collection, analytics, and automated trading for Polymarket's
15-minute BTC/ETH/SOL/XRP Up/Down markets.

---

## Architecture — 3-Agent Design

```
┌──────────────────────────────────────────────────────────────┐
│  Agent 1: CollectorAgent  (collector.py)                     │
│  ─────────────────────────────────────────────────────────── │
│  • Discovers current + next 15-minute markets via Gamma API  │
│  • Streams live order books via WebSocket                    │
│  • Fetches 1-minute price history for analytics warming      │
│  • Fetches recent trades from data-api.polymarket.com        │
│  • Renders live terminal dashboard (rich)                    │
└──────────────────┬───────────────────────────────────────────┘
                   │ MarketState (shared async store)
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  Agent 2: DecisionEngine  (decision.py)                      │
│  ─────────────────────────────────────────────────────────── │
│  • Reads AnalyticsSnapshots from Agent 1                     │
│  • Detects arbitrage (UP_ask + DOWN_ask < 0.97)              │
│  • Generates directional signals at 60/80/90% of market      │
│  • Applies HighVol+Trend momentum strategy                   │
│  • Enforces position limits and minimum edge                 │
│  • Pushes TradeSignals → asyncio.Queue                       │
└──────────────────┬───────────────────────────────────────────┘
                   │ asyncio.Queue[TradeSignal]
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  Agent 3: TraderAgent  (trader.py)                           │
│  ─────────────────────────────────────────────────────────── │
│  • Consumes signals from the queue                           │
│  • Signs orders with EIP-712 (eth-account)                   │
│  • Posts signed orders to CLOB API with L2 HMAC auth         │
│  • Tracks fills, positions, and realised P&L                 │
│  • Falls back to paper-trade when credentials absent         │
└──────────────────────────────────────────────────────────────┘
```

### Why 3 Agents?

| Concern         | Agent          | Rationale                                              |
|-----------------|----------------|--------------------------------------------------------|
| Data quality    | Collector      | Isolated — can be tested without touching exchange     |
| Signal quality  | Decision       | Backtestable in isolation from order routing           |
| Order safety    | Trader         | Deduplicates signals, enforces risk limits, logs fills |

---

## Live Dashboard

```
╔══════════════════════════════════════════════════════════════════════╗
║  POLYMARKET 15M CRYPTO MARKETS  2024-02-27 10:30:25 UTC  ● WS Live  ║
╚══════════════════════════════════════════════════════════════════════╝

 Symbol │ Status  │ Market ID │ ConditionID │ Title      │ Timestamp │ Side │ Asset (TokenID) │ Size │ Price │ Spread │ Bid Depth │ Ask Depth │ Outcome
 ───────┼─────────┼───────────┼─────────────┼────────────┼───────────┼──────┼─────────────────┼──────┼───────┼────────┼───────────┼───────────┼────────
 BTC    │ CURRENT │ 1234…     │ 0xabc…      │ BTC >X 10… │ 10:30:25  │ SELL │ 456789…         │ 120  │ 0.48  │ 0.02   │  85.0     │ 120.0     │ DOWN
        │         │ 1234…     │ 0xabc…      │ BTC >X 10… │ 10:30:25  │ BUY  │ 123456…         │  85  │ 0.52  │ 0.02   │  85.0     │ 120.0     │ UP

 Symbol │ Progress                    │ Vol Bucket │ Trend  │ RV60    │ Eff60 │ UP Price │ DOWN Price │ Spread │ P(±0.08%) │ P(±0.12%) │ P(±0.20%) │ Dir@60% │ Dir@80% │ Dir@90%
 ───────┼─────────────────────────────┼────────────┼────────┼─────────┼───────┼──────────┼────────────┼────────┼───────────┼───────────┼───────────┼─────────┼─────────┼────────
 BTC    │ ████████░░░░░░░░░░░░  58.3% │ LowVol     │ Range  │ 0.00124 │ 0.145 │ 0.5200   │ 0.4800     │ 0.0200 │    65.0%  │    74.0%  │    87.0%  │ ↑ UP 54%│ ↑ UP 55%│ ↑ UP 56%
```

---

## Analytics Engine

### 1-Minute Price History

Each token's mid-price is sampled at every REST refresh (5 s) and WebSocket
update. A rolling deque of 120 bars is maintained per symbol.

### Volatility Bucket

```
r_t = ln(P_t / P_{t-1})          # 1-min log return
RV60 = sqrt( Σ r_t² )  for t in last 60 bars

LowVol  if RV60 ≤ median(RV60 history)
HighVol if RV60 > median
```

### Trend Bucket (Efficiency Ratio)

```
Eff60 = |P_now - P_60m_ago| / Σ|P_k - P_{k-1}|

Range if Eff60 ≤ median(Eff60 history)
Trend if Eff60 > median
```

### 4-Bucket Grid

| Bucket           | Typical behaviour                           |
|------------------|---------------------------------------------|
| LowVol + Range   | Tight, mean-reverting → high P(small Rrem)  |
| LowVol + Trend   | Slow directional drift                       |
| HighVol + Range  | Noisy, unpredictable                         |
| HighVol + Trend  | Strong momentum → follow the move            |

### Probability Table P_B(X)

```
P_B(X) = Pr(|R_rem| ≤ X | bucket B)
       = #{|R_rem| ≤ X in bucket B} / #{candles in bucket B}

where R_rem = (close - P_12) / P_12   (remaining return from minute-12)
```

Bootstrap priors are used until ≥ 30 historical candles per bucket:

| Bucket           | P(X=0.08%) | P(X=0.12%) | P(X=0.20%) |
|------------------|:----------:|:----------:|:----------:|
| LowVol + Range   |    65 %    |    74 %    |    87 %    |
| LowVol + Trend   |    52 %    |    63 %    |    80 %    |
| HighVol + Range  |    44 %    |    56 %    |    71 %    |
| HighVol + Trend  |    36 %    |    47 %    |    63 %    |

### Direction Probability at 60 / 80 / 90 %

Base signal is the UP token's market price (already the best probability
estimate). Adjusted by:

- **Timing** — signal strengthens linearly as market approaches close
- **Bucket** — HighVol+Trend amplifies momentum; LowVol+Range dampens it

---

## Trading Strategies

### 1. Arbitrage (zero-risk when both legs fill)
If `UP_ask + DOWN_ask < 0.97`, buy both tokens.
Guaranteed payout of $1.00 on a $<0.97 investment.

### 2. Directional at Timing Thresholds
At 9 min (60%), 12 min (80%), 13.5 min (90%) of elapsed market time:
- If adjusted `P(UP) > 0.50 + MIN_EDGE` → buy UP token
- If adjusted `P(UP) < 0.50 - MIN_EDGE` → buy DOWN token
- Trade size scales with edge strength

### 3. HighVol+Trend Momentum
Within the first 70% of the market, if the bucket is HighVol+Trend and the
price has deviated ≥ 8 cents from 0.50, enter in the direction of the trend.

---

## Installation

```bash
pip install -r polymarket/requirements.txt
```

---

## Usage

```bash
# Data-only mode (live dashboard, no trading)
python -m polymarket --data-only

# Paper-trade mode (signals generated, no real orders sent)
python -m polymarket --paper

# Live trading (requires environment variables)
export POLY_PRIVATE_KEY="0x..."        # Ethereum private key
export POLY_ADDRESS="0x..."            # Polygon wallet address
export POLY_API_KEY="..."              # Polymarket CLOB API key
export POLY_API_SECRET="..."           # CLOB API secret (base64)
export POLY_API_PASSPHRASE="..."       # CLOB API passphrase
python -m polymarket
```

---

## Configuration (`config.py`)

| Variable           | Default | Description                             |
|--------------------|---------|-----------------------------------------|
| `MIN_TRADE_SIZE`   | 2.0     | Minimum trade size (USDC)               |
| `MAX_TRADE_SIZE`   | 50.0    | Maximum single trade size               |
| `MAX_POSITION`     | 100.0   | Max USDC exposure per symbol            |
| `MIN_EDGE`         | 0.05    | Minimum probability edge to trade       |
| `ARB_THRESHOLD`    | 0.97    | UP+DOWN threshold for arbitrage flag    |
| `REST_REFRESH_SEC` | 5       | REST API poll interval (seconds)        |
| `LOOKBACK_BARS`    | 60      | Number of 1-min bars for vol/trend      |

---

## Obtaining Polymarket API Credentials

1. Create a Polymarket account and connect a Polygon wallet
2. Use `createOrDeriveApiKey()` from the Python SDK:
   ```python
   from py_clob_client.client import ClobClient
   client = ClobClient(host="https://clob.polymarket.com",
                       key=PRIVATE_KEY, chain_id=137)
   creds = client.create_or_derive_api_creds()
   print(creds.api_key, creds.api_secret, creds.api_passphrase)
   ```
3. Set the environment variables shown above

> **Risk Warning**: Polymarket markets resolve to 0 or 1. Small trades can
> still result in total loss of the invested amount. The 15-minute markets
> are high-frequency — manage position sizes carefully.
