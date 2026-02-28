"""
Polymarket 15M Crypto Market System — Orchestrator

Launches four concurrent agents:
  Agent 1 - CollectorAgent:  Fetches live market data, order books, trades
  Agent 2 - DecisionEngine:  Analyses data and emits TradeSignals
  Agent 3 - TraderAgent:     Executes orders (paper or live)
  Agent 4 - DataExporter:    Pushes snapshots to GitHub every 5 min

Usage
-----
  # Full pipeline — paper trade (default, no credentials needed):
  python -m polymarket

  # Data collection only (no signals / trades):
  python -m polymarket --data-only

  # Live trading (requires env vars):
  POLY_PRIVATE_KEY=<key> POLY_ADDRESS=<addr> \\
  POLY_API_KEY=<key> POLY_API_SECRET=<secret> POLY_API_PASSPHRASE=<pass> \\
  python -m polymarket

Environment Variables
---------------------
  POLY_PRIVATE_KEY     Ethereum private key (0x-prefixed)
  POLY_ADDRESS         Polygon wallet address
  POLY_API_KEY         Polymarket CLOB API key
  POLY_API_SECRET      CLOB API secret (base64)
  POLY_API_PASSPHRASE  CLOB API passphrase

  GITHUB_TOKEN         Token with write scope for Bob repo export
  EXPORT_REPO          Target repo URL  (default: Cyberdude01/Bob)
  EXPORT_INTERVAL      Push cadence in seconds (default: 300)
"""
from __future__ import annotations

import asyncio
import sys
from rich.console import Console

from .collector import CollectorAgent, MarketState, run_display
from .decision  import DecisionEngine, PositionBook
from .exporter  import DataExporter
from .trader    import TraderAgent

console = Console()


async def main(data_only: bool = False):
    console.print("[bold cyan]Polymarket 15M Market System starting…[/bold cyan]")

    # ── Shared State ──────────────────────────────────────────────────────────
    state        = MarketState()
    signal_queue = asyncio.Queue(maxsize=64)

    # ── Agent 1: Data Collector ───────────────────────────────────────────────
    collector = CollectorAgent(state)

    if data_only:
        console.print("[dim]Data-only mode — trading agents disabled[/dim]")
        exporter = DataExporter(state)
        try:
            await asyncio.gather(
                collector.run(),
                run_display(state),
                exporter.run(),
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        return

    # ── Agent 2: Decision Engine ───────────────────────────────────────────────
    book     = PositionBook(initial_balance=0.0)
    decision = DecisionEngine(state, book)

    # ── Agent 3: Trade Executor ────────────────────────────────────────────────
    trader = TraderAgent(state, book, signal_queue)

    # ── Agent 4: GitHub Data Exporter ─────────────────────────────────────────
    exporter = DataExporter(
        state,
        book       = book,
        signal_log = decision.signal_log,
        exec_log   = trader.execution_log,
    )

    console.print(
        "[bold]Agents:[/bold]\n"
        "  [green]1[/green] CollectorAgent  — live market data + order books\n"
        "  [green]2[/green] DecisionEngine  — signal generation (paper trade)\n"
        "  [green]3[/green] TraderAgent     — order execution\n"
        "  [green]4[/green] DataExporter    — GitHub snapshot push every 5 min\n"
    )

    try:
        await asyncio.gather(
            collector.run(),
            decision.run(signal_queue, interval=2.0),
            trader.run(),
            run_display(
                state,
                signal_log = decision.signal_log,
                exec_log   = trader.execution_log,
                book       = book,
            ),
            exporter.run(),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        trader.print_summary()


def cli():
    data_only = "--data-only" in sys.argv
    asyncio.run(main(data_only=data_only))


if __name__ == "__main__":
    cli()
