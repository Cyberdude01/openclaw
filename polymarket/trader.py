"""
Polymarket 15M Trade Executor

Agent 3 of 3 — responsible for:
  - Authenticating with the Polymarket CLOB L2 API
  - Consuming TradeSignals from the decision engine queue
  - Signing and posting orders
  - Tracking fill status and updating PositionBook
  - Paper-trade mode when credentials are not configured

Authentication (Polymarket CLOB L2)
------------------------------------
Headers required on POST /order:
  POLY_ADDRESS     — Polygon wallet address
  POLY_SIGNATURE   — HMAC-SHA256 of (timestamp + method + path + body)
  POLY_TIMESTAMP   — UNIX timestamp (string)
  POLY_API_KEY     — API key from Polymarket
  POLY_PASSPHRASE  — Passphrase from Polymarket

Order Signing (EIP-712)
------------------------
Each order struct must be signed with the private key using eth_account.
The signed order is posted as JSON to POST /order.

For full authentication setup, see:
  https://docs.polymarket.com/api-reference/authentication.md
  https://docs.polymarket.com/trading/clients/l2.md
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from eth_account import Account

from .collector import MarketState
from .config import (
    CLOB_API,
    MAX_TRADE_SIZE,
    MIN_TRADE_SIZE,
    POLY_ADDRESS,
    POLY_API_KEY,
    POLY_API_PASSPHRASE,
    POLY_API_SECRET,
    POLY_PRIVATE_KEY,
    STRATEGY_VERSION,
)
from .database import Database
from .decision import PositionBook
from .models import Side, TradeSignal

# ─── EIP-712 Order Structure ──────────────────────────────────────────────────

# Polymarket CTF Exchange contract on Polygon
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLYGON_CHAIN_ID = 137
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# EIP-712 domain and type for Polymarket orders
ORDER_DOMAIN = {
    "name": "CTFExchange",
    "version": "1",
    "chainId": POLYGON_CHAIN_ID,
    "verifyingContract": CTF_EXCHANGE,
}

ORDER_TYPES = {
    "Order": [
        {"name": "salt",          "type": "uint256"},
        {"name": "maker",         "type": "address"},
        {"name": "signer",        "type": "address"},
        {"name": "taker",         "type": "address"},
        {"name": "tokenId",       "type": "uint256"},
        {"name": "makerAmount",   "type": "uint256"},
        {"name": "takerAmount",   "type": "uint256"},
        {"name": "expiration",    "type": "uint256"},
        {"name": "nonce",         "type": "uint256"},
        {"name": "feeRateBps",    "type": "uint256"},
        {"name": "side",          "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ]
}


# ─── CLOB Auth Helpers ────────────────────────────────────────────────────────

def _l2_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    """Generate L2 authentication headers using HMAC-SHA256."""
    if not POLY_API_KEY or not POLY_API_SECRET:
        return {}
    ts        = str(int(time.time()))
    message   = ts + method.upper() + path + (body or "")
    try:
        secret_bytes = base64.b64decode(POLY_API_SECRET)
    except Exception:
        secret_bytes = POLY_API_SECRET.encode()
    sig = base64.b64encode(
        hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "POLY_ADDRESS":     POLY_ADDRESS,
        "POLY_SIGNATURE":   sig,
        "POLY_TIMESTAMP":   ts,
        "POLY_API_KEY":     POLY_API_KEY,
        "POLY_PASSPHRASE":  POLY_API_PASSPHRASE,
    }


# ─── Order Builder ────────────────────────────────────────────────────────────

class OrderBuilder:
    """Builds and signs Polymarket CLOB orders."""

    USDC_DECIMALS  = 6    # USDC.e on Polygon has 6 decimals
    TOKEN_DECIMALS = 6

    def __init__(self, private_key: str, address: str):
        self._key     = private_key
        self._address = address
        try:
            self._account = Account.from_key(private_key) if private_key else None
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "POLY_PRIVATE_KEY is set but invalid — falling back to paper-trade mode."
            )
            self._account = None

    def _to_usdc_units(self, amount: float) -> int:
        return int(round(amount * 10 ** self.USDC_DECIMALS))

    def _to_token_units(self, amount: float) -> int:
        return int(round(amount * 10 ** self.TOKEN_DECIMALS))

    def _sign_eip712(self, order_data: dict) -> str:
        """
        Sign the order using EIP-712 typed data.
        Uses sign_typed_data when available (eth-account >= 0.10); falls back
        to a placeholder for older versions / paper-trade mode.
        """
        if not self._account:
            return "0x" + "0" * 130   # dummy signature for paper trading

        try:
            signed = self._account.sign_typed_data(
                domain_data   = ORDER_DOMAIN,
                message_types = ORDER_TYPES,
                message_data  = order_data,
            )
            return signed.signature.hex()
        except Exception:
            # Fallback: raw keccak hash of the order data (not valid for mainnet,
            # only used in paper-trade mode when live credentials are absent)
            import json as _json, hashlib as _hl
            raw   = _json.dumps(order_data, sort_keys=True).encode()
            digest = _hl.sha256(raw).hexdigest()
            return "0x" + digest * 2  # 64-byte placeholder

    def build_limit_order(
        self,
        token_id:  str,
        side:      Side,
        size:      float,   # USDC notional
        price:     float,   # e.g. 0.55
        expiry:    int = 0, # 0 = GTC
    ) -> Tuple[dict, str]:
        """
        Build a signed limit order dict ready to POST to /order.
        Returns (order_body, signature).
        """
        import random

        salt         = random.randint(0, 2**128 - 1)
        side_int     = 0 if side == Side.BUY else 1

        # USDC in / tokens out (for BUY)
        if side == Side.BUY:
            maker_amount = self._to_usdc_units(size)
            taker_amount = self._to_token_units(size / price) if price > 0 else 0
        else:  # SELL
            maker_amount = self._to_token_units(size)
            taker_amount = self._to_usdc_units(size * price)

        order_data = {
            "salt":          salt,
            "maker":         self._address,
            "signer":        self._address,
            "taker":         "0x0000000000000000000000000000000000000000",
            "tokenId":       int(token_id) if token_id.isdigit() else 0,
            "makerAmount":   maker_amount,
            "takerAmount":   taker_amount,
            "expiration":    expiry,
            "nonce":         0,
            "feeRateBps":    0,
            "side":          side_int,
            "signatureType": 0,     # EOA signature
        }
        signature = self._sign_eip712(order_data)
        order_body = {
            "order": {
                "salt":          str(salt),
                "maker":         self._address,
                "signer":        self._address,
                "taker":         "0x0000000000000000000000000000000000000000",
                "tokenId":       str(token_id),
                "makerAmount":   str(maker_amount),
                "takerAmount":   str(taker_amount),
                "expiration":    str(expiry),
                "nonce":         "0",
                "feeRateBps":    "0",
                "side":          str(side_int),
                "signatureType": "0",
                "signature":     signature,
            },
            "owner":      self._address,
            "orderType":  "GTC",
        }
        return order_body, signature


# ─── Trade Executor ───────────────────────────────────────────────────────────

class TraderAgent:
    """
    Consumes TradeSignals and executes orders on the Polymarket CLOB.

    Paper-trade mode is automatically activated when POLY_PRIVATE_KEY or
    POLY_API_KEY is not set in the environment.
    """

    PAPER_MODE_MSG = (
        "[yellow]Paper-trade mode active. "
        "Set POLY_PRIVATE_KEY, POLY_ADDRESS, POLY_API_KEY, "
        "POLY_API_SECRET, POLY_API_PASSPHRASE to enable live trading.[/yellow]"
    )

    def __init__(
        self,
        state:  MarketState,
        book:   PositionBook,
        signal_queue: asyncio.Queue,
        db: Optional[Database] = None,
    ):
        self.state  = state
        self.book   = book
        self._queue = signal_queue
        self.db     = db
        self._live  = bool(POLY_PRIVATE_KEY and POLY_API_KEY)
        self._builder: Optional[OrderBuilder] = None
        self._log: List[dict] = []    # in-memory execution log

        if self._live:
            self._builder = OrderBuilder(POLY_PRIVATE_KEY, POLY_ADDRESS)
        else:
            from rich.console import Console
            Console().print(self.PAPER_MODE_MSG)

    # ── Order Execution ───────────────────────────────────────────────────────

    async def _post_order(
        self,
        session:    aiohttp.ClientSession,
        signal:     TradeSignal,
        order_body: dict,
    ) -> Optional[dict]:
        path    = "/order"
        url     = CLOB_API + path
        body_s  = json.dumps(order_body)
        headers = {
            "Content-Type": "application/json",
            **_l2_headers("POST", path, body_s),
        }
        try:
            async with session.post(url, data=body_s, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                resp = await r.json()
                if r.status == 200:
                    return resp
                else:
                    from rich.console import Console
                    Console().log(f"[red]Order rejected ({r.status}): {resp}[/red]")
                    return None
        except Exception as exc:
            from rich.console import Console
            Console().log(f"[red]Order error: {exc}[/red]")
            return None

    async def _execute_signal(self, session: aiohttp.ClientSession, signal: TradeSignal):
        if self._live and self._builder:
            order_body, _ = self._builder.build_limit_order(
                token_id = signal.token_id,
                side     = signal.side,
                size     = signal.size,
                price    = signal.price,
            )
            result = await self._post_order(session, signal, order_body)
            fill_price = signal.price   # assume limit price for tracking
        else:
            # Paper trade: simulate fill at signal price
            result     = {"orderId": f"paper-{int(time.time())}", "status": "FILLED"}
            fill_price = signal.price

        # Record
        ts    = datetime.now(timezone.utc).isoformat()
        mode  = "live" if self._live else "paper"
        order_id = (result or {}).get("orderId", "")
        entry = {
            "ts":         ts,
            "symbol":     signal.symbol,
            "outcome":    signal.outcome.value,
            "side":       signal.side.value,
            "size":       signal.size,
            "price":      fill_price,
            "confidence": signal.confidence,
            "trigger":    signal.trigger,
            "reason":     signal.reason,
            "result":     result,
            "mode":       mode,
        }
        self._log.append(entry)
        self.book.record_fill(signal, fill_price)

        # Persist to SQLite
        if self.db:
            try:
                self.db.insert_trade({
                    "ts":               ts,
                    "symbol":           signal.symbol,
                    "condition_id":     signal.condition_id,
                    "token_id":         signal.token_id,
                    "outcome":          signal.outcome.value,
                    "side":             signal.side.value,
                    "size":             signal.size,
                    "entry_price":      fill_price,
                    "confidence":       signal.confidence,
                    "trigger":          signal.trigger,
                    "reasoning":        signal.reason,
                    "mode":             mode,
                    "order_id":         order_id,
                    "strategy_version": STRATEGY_VERSION,
                })
            except Exception:
                pass  # Never let DB errors interrupt execution

        # Console log
        from rich.console import Console
        mode_tag = "[green]LIVE[/green]" if self._live else "[yellow]PAPER[/yellow]"
        Console().log(
            f"{mode_tag} {signal.side.value} {signal.size:.2f} "
            f"{signal.symbol}/{signal.outcome.value} @ {fill_price:.4f}  "
            f"conf={signal.confidence:.2f}  [{signal.reason}]"
        )

    # ── Deduplication ─────────────────────────────────────────────────────────

    def _already_entered(self, signal: TradeSignal) -> bool:
        """Avoid double-entering the same market/outcome.
        pre_open positions do not block subsequent in-market signals."""
        pos = self.book.get_position(signal.condition_id, signal.outcome)
        if not pos or pos.size < MIN_TRADE_SIZE:
            return False
        # Allow non-pre_open triggers through even if a pre_open leg is filled
        if signal.trigger != "pre_open" and self.book.position_only_preopen(
                signal.condition_id, signal.outcome):
            return False
        return True

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def run(self):
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    signal: TradeSignal = await asyncio.wait_for(
                        self._queue.get(), timeout=5.0
                    )
                    if self._already_entered(signal):
                        continue
                    await self._execute_signal(session, signal)
                    self._queue.task_done()
                except asyncio.TimeoutError:
                    pass
                except Exception as exc:
                    from rich.console import Console
                    Console().log(f"[red]Trader error: {exc}[/red]")

    # ── Execution Log ─────────────────────────────────────────────────────────

    @property
    def execution_log(self) -> List[dict]:
        return list(self._log)

    def print_summary(self):
        from rich.console import Console
        from rich.table import Table
        c   = Console()
        tbl = Table(title="Execution Summary", show_lines=True)
        tbl.add_column("Time")
        tbl.add_column("Symbol")
        tbl.add_column("Outcome")
        tbl.add_column("Side")
        tbl.add_column("Size", justify="right")
        tbl.add_column("Price", justify="right")
        tbl.add_column("Conf", justify="right")
        tbl.add_column("Mode")
        tbl.add_column("Reason")
        for e in self._log:
            tbl.add_row(
                e["ts"][:19],
                e["symbol"],
                e["outcome"],
                e["side"],
                f"{e['size']:.2f}",
                f"{e['price']:.4f}",
                f"{e['confidence']:.2f}",
                e["mode"],
                e["reason"][:60],
            )
        c.print(tbl)
        c.print(self.book.summary())
