"""
Polymarket 15M Crypto Market Data Collector

Agent 1 of 3 — responsible for:
  - Discovering current and next 15-minute markets for BTC/ETH/SOL/XRP
  - Fetching real-time order book data via REST + WebSocket
  - Fetching 1-minute price history for analytics
  - Populating the shared MarketState store
  - Rendering the live terminal dashboard
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import websockets
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .analytics import AnalyticsRegistry
from .config import (
    CLOB_API,
    DATA_API,
    GAMMA_API,
    MARKET_DURATION_SECONDS,
    REST_REFRESH_SEC,
    SLUGS,
    SYMBOL_MAP,
    WS_RECONNECT_SEC,
    WS_URL,
    DISPLAY_REFRESH_SEC,
    ARB_THRESHOLD,
)
from .models import (
    Level,
    MarketInfo,
    MarketRow,
    MarketStatus,
    OrderBook,
    Outcome,
    Side,
    TokenInfo,
    TradeRecord,
)

console = Console()


# ─── Shared State ─────────────────────────────────────────────────────────────

class MarketState:
    """Thread-safe (asyncio) shared data store for all agents."""

    def __init__(self):
        # slug → [current_market, next_market]
        self.markets: Dict[str, List[MarketInfo]] = defaultdict(list)
        # token_id → OrderBook
        self.order_books: Dict[str, OrderBook] = {}
        # slug → last N trades (from data-api)
        self.recent_trades: Dict[str, List[TradeRecord]] = defaultdict(list)
        # Analytics registry
        self.analytics = AnalyticsRegistry()
        # Flag: WebSocket connected
        self.ws_connected = False
        # Last refresh
        self.last_refresh = 0.0
        self._lock = asyncio.Lock()

    async def update_market(self, slug: str, market: MarketInfo):
        async with self._lock:
            existing = [m for m in self.markets[slug] if m.market_id != market.market_id]
            existing.append(market)
            # Keep only current + next
            existing.sort(key=lambda m: m.start_time)
            self.markets[slug] = existing[-2:]

    async def update_order_book(self, token_id: str, ob: OrderBook):
        async with self._lock:
            self.order_books[token_id] = ob
            # Push mid-price into analytics
            for slug, markets in self.markets.items():
                for mkt in markets:
                    symbol = SYMBOL_MAP.get(slug, slug)
                    engine = self.analytics.get(symbol)
                    if mkt.up_token and mkt.up_token.token_id == token_id:
                        mkt.up_token.order_book = ob
                        mkt.up_token.price = ob.mid_price
                        engine.add_price(ob.timestamp, ob.mid_price)
                    elif mkt.down_token and mkt.down_token.token_id == token_id:
                        mkt.down_token.order_book = ob
                        mkt.down_token.price = ob.mid_price

    def get_current_market(self, slug: str) -> Optional[MarketInfo]:
        markets = self.markets.get(slug, [])
        now = datetime.now(timezone.utc)
        for m in markets:
            if m.start_time <= now <= m.end_time:
                return m
        # Fallback: the most recent one
        return markets[-1] if markets else None

    def get_next_market(self, slug: str) -> Optional[MarketInfo]:
        markets = self.markets.get(slug, [])
        now = datetime.now(timezone.utc)
        upcoming = [m for m in markets if m.start_time > now]
        return upcoming[0] if upcoming else None


# ─── API Client ───────────────────────────────────────────────────────────────

class PolymarketClient:
    """Async HTTP client for Polymarket REST APIs."""

    def __init__(self, session: aiohttp.ClientSession):
        self._s = session

    async def _get(self, url: str, params: dict = None) -> Any:
        try:
            async with self._s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                r.raise_for_status()
                return await r.json()
        except Exception as exc:
            console.log(f"[red]HTTP error {url}: {exc}[/red]")
            return None

    async def fetch_event_by_slug(self, slug: str) -> Optional[dict]:
        """Fetch the parent event for a 15M slug."""
        data = await self._get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return None

    async def fetch_markets_for_event(self, event_id: str) -> List[dict]:
        """Fetch all markets belonging to an event."""
        data = await self._get(
            f"{GAMMA_API}/markets",
            params={"event_id": event_id, "limit": 20},
        )
        return data if isinstance(data, list) else []

    async def fetch_markets_by_slug(self, slug: str) -> List[dict]:
        """Fallback: search for markets directly by slug when event lookup fails."""
        data = await self._get(
            f"{GAMMA_API}/markets",
            params={"slug": slug, "limit": 5},
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "markets" in data:
            return data["markets"]
        return []

    async def fetch_order_book(self, token_id: str) -> Optional[dict]:
        return await self._get(f"{CLOB_API}/book", params={"token_id": token_id})

    async def fetch_price_history(self, token_id: str, interval: str = "1m", fidelity: int = 1) -> List[dict]:
        data = await self._get(
            f"{CLOB_API}/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        if isinstance(data, dict) and "history" in data:
            return data["history"]
        return []

    async def fetch_last_trade_price(self, token_id: str) -> Optional[float]:
        data = await self._get(f"{CLOB_API}/last-trade-price", params={"token_id": token_id})
        if isinstance(data, dict) and "price" in data:
            try:
                return float(data["price"])
            except (ValueError, TypeError):
                pass
        return None

    async def fetch_trades(self, condition_id: str, limit: int = 50) -> List[dict]:
        data = await self._get(f"{DATA_API}/trades", params={"market": condition_id, "limit": limit})
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []


# ─── Market Discovery ─────────────────────────────────────────────────────────

def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _next_15m_boundary(after: datetime) -> datetime:
    """Return the next UTC timestamp that is a multiple of 15 minutes."""
    minutes = after.minute
    remainder = minutes % 15
    add_mins = 15 - remainder if remainder != 0 else 15
    boundary = after.replace(second=0, microsecond=0) + timedelta(minutes=add_mins)
    return boundary


def _parse_market(raw: dict, slug: str, symbol: str) -> Optional[MarketInfo]:
    """Convert a Gamma API market dict into a MarketInfo."""
    market_id    = str(raw.get("id", ""))
    condition_id = str(raw.get("conditionId", raw.get("condition_id", "")))
    title        = raw.get("question", raw.get("title", "—"))
    start_str    = raw.get("startDate", raw.get("start_date", ""))
    end_str      = raw.get("endDate",   raw.get("end_date",   ""))
    active       = raw.get("active", True)
    closed       = raw.get("closed", False)

    start_time = _parse_dt(start_str)
    end_time   = _parse_dt(end_str)
    if not start_time or not end_time:
        return None

    now = datetime.now(timezone.utc)
    if start_time <= now <= end_time:
        status = MarketStatus.CURRENT
    elif start_time > now:
        status = MarketStatus.NEXT
    else:
        status = MarketStatus.EXPIRED

    # Parse tokens — clobTokenIds is a JSON-encoded list or comma-separated
    tokens_raw = raw.get("clobTokenIds", raw.get("clob_token_ids", "[]"))
    if isinstance(tokens_raw, str):
        try:
            token_ids: List[str] = json.loads(tokens_raw)
        except json.JSONDecodeError:
            token_ids = [t.strip() for t in tokens_raw.split(",") if t.strip()]
    elif isinstance(tokens_raw, list):
        token_ids = [str(t) for t in tokens_raw]
    else:
        token_ids = []

    outcomes_raw = raw.get("outcomes", '["UP","DOWN"]')
    if isinstance(outcomes_raw, str):
        try:
            outcomes: List[str] = json.loads(outcomes_raw)
        except json.JSONDecodeError:
            outcomes = [o.strip() for o in outcomes_raw.split(",")]
    elif isinstance(outcomes_raw, list):
        outcomes = [str(o) for o in outcomes_raw]
    else:
        outcomes = ["UP", "DOWN"]

    out_prices_raw = raw.get("outcomePrices", "[0.5,0.5]")
    if isinstance(out_prices_raw, str):
        try:
            out_prices: List[float] = [float(p) for p in json.loads(out_prices_raw)]
        except (json.JSONDecodeError, ValueError):
            out_prices = [0.5, 0.5]
    elif isinstance(out_prices_raw, list):
        try:
            out_prices = [float(p) for p in out_prices_raw]
        except (ValueError, TypeError):
            out_prices = [0.5, 0.5]
    else:
        out_prices = [0.5, 0.5]

    up_token   = None
    down_token = None
    for i, (tid, oc) in enumerate(zip(token_ids, outcomes)):
        price = out_prices[i] if i < len(out_prices) else 0.5
        oc_upper = oc.upper()
        if "UP" in oc_upper or "YES" in oc_upper:
            up_token   = TokenInfo(token_id=tid, outcome=Outcome.UP,   price=price)
        elif "DOWN" in oc_upper or "NO" in oc_upper:
            down_token = TokenInfo(token_id=tid, outcome=Outcome.DOWN, price=price)

    try:
        volume    = float(raw.get("volume", 0) or 0)
        liquidity = float(raw.get("liquidity", 0) or 0)
    except (TypeError, ValueError):
        volume = liquidity = 0.0

    return MarketInfo(
        market_id    = market_id,
        condition_id = condition_id,
        slug         = slug,
        title        = title,
        symbol       = symbol,
        start_time   = start_time,
        end_time     = end_time,
        status       = status,
        up_token     = up_token,
        down_token   = down_token,
        volume       = volume,
        liquidity    = liquidity,
    )


# ─── Collector Agent ──────────────────────────────────────────────────────────

class CollectorAgent:
    """
    Fetches and maintains live market data. Outputs to MarketState.
    Runs three concurrent loops:
      1. REST poller  — market discovery + order books every 5 s
      2. WebSocket    — real-time order book updates
      3. History load — 1-minute price history on startup
    """

    def __init__(self, state: MarketState):
        self.state   = state
        self._client: Optional[PolymarketClient] = None

    # ── Market Discovery ──────────────────────────────────────────────────────

    async def _discover_markets(self, slug: str):
        symbol = SYMBOL_MAP.get(slug, slug.upper())
        markets_raw: List[dict] = []

        # ── Step 1: event-based lookup ────────────────────────────────────────
        event = await self._client.fetch_event_by_slug(slug)
        if event:
            event_id    = str(event.get("id", ""))
            markets_raw = await self._client.fetch_markets_for_event(event_id)
            console.log(f"[dim]{slug}: event found ({event_id}), {len(markets_raw)} markets[/dim]")
        else:
            console.log(f"[yellow]{slug}: no event found — trying direct market search[/yellow]")

        # ── Step 2: direct slug fallback if event lookup returned nothing ─────
        if not markets_raw:
            markets_raw = await self._client.fetch_markets_by_slug(slug)
            console.log(f"[dim]{slug}: direct slug search returned {len(markets_raw)} markets[/dim]")

        if not markets_raw:
            console.log(f"[red]{slug}: no markets found via any method[/red]")
            return

        parsed = []
        for raw in markets_raw:
            mkt = _parse_market(raw, slug, symbol)
            if mkt and mkt.status != MarketStatus.EXPIRED:
                parsed.append(mkt)

        console.log(f"[dim]{slug}: {len(parsed)} active/upcoming markets after filtering[/dim]")

        # Sort by start time; keep current + next
        parsed.sort(key=lambda m: m.start_time)
        for mkt in parsed[-2:]:
            await self.state.update_market(slug, mkt)

    # ── Order Book REST Refresh ────────────────────────────────────────────────

    async def _refresh_order_books(self):
        """Fetch order books for all known tokens via REST."""
        token_ids = set()
        for markets in self.state.markets.values():
            for mkt in markets:
                if mkt.up_token:
                    token_ids.add(mkt.up_token.token_id)
                if mkt.down_token:
                    token_ids.add(mkt.down_token.token_id)

        tasks = [self._fetch_and_store_ob(tid) for tid in token_ids]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_and_store_ob(self, token_id: str):
        raw = await self._client.fetch_order_book(token_id)
        if not raw:
            return
        ob = OrderBook(
            token_id         = token_id,
            timestamp        = float(raw.get("timestamp", time.time())),
            bids             = [Level(float(b["price"]), float(b["size"])) for b in raw.get("bids", [])],
            asks             = [Level(float(a["price"]), float(a["size"])) for a in raw.get("asks", [])],
            last_trade_price = float(raw.get("last_trade_price", 0) or 0),
        )
        await self.state.update_order_book(token_id, ob)

    # ── Historical Price Load ─────────────────────────────────────────────────

    async def _load_price_history(self):
        """On startup, load last 1h of 1-minute prices for each token."""
        for slug, markets in self.state.markets.items():
            symbol = SYMBOL_MAP.get(slug, slug)
            engine = self.state.analytics.get(symbol)
            for mkt in markets:
                if mkt.up_token:
                    history = await self._client.fetch_price_history(mkt.up_token.token_id)
                    for bar in history:
                        try:
                            ts = float(bar["t"])
                            p  = float(bar["p"])
                            engine.add_price(ts, p)
                        except (KeyError, ValueError, TypeError):
                            pass

    # ── Recent Trades ─────────────────────────────────────────────────────────

    async def _refresh_trades(self):
        for slug, markets in self.state.markets.items():
            for mkt in markets:
                if not mkt.condition_id:
                    continue
                raw_trades = await self._client.fetch_trades(mkt.condition_id, limit=50)
                records = []
                for t in raw_trades:
                    try:
                        oc_str = str(t.get("outcome", "UP")).upper()
                        oc     = Outcome.UP if "UP" in oc_str else Outcome.DOWN
                        side   = Side.BUY   if str(t.get("side", "BUY")).upper() == "BUY" else Side.SELL
                        records.append(TradeRecord(
                            id         = str(t.get("id", "")),
                            market     = str(t.get("market", "")),
                            asset_id   = str(t.get("asset_id", "")),
                            side       = side,
                            size       = float(t.get("size", 0) or 0),
                            price      = float(t.get("price", 0) or 0),
                            outcome    = oc,
                            match_time = str(t.get("match_time", "")),
                            status     = str(t.get("status", "")),
                        ))
                    except Exception:
                        pass
                self.state.recent_trades[slug] = records[:50]

    # ── WebSocket Handler ─────────────────────────────────────────────────────

    async def _ws_loop(self):
        while True:
            token_ids = []
            for markets in self.state.markets.values():
                for mkt in markets:
                    if mkt.up_token:
                        token_ids.append(mkt.up_token.token_id)
                    if mkt.down_token:
                        token_ids.append(mkt.down_token.token_id)
            if not token_ids:
                await asyncio.sleep(2)
                continue
            try:
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    sub = json.dumps({"assets_ids": token_ids, "type": "market", "custom_feature_enabled": True})
                    await ws.send(sub)
                    self.state.ws_connected = True
                    console.log(f"[green]WebSocket connected: {len(token_ids)} tokens[/green]")
                    async for raw_msg in ws:
                        try:
                            msgs = json.loads(raw_msg)
                            if isinstance(msgs, dict):
                                msgs = [msgs]
                            for msg in msgs:
                                await self._handle_ws_message(msg)
                        except (json.JSONDecodeError, Exception):
                            pass
            except Exception as exc:
                self.state.ws_connected = False
                console.log(f"[yellow]WebSocket error: {exc}. Reconnecting in {WS_RECONNECT_SEC}s…[/yellow]")
                await asyncio.sleep(WS_RECONNECT_SEC)

    async def _handle_ws_message(self, msg: dict):
        event_type = msg.get("event_type", "")
        asset_id   = msg.get("asset_id", "")
        if not asset_id:
            return

        if event_type in ("book", "price_change"):
            ob = self.state.order_books.get(asset_id, OrderBook(token_id=asset_id, timestamp=time.time()))
            if "bids" in msg:
                ob.bids = [Level(float(b["price"]), float(b["size"])) for b in msg["bids"] if float(b.get("size", 0)) > 0]
            if "asks" in msg:
                ob.asks = [Level(float(a["price"]), float(a["size"])) for a in msg["asks"] if float(a.get("size", 0)) > 0]
            ob.bids.sort(key=lambda l: l.price, reverse=True)
            ob.asks.sort(key=lambda l: l.price)
            ob.timestamp = time.time()
            await self.state.update_order_book(asset_id, ob)

        elif event_type == "last_trade_price":
            ob = self.state.order_books.get(asset_id)
            if ob:
                try:
                    ob.last_trade_price = float(msg.get("price", ob.last_trade_price))
                except (TypeError, ValueError):
                    pass

        elif event_type == "best_bid_ask":
            ob = self.state.order_books.get(asset_id, OrderBook(token_id=asset_id, timestamp=time.time()))
            try:
                bid = float(msg.get("bid", ob.best_bid))
                ask = float(msg.get("ask", ob.best_ask))
                if not ob.bids or ob.best_bid != bid:
                    ob.bids = [Level(bid, 0.0)]
                if not ob.asks or ob.best_ask != ask:
                    ob.asks = [Level(ask, 0.0)]
                ob.timestamp = time.time()
                await self.state.update_order_book(asset_id, ob)
            except (TypeError, ValueError):
                pass

    # ── Main REST Loop ─────────────────────────────────────────────────────────

    async def _rest_loop(self):
        while True:
            try:
                for slug in SLUGS:
                    await self._discover_markets(slug)
                await self._refresh_order_books()
                await self._refresh_trades()
                self.state.last_refresh = time.time()
            except Exception as exc:
                console.log(f"[red]REST loop error: {exc}[/red]")
            await asyncio.sleep(REST_REFRESH_SEC)

    # ── Entry Point ───────────────────────────────────────────────────────────

    async def run(self):
        async with aiohttp.ClientSession() as session:
            self._client = PolymarketClient(session)
            # Initial market discovery
            for slug in SLUGS:
                await self._discover_markets(slug)
            # Load historical price data
            await self._load_price_history()
            # Launch loops concurrently
            await asyncio.gather(
                self._rest_loop(),
                self._ws_loop(),
            )


# ─── Display ──────────────────────────────────────────────────────────────────

OUTCOME_COLOUR = {Outcome.UP: "green", Outcome.DOWN: "red"}
SIDE_COLOUR    = {Side.BUY: "green",   Side.SELL: "red"}
BUCKET_COLOUR  = {
    "LowVol+Range":  "cyan",
    "LowVol+Trend":  "blue",
    "HighVol+Range": "yellow",
    "HighVol+Trend": "red",
}


def _pct_bar(pct: float, width: int = 20) -> str:
    filled = int(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    colour = "green" if pct < 0.6 else ("yellow" if pct < 0.9 else "red")
    return f"[{colour}]{bar}[/{colour}] {pct*100:5.1f}%"


def _dir_str(prob_up: float) -> Text:
    if prob_up >= 0.55:
        t = Text(f"↑ UP  {prob_up*100:.1f}%", style="bold green")
    elif prob_up <= 0.45:
        t = Text(f"↓ DOWN {(1-prob_up)*100:.1f}%", style="bold red")
    else:
        t = Text(f"≈ NEUTRAL {prob_up*100:.1f}%", style="yellow")
    return t


def build_market_table(state: MarketState) -> Table:
    """Build the main data table: one UP+DOWN pair per market."""
    tbl = Table(
        title=None,
        show_header=True,
        header_style="bold white on dark_blue",
        show_lines=True,
        expand=True,
    )
    tbl.add_column("Symbol",      style="bold",    min_width=6)
    tbl.add_column("Status",      style="dim",     min_width=8)
    tbl.add_column("Market ID",   style="dim",     min_width=8,  overflow="fold")
    tbl.add_column("Condition ID",style="dim",     min_width=10, overflow="fold")
    tbl.add_column("Title",                        min_width=20, overflow="fold")
    tbl.add_column("Timestamp",   style="dim",     min_width=19)
    tbl.add_column("Side",                         min_width=5)
    tbl.add_column("Asset (TokenID)",              min_width=12, overflow="fold")
    tbl.add_column("Size",        justify="right", min_width=8)
    tbl.add_column("Price",       justify="right", min_width=6)
    tbl.add_column("Spread",      justify="right", min_width=7)
    tbl.add_column("Bid Depth",   justify="right", min_width=9)
    tbl.add_column("Ask Depth",   justify="right", min_width=9)
    tbl.add_column("Outcome",                      min_width=6)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    for slug in SLUGS:
        symbol = SYMBOL_MAP.get(slug, slug)
        markets = state.markets.get(slug, [])
        if not markets:
            tbl.add_row(
                symbol, "—", "—", "—", "Awaiting data…", now_str,
                "—", "—", "—", "—", "—", "—", "—", "—",
            )
            continue

        for mkt in markets:
            status_lbl = (
                "[bold green]CURRENT[/bold green]" if mkt.status == MarketStatus.CURRENT
                else "[dim]NEXT[/dim]"
            )
            elapsed_str = f"{mkt.elapsed_seconds/60:.1f}m elapsed"
            market_id_short = mkt.market_id[:8] + "…" if len(mkt.market_id) > 10 else mkt.market_id
            cond_short      = mkt.condition_id[:10] + "…" if len(mkt.condition_id) > 12 else mkt.condition_id

            # ── DOWN (SELL) row ──────────────────────────────────────────────
            if mkt.down_token:
                ob   = mkt.down_token.order_book
                tok  = mkt.down_token
                size  = f"{ob.ask_depth:.1f}"  if ob else "—"
                price = f"{ob.best_ask:.4f}"   if ob else f"{tok.price:.4f}"
                sprd  = f"{ob.spread:.4f}"     if ob else "—"
                bid_d = f"{ob.bid_depth:.1f}"  if ob else "—"
                ask_d = f"{ob.ask_depth:.1f}"  if ob else "—"
                tid   = tok.token_id[:12] + "…" if len(tok.token_id) > 14 else tok.token_id
                tbl.add_row(
                    f"[bold]{symbol}[/bold]", status_lbl,
                    market_id_short, cond_short, mkt.title, now_str,
                    "[red]SELL[/red]", tid,
                    size, f"[red]{price}[/red]", sprd, bid_d, ask_d,
                    "[red]DOWN[/red]",
                )

            # ── UP (BUY) row ─────────────────────────────────────────────────
            if mkt.up_token:
                ob   = mkt.up_token.order_book
                tok  = mkt.up_token
                size  = f"{ob.bid_depth:.1f}"  if ob else "—"
                price = f"{ob.best_bid:.4f}"   if ob else f"{tok.price:.4f}"
                sprd  = f"{ob.spread:.4f}"     if ob else "—"
                bid_d = f"{ob.bid_depth:.1f}"  if ob else "—"
                ask_d = f"{ob.ask_depth:.1f}"  if ob else "—"
                tid   = tok.token_id[:12] + "…" if len(tok.token_id) > 14 else tok.token_id
                tbl.add_row(
                    "", "",
                    market_id_short, cond_short, mkt.title, now_str,
                    "[green]BUY[/green]", tid,
                    size, f"[green]{price}[/green]", sprd, bid_d, ask_d,
                    "[green]UP[/green]",
                )
    return tbl


def build_analytics_table(state: MarketState) -> Table:
    """Build the analytics + probability table."""
    tbl = Table(
        title=None,
        show_header=True,
        header_style="bold white on dark_magenta",
        show_lines=True,
        expand=True,
    )
    tbl.add_column("Symbol",       style="bold", min_width=6)
    tbl.add_column("Progress",                   min_width=28)
    tbl.add_column("Vol Bucket",                 min_width=12)
    tbl.add_column("Trend Bucket",               min_width=12)
    tbl.add_column("RV60",         justify="right", min_width=8)
    tbl.add_column("Eff60",        justify="right", min_width=8)
    tbl.add_column("UP Price",     justify="right", min_width=9)
    tbl.add_column("DOWN Price",   justify="right", min_width=10)
    tbl.add_column("Spread",       justify="right", min_width=7)
    tbl.add_column("P(±0.08%)",    justify="right", min_width=10)
    tbl.add_column("P(±0.12%)",    justify="right", min_width=10)
    tbl.add_column("P(±0.20%)",    justify="right", min_width=10)
    tbl.add_column("Dir@60%",      min_width=14)
    tbl.add_column("Dir@80%",      min_width=14)
    tbl.add_column("Dir@90%",      min_width=14)
    tbl.add_column("Arb Oppty",    min_width=10)

    for slug in SLUGS:
        symbol = SYMBOL_MAP.get(slug, slug)
        engine = state.analytics.get(symbol)
        mkt    = state.get_current_market(slug)

        if not mkt:
            tbl.add_row(symbol, "No market", *["—"] * 14)
            continue

        # Compute analytics
        snap = engine.compute_snapshot(mkt) if mkt else None

        progress  = _pct_bar(mkt.elapsed_pct)
        arb       = mkt.arb_opportunity
        arb_str   = f"[bold yellow]+{arb:.4f}[/bold yellow]" if arb else "[dim]None[/dim]"

        if snap:
            bk_str   = f"{snap.vol_bucket.value}"
            bk_color = BUCKET_COLOUR.get(f"{snap.vol_bucket.value}+{snap.trend_bucket.value}", "white")
            tbl.add_row(
                f"[bold]{symbol}[/bold]",
                progress,
                f"[{bk_color}]{snap.vol_bucket.value}[/{bk_color}]",
                f"[{bk_color}]{snap.trend_bucket.value}[/{bk_color}]",
                f"{snap.rv60:.5f}",
                f"{snap.eff60:.3f}",
                f"[green]{snap.up_price:.4f}[/green]",
                f"[red]{snap.down_price:.4f}[/red]",
                f"{snap.spread:.4f}",
                f"{snap.prob_008*100:.1f}%",
                f"{snap.prob_012*100:.1f}%",
                f"{snap.prob_020*100:.1f}%",
                _dir_str(snap.dir_60pct),
                _dir_str(snap.dir_80pct),
                _dir_str(snap.dir_90pct),
                arb_str,
            )
        else:
            bars = engine.bar_count()
            tbl.add_row(
                f"[bold]{symbol}[/bold]",
                progress,
                f"[dim]Warming up ({bars} bars)…[/dim]",
                "—", "—", "—", "—", "—", "—", "—", "—", "—", "—", "—", "—", arb_str,
            )
    return tbl


def build_probability_summary(state: MarketState) -> Panel:
    """Build a compact probability legend panel."""
    lines = ["[bold]Historical Probability Table[/bold] — P(|R_rem| ≤ X) at minute 12 by bucket\n"]
    for slug in SLUGS:
        symbol = SYMBOL_MAP.get(slug, slug)
        engine = state.analytics.get(symbol)
        snap   = engine.last_snapshot
        if snap:
            bk_str = f"{snap.vol_bucket.value}+{snap.trend_bucket.value}"
            color  = BUCKET_COLOUR.get(bk_str, "white")
            lines.append(
                f"  [{color}]{symbol:3s} [{bk_str}][/{color}]  "
                f"X=0.08%: {snap.prob_008*100:.1f}%  "
                f"X=0.12%: {snap.prob_012*100:.1f}%  "
                f"X=0.20%: {snap.prob_020*100:.1f}%  "
                f"| {engine.candle_count()} candles"
            )
        else:
            lines.append(f"  [dim]{symbol}: insufficient data ({engine.bar_count()} bars)[/dim]")
    return Panel("\n".join(lines), title="Probability Model", border_style="magenta")


def build_dashboard(state: MarketState) -> Layout:
    """Compose the full terminal dashboard."""
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ws_ind = "[green]●[/green] WS Live" if state.ws_connected else "[red]○[/red] WS Offline"
    refresh_ago = int(time.time() - state.last_refresh) if state.last_refresh else "—"

    header = Panel(
        f"[bold cyan]POLYMARKET 15M CRYPTO MARKETS[/bold cyan]  "
        f"[dim]{now}[/dim]  {ws_ind}  [dim]Last REST refresh: {refresh_ago}s ago[/dim]",
        border_style="cyan",
    )

    layout = Layout()
    layout.split_column(
        Layout(header,                          name="header",  size=3),
        Layout(build_market_table(state),       name="market",  ratio=3),
        Layout(build_analytics_table(state),    name="analytics", ratio=2),
        Layout(build_probability_summary(state),name="probab",  size=8),
    )
    return layout


# ─── Display Runner ───────────────────────────────────────────────────────────

async def run_display(state: MarketState):
    """Render the live dashboard, refreshing every DISPLAY_REFRESH_SEC."""
    with Live(console=console, refresh_per_second=1 / DISPLAY_REFRESH_SEC, screen=True) as live:
        while True:
            try:
                live.update(build_dashboard(state))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.sleep(DISPLAY_REFRESH_SEC)
            except asyncio.CancelledError:
                break  # clean exit on shutdown
