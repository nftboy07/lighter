#!/usr/bin/env python3
"""
Lighter DEX High-Frequency Market Maker (MM) Orchestrator
=========================================================
Master Entrypoint for Lighter DEX Market Making Bot:
- Real-time WebSocket Orderbook & Trades consumer
- Continuous Avellaneda-Stoikov & GLFT Quoting Engine
- Queue-preserving Deadband Order Management System (OMS)
- Institutional Risk Controls & Circuit Breakers
- SQLite Volume & Campaign Points Persistence
- Live Terminal Dashboard & Interactive Telegram Panel
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from lighter_db import LighterDBManager
from lighter_execution import (
    ActiveOrder,
    LighterExecutionEngine,
    LighterWebSocketStreamer,
)
from lighter_risk_manager import LighterRiskManager, RiskLimits
from lighter_strategy import (
    AvellanedaStoikovQuoter,
    L2OrderBook,
    OrderSide,
    TargetQuote,
)
from lighter_telegram import LighterTelegramBot, tg_send
from lighter_news_sniper import (
    CatalystClassifier,
    CatalystSignal,
    NewsIngestionManager,
    NewsItem,
)

load_dotenv()

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("LighterMM")


def mask_sensitive(val: str, show_last: int = 4) -> str:
    """Masks secret keys in terminal output."""
    if not val or len(val) < 8:
        return "***"
    return val[:4] + "..." + val[-show_last:]


class LighterMarketMakerBot:
    """
    Master Market Maker Bot for zkLighter Orderbook DEX.
    Features Hybrid Quoting Engine:
    - In quiet market periods: 0-fee Avellaneda-Stoikov quoting to capture spread & farm Robinhood points.
    - Instant Catalyst Switch: Instantly cancels maker quotes and triggers directional taker snipe upon breaking news.
    """

    def __init__(
        self,
        market_index: int = 0,
        is_paper_mode: bool = True,
        base_size: float = 0.05,
        target_spread_bps: float = 2.0,
        num_layers: int = 3,
        gamma: float = 0.05,
        kappa: float = 1.8,
        phi: float = 0.01,
        max_inventory: float = 1.0,
        soft_inventory: float = 0.5,
        max_daily_loss_usd: float = 100.0,
        db_path: str = "lighter_mm.db",
        enable_telegram: bool = True,
        enable_hybrid: bool = True,
        catalyst_cooldown_sec: float = 30.0,
    ):
        self.market_index = market_index
        self.is_paper_mode = is_paper_mode
        self.base_size = base_size
        self.target_spread_bps = target_spread_bps
        self.num_layers = num_layers
        self.is_running = False
        self.enable_hybrid = enable_hybrid
        self.catalyst_cooldown_sec = catalyst_cooldown_sec
        self.engine_state = "MAKER_QUOTING"
        self.last_catalyst_time: float = 0.0
        self.active_catalyst: Optional[CatalystSignal] = None

        # 1. Initialize SQLite Database
        self.db = LighterDBManager(db_path=db_path)

        # 2. Initialize Quantitative Quoter (0-fee Avellaneda-Stoikov)
        self.quoter = AvellanedaStoikovQuoter(
            gamma=gamma,
            kappa=kappa,
            volatility=0.015,
            phi=phi,
            num_layers=num_layers,
            base_size=base_size,
            target_spread_bps=target_spread_bps,
        )

        # 3. Initialize Risk Manager
        limits = RiskLimits(
            max_inventory=max_inventory,
            soft_inventory_limit=soft_inventory,
            max_daily_loss_usd=max_daily_loss_usd,
        )
        self.risk_manager = LighterRiskManager(limits=limits)

        # 4. Initialize Execution Engine
        account_idx = int(os.getenv("LIGHTER_ACCOUNT_INDEX", "0"))
        api_key_idx = int(os.getenv("LIGHTER_API_KEY_INDEX", "2"))
        private_key = os.getenv("LIGHTER_API_PRIVATE_KEY", "")
        base_url = os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")

        self.execution = LighterExecutionEngine(
            base_url=base_url,
            account_index=account_idx,
            api_key_index=api_key_idx,
            api_private_key=private_key,
            market_index=market_index,
            is_paper_mode=is_paper_mode,
            on_fill_callback=self.on_fill_executed,
        )

        # 5. Initialize WebSocket Streamer
        ws_url = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
        self.ws_streamer = LighterWebSocketStreamer(
            ws_url=ws_url,
            market_index=market_index,
            on_orderbook_callback=self.on_orderbook_update,
            on_trade_callback=self.on_public_trade_update,
            on_heartbeat_callback=self.risk_manager.record_heartbeat,
        )

        # 6. Initialize Hybrid Catalyst Ingestion Streams
        self.classifier: Optional[CatalystClassifier] = None
        self.news_manager: Optional[NewsIngestionManager] = None
        if self.enable_hybrid:
            self.classifier = CatalystClassifier()
            news_db = os.getenv("NEWS_DB_PATH", db_path)
            self.news_manager = NewsIngestionManager(self._handle_news_event, db_path=news_db)

        # State tracking
        self.current_book: Optional[L2OrderBook] = None
        self.prev_book: Optional[L2OrderBook] = None
        self.last_dashboard_render: float = 0.0
        self.dashboard_interval_sec: float = 3.0
        self.loop = None

        # 7. Initialize Telegram Bot Context
        self.tg_bot = None
        if enable_telegram:
            self.tg_context = {
                "market_index": self.market_index,
                "is_paper_mode": self.is_paper_mode,
                "quoter": self.quoter,
                "risk_manager": self.risk_manager,
                "execution_engine": self.execution,
                "db": self.db,
                "current_book": None,
                "bot_instance": self,
            }
            self.tg_bot = LighterTelegramBot(self.tg_context)

    async def _handle_news_event(self, news: NewsItem, event: Optional[Any] = None):
        """Processes incoming news headlines for instant catalyst classification."""
        if not self.enable_hybrid or not self.classifier:
            return

        signal = self.classifier.process_news(news)
        if signal:
            await self.on_catalyst_trigger(signal, event)

    async def on_catalyst_trigger(self, signal: CatalystSignal, event: Optional[Any] = None) -> Dict[str, Any]:
        """
        Instant Catalyst Switch:
        1. Immediately cancels all maker quotes in 0ms.
        2. Switches engine state to CATALYST_SNIPING.
        3. Fires directional taker snipe order.
        4. Sends high-priority Telegram alert.
        """
        logger.warning(
            f"⚡ [CATALYST SWITCH] Breaking Catalyst Detected: '{signal.headline}' | "
            f"Target: {signal.target_asset} | Sentiment: {signal.sentiment} (Score: {signal.conviction_score:.2f})"
        )

        self.engine_state = "CATALYST_SNIPING"
        self.last_catalyst_time = time.time()
        self.active_catalyst = signal

        # Step 1: Immediately cancel maker quotes
        canceled_count = await self.execution.cancel_all_orders()
        logger.info(f"⚡ [CATALYST SWITCH] Canceled {canceled_count} open maker quotes in 0ms.")

        # Step 2: Trigger directional taker snipe
        side = OrderSide.BUY if signal.sentiment == "BULLISH" else OrderSide.SELL
        snipe_size = self.base_size * 2.0

        exec_price = 0.0
        if self.current_book and self.current_book.mid_price > 0:
            exec_price = self.current_book.best_ask if side == OrderSide.BUY else self.current_book.best_bid
            if exec_price <= 0:
                exec_price = self.current_book.mid_price
        else:
            exec_price = float(os.getenv("LIGHTER_ETH_PRICE", "2650.0"))

        result = await self.execution.execute_taker_snipe(
            side=side,
            price=exec_price,
            size=snipe_size,
            reason=f"CATALYST: {signal.headline[:40]}",
        )

        # Step 3: Send high-priority Telegram alert
        tg_text = (
            f"🚨 <b>HYBRID ENGINE: INSTANT CATALYST SWITCH</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📰 <b>Headline:</b> {signal.headline}\n"
            f"🎯 <b>Asset:</b> {signal.target_asset} (Market #{signal.market_index})\n"
            f"⚡ <b>Sentiment:</b> {signal.sentiment} (Score: {signal.conviction_score:.2f})\n"
            f"🚫 <b>Maker Quotes:</b> CANCELLED ({canceled_count} orders)\n"
            f"🚀 <b>Taker Snipe:</b> {side.value} {snipe_size:.4f} @ ${exec_price:,.2f}\n"
            f"💰 <b>Trade Notional:</b> ${exec_price * snipe_size:,.2f} USD\n"
            f"✨ <i>0-fee Avellaneda-Stoikov quoting will safely resume after market stabilization.</i>"
        )
        tg_send(tg_text)

        return result

    def on_fill_executed(
        self,
        order: ActiveOrder,
        fill_qty: float,
        fill_price: float,
        realized_pnl: float,
        is_maker: bool = True,
    ):
        """Callback invoked whenever a maker quote or taker snipe receives a fill."""
        usd_value = fill_qty * fill_price
        side_str = order.side.value

        # Update Risk Manager
        delta_inv = fill_qty if order.side == OrderSide.BUY else -fill_qty
        self.risk_manager.update_inventory(delta_inv)
        self.risk_manager.update_pnl(realized_pnl, usd_value)

        # Record to SQLite DB
        self.db.record_fill(
            market_index=self.market_index,
            order_id=order.order_id,
            client_order_id=str(order.client_order_id),
            side=side_str,
            price=fill_price,
            size=fill_qty,
            usd_value=usd_value,
            realized_pnl=realized_pnl,
            is_maker=is_maker,
        )

        # Console log
        pnl_badge = f"[PnL: ${realized_pnl:+.2f}]" if realized_pnl != 0 else ""
        fill_type = "MAKER" if is_maker else "TAKER SNIPE"
        logger.info(
            f"⚡ [{fill_type} FILL] {side_str} {fill_qty:.4f} @ ${fill_price:,.2f} (${usd_value:,.2f}) | "
            f"Inv: {self.risk_manager.inventory:+.4f} {pnl_badge}"
        )

        # Outbound Telegram notification
        stats = self.db.get_stats(market_index=self.market_index)
        title_badge = "⚡ <b>Maker Fill Executed</b>" if is_maker else "🚀 <b>Taker Catalyst Snipe Executed</b>"
        layer_badge = f" | <b>Layer:</b> {order.layer}" if is_maker and order.layer >= 0 else ""
        tg_text = (
            f"{title_badge}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Side:</b> {side_str}{layer_badge}\n"
            f"<b>Price:</b> ${fill_price:,.2f}\n"
            f"<b>Size:</b> {fill_qty:.4f} (${usd_value:,.2f})\n"
            f"<b>Realized PnL:</b> ${realized_pnl:+.2f}\n"
            f"<b>Current Inventory:</b> {self.risk_manager.inventory:+.4f}\n"
            f"<b>Total Volume:</b> ${stats['total_volume_usd']:,.2f}\n"
            f"<b>Estimated Points:</b> ✨ {stats['estimated_points']:.3f} pts"
        )
        tg_send(tg_text)

    def on_public_trade_update(self, price: float, size: float, is_ask: bool):
        """Callback for public trades on the market (drives paper fills)."""
        self.execution.handle_market_trade(price, size, is_buyer_maker=not is_ask)

    def on_orderbook_update(self, book: L2OrderBook):
        """Callback triggered upon each Level-2 orderbook delta/snapshot."""
        self.prev_book = self.current_book
        self.current_book = book
        if self.tg_bot and hasattr(self, "tg_context"):
            self.tg_context["current_book"] = book

        if not self.is_running or not self.loop:
            return

        # Schedule high-frequency quoting cycle
        asyncio.run_coroutine_threadsafe(self._process_quoting_cycle(book), self.loop)

    async def _process_quoting_cycle(self, book: L2OrderBook):
        """Core asynchronous market making cycle with Hybrid Quoting Engine."""
        if book.mid_price <= 0:
            return

        # 0. Hybrid Engine Check: In catalyst stabilization window, keep maker quoting paused
        if self.enable_hybrid and self.engine_state == "CATALYST_SNIPING":
            if time.time() - self.last_catalyst_time >= self.catalyst_cooldown_sec:
                logger.info("🟢 [HYBRID] Catalyst cooldown expired. Market calm — Resuming 0-fee Avellaneda-Stoikov quoting.")
                self.engine_state = "MAKER_QUOTING"
                self.active_catalyst = None
            else:
                now = time.time()
                if now - self.last_dashboard_render >= self.dashboard_interval_sec:
                    self.last_dashboard_render = now
                    self.render_dashboard(book)
                return

        # 1. Generate Target Quotes using Avellaneda-Stoikov & GLFT (0-fee maker quoting)
        target_quotes = self.quoter.generate_quotes(
            book=book,
            inventory_q=self.risk_manager.inventory,
            prev_book=self.prev_book,
        )

        # 2. Validate Quotes with Risk Engine
        validated_quotes = self.risk_manager.validate_quotes(
            target_quotes=target_quotes,
            mid_price=book.mid_price,
            current_volatility=self.quoter.sigma,
        )

        # 3. Compute Queue-Preserving Deadband Order Diff
        cancels, placements = self.execution.oms.compute_diff(
            target_quotes=validated_quotes,
            tick_size=self.quoter.tick_size,
        )

        # 4. Execute Batch Submissions / Cancellations
        if cancels or placements:
            await self.execution.execute_diff(cancels, placements)

        # 5. Render Dashboard Periodically
        now = time.time()
        if now - self.last_dashboard_render >= self.dashboard_interval_sec:
            self.last_dashboard_render = now
            self.render_dashboard(book)

    def render_dashboard(self, book: L2OrderBook):
        """Renders live terminal analytics and market state."""
        stats = self.db.get_stats(market_index=self.market_index)
        risk = self.risk_manager.get_status()
        active_orders = list(self.execution.oms.active_orders.values())
        buy_orders = [o for o in active_orders if o.side == OrderSide.BUY]
        sell_orders = [o for o in active_orders if o.side == OrderSide.SELL]

        mode_badge = "🧪 PAPER TRADING" if self.is_paper_mode else "⚡ LIVE TRADING"
        if risk["is_paused"]:
            state_badge = "⏸️ PAUSED"
        elif self.engine_state == "CATALYST_SNIPING":
            state_badge = "⚡ CATALYST TAKER SNIPING"
        else:
            state_badge = "🟢 0-FEE AS QUOTING"

        print("\n" + "═" * 78)
        print(f"  LIGHTER DEX HYBRID MARKET MAKER & CATALYST SNIPER  |  {mode_badge}  |  {state_badge}")
        print("═" * 78)
        print(
            f"  Market: Index {self.market_index}  |  Mid: ${book.mid_price:,.2f}  |  "
            f"Spread: {book.spread_bps:.2f} bps (${book.spread:.2f})  |  Vol: {self.quoter.sigma*100:.2f}%"
        )
        print(
            f"  Inventory: {risk['inventory']:+.4f} units (Soft: ±{risk['soft_inventory']} | Hard: ±{risk['max_inventory']})  |  "
            f"Active Orders: {len(active_orders)} (Bids: {len(buy_orders)} | Asks: {len(sell_orders)})"
        )
        print("─" * 78)
        print(
            f"  Total Volume: ${stats['total_volume_usd']:,.2f}  |  "
            f"Fills: {stats['total_fills']} (Buy: {stats['buy_fills']} | Sell: {stats['sell_fills']})"
        )
        print(
            f"  Realized PnL: ${stats['total_realized_pnl_usd']:+,.2f} (Win: {stats.get('win_rate_pct', 0.0)}%)  |  "
            f"Estimated Points: ✨ {stats['estimated_points']:,.3f} pts (Robinhood Campaign)"
        )
        print("─" * 78)

        # Print active quotes table
        print(f"  {'LAYER':<6} {'BID SIZE':<10} {'BID PRICE':<12} | {'ASK PRICE':<12} {'ASK SIZE':<10}")
        max_layers = max(len(buy_orders), len(sell_orders), 1)
        for i in range(max_layers):
            b = buy_orders[i] if i < len(buy_orders) else None
            a = sell_orders[i] if i < len(sell_orders) else None
            bid_str = f"{b.size:.4f}" if b else "-"
            bid_p_str = f"${b.price:,.2f}" if b else "-"
            ask_p_str = f"${a.price:,.2f}" if a else "-"
            ask_str = f"{a.size:.4f}" if a else "-"
            print(f"  {i:<6} {bid_str:<10} {bid_p_str:<12} | {ask_p_str:<12} {ask_str:<10}")

        print("═" * 78)

    async def start(self):
        """Starts the market maker execution loop and hybrid news stream."""
        self.is_running = True
        self.loop = asyncio.get_running_loop()

        print("=" * 78)
        print("  STARTING LIGHTER DEX HYBRID MARKET MAKER BOT")
        print(f"  Mode: {'PAPER TRADING / SIMULATION' if self.is_paper_mode else 'LIVE - REAL FUNDS'}")
        print(f"  Market Index: {self.market_index}")
        print(f"  Base Size: {self.base_size} | Target Half-Spread: {self.target_spread_bps} bps | Layers: {self.num_layers}")
        print(f"  Hybrid Engine: {'ENABLED (AS Quoting + Instant Catalyst Snipe)' if self.enable_hybrid else 'DISABLED (Pure MM)'}")
        print("=" * 78)

        # Start Telegram interactive bot
        if self.tg_bot:
            self.tg_bot.start_polling_in_background()

        # Start Hybrid News Ingestion Scheduler if enabled
        if self.enable_hybrid and self.news_manager:
            await self.news_manager.start()
            logger.info("📡 [HYBRID] News Ingestion & Catalyst Classifier active in background.")

        # Send launch notification
        tg_send(
            f"🚀 <b>Lighter Hybrid MM Bot Started</b>\n"
            f"<b>Mode:</b> {'🧪 PAPER' if self.is_paper_mode else '⚡ LIVE'}\n"
            f"<b>Market:</b> Index {self.market_index}\n"
            f"<b>Base Size:</b> {self.base_size}\n"
            f"<b>Target Half-Spread:</b> {self.target_spread_bps} bps\n"
            f"<b>Layers:</b> {self.num_layers}\n"
            f"<b>Hybrid Engine:</b> {'🟢 ACTIVE' if self.enable_hybrid else '🔴 DISABLED'}"
        )

        # Run WebSocket streaming loop
        await self.ws_streamer.start()

    async def shutdown(self):
        """Performs clean shutdown and quote cancellation."""
        logger.info("[SHUTDOWN] Canceling all active quotes on Lighter...")
        self.is_running = False
        self.ws_streamer.stop()

        canceled = await self.execution.cancel_all_orders()
        logger.info(f"[SHUTDOWN] Cancelled {canceled} open orders.")

        stats = self.db.get_stats(market_index=self.market_index)
        print("\n" + "=" * 78)
        print("  FINAL SESSION SUMMARY REPORT")
        print("=" * 78)
        print(f"  Total Maker Volume:   ${stats['total_volume_usd']:,.2f}")
        print(f"  Total Fills:          {stats['total_fills']}")
        print(f"  Realized PnL:         ${stats['total_realized_pnl_usd']:+,.2f}")
        print(f"  Win Rate:             {stats.get('win_rate_pct', 0.0):.1f}%")
        print(f"  Estimated Points:     ✨ {stats['estimated_points']:,.3f} pts")
        print("=" * 78 + "\n")


def parse_args():
    parser = argparse.ArgumentParser(description="Lighter DEX High-Frequency Market Maker Bot")
    parser.add_argument("--paper", action="store_true", default=True, help="Run in Paper Trading simulation mode (default)")
    parser.add_argument("--live", action="store_true", help="Run in LIVE trading mode on zkLighter")
    parser.add_argument("--market", type=int, default=int(os.getenv("MARKET_INDEX", "0")), help="Market index (default: 0)")
    parser.add_argument("--size", type=float, default=float(os.getenv("BASE_ORDER_SIZE", "0.05")), help="Base order size in units")
    parser.add_argument("--spread", type=float, default=float(os.getenv("TARGET_SPREAD_BPS", "2.0")), help="Target half spread in bps")
    parser.add_argument("--layers", type=int, default=int(os.getenv("NUM_LAYERS", "3")), help="Number of quoting grid tiers")
    parser.add_argument("--no-telegram", action="store_true", help="Disable Telegram bot integration")
    parser.add_argument("--no-hybrid", action="store_true", help="Disable Hybrid instant catalyst switch engine")
    return parser.parse_args()


def main():
    args = parse_args()
    is_paper = not args.live

    bot = LighterMarketMakerBot(
        market_index=args.market,
        is_paper_mode=is_paper,
        base_size=args.size,
        target_spread_bps=args.spread,
        num_layers=args.layers,
        enable_telegram=not args.no_telegram,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_sigint(sig, frame):
        logger.info("\n[INTERRUPT] Received Ctrl+C / SIGINT signal. Initiating graceful shutdown...")
        asyncio.run_coroutine_threadsafe(bot.shutdown(), loop)
        loop.stop()

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        loop.run_until_complete(bot.start())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        loop.run_until_complete(bot.shutdown())
        loop.close()


if __name__ == "__main__":
    main()
