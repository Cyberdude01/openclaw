"""
Polymarket 15M Crypto Market System — Orchestrator

Launches three concurrent agents:
  Agent 1 - CollectorAgent:  Fetches live market data, order books, trades
  Agent 2 - DecisionEngine:  Analyses data and emits TradeSignals
  Agent 3 - TraderAgent:     Executes orders from the signal queue

Usage
-----
  # Data collection only (no trading):
  python -m polymarket

  # With live trading (requires env vars):
  POLY_PRIVATE_KEY=<key> POLY_ADDRESS=<addr> \\
  POLY_API_KEY=<key> POLY_API_SECRET=<secret> POLY_API_PASSPHRASE=<pass> \\
  python -m polymarket

  # Paper-trade mode (signals generated, no real orders):
  python -m polymarket --paper

  # Data-only mode (no trading agent):
  python -m polymarket --data-only

Environment Variables
---------------------
  POLY_PRIVATE_KEY     Ethereum private key (0x-prefixed)
  POLY_ADDRESS         Polygon wallet address
  POLY_API_KEY         Polymarket CLOB API key
  POLY_API_SECRET      CLOB API secret (base64)
  POLY_API_PASSPHRASE  CLOB API passphrase
"""
from __future__ import annotations

import asyncio
import sys
from rich.console import Console

from .collector  import CollectorAgent, MarketState, run_display
from .decision   import DecisionEngine, PositionBook
from .trader     import TraderAgent

console = Console()


async def main(data_only: bool = False, paper: bool = False):
    console.print("[bold cyan]Polymarket 15M Market System starting…[/bold cyan]")

    # ── Shared State ──────────────────────────────────────────────────────────
    state        = MarketState()
    signal_queue = asyncio.Queue(maxsize=64)

    # ── Agent 1: Data Collector ───────────────────────────────────────────────
    collector = CollectorAgent(state)

    if data_only:
        console.print("[dim]Running in data-only mode (no trading agents)[/dim]")
        try:
            await asyncio.gather(
                collector.run(),
                run_display(state),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        return

    # ── Agent 2: Decision Engine ───────────────────────────────────────────────
    book     = PositionBook(initial_balance=0.0)
    decision = DecisionEngine(state, book)

    # ── Agent 3: Trade Executor ────────────────────────────────────────────────
    trader = TraderAgent(state, book, signal_queue)

    console.print(
        "[bold]Agents:[/bold]\n"
        "  [green]1[/green] CollectorAgent  — live market data\n"
        "  [green]2[/green] DecisionEngine  — signal generation\n"
        "  [green]3[/green] TraderAgent     — order execution\n"
    )

    try:
        await asyncio.gather(
            collector.run(),
            decision.run(signal_queue, interval=2.0),
            trader.run(),
            run_display(state),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        trader.print_summary()


def cli():
    data_only = "--data-only" in sys.argv
    paper     = "--paper"     in sys.argv
    asyncio.run(main(data_only=data_only, paper=paper))


if __name__ == "__main__":
    cli()
