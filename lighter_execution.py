#!/usr/bin/env python3
"""
Execution Engine & Order Management System (OMS) for Lighter DEX
================================================================
Handles:
- Queue-preserving Deadband order diffing
- WebSocket Level-2 Orderbook streaming (wss://mainnet.zklighter.elliot.ai/stream)
- Lighter Protocol SignerClient / TransactionApi integration (L2 Tx)
- High-fidelity Paper Trading Simulator with realistic queue execution
- Atomic batch order placement and cancellation
"""

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import aiohttp
from lighter_strategy import L2OrderBook, OrderBookLevel, OrderSide, TargetQuote

logger = logging.getLogger(__name__)


@dataclass
class ActiveOrder:
    client_order_id: int
    order_id: str
    side: OrderSide
    price: float
    size: float
    layer: int
    timestamp: float
    is_simulated: bool = False


class DeadbandOMS:
    """
    Queue-preserving Order Management System.
    Applies price deadbands and size drift thresholds to prevent unnecessary
    order cancellation churn, maintaining FIFO queue priority at Top-of-Book.
    """

    def __init__(
        self,
        price_deadband_ticks: int = 1,
        size_drift_pct: float = 0.15,
        stale_order_max_age_sec: float = 180.0,
    ):
        self.price_deadband_ticks = price_deadband_ticks
        self.size_drift_pct = size_drift_pct
        self.stale_order_max_age_sec = stale_order_max_age_sec
        self.active_orders: Dict[int, ActiveOrder] = {}  # Key: client_order_id

    def compute_diff(
        self,
        target_quotes: Dict[OrderSide, List[TargetQuote]],
        tick_size: float,
    ) -> Tuple[List[int], List[TargetQuote]]:
        """
        Calculates required cancellations (client_order_ids) and new quotes to place.
        Preserves active orders that are within deadband tolerances.
        """
        cancels_to_execute: List[int] = []
        quotes_to_place: List[TargetQuote] = []

        now = time.time()
        active_list = list(self.active_orders.values())
        matched_client_ids: Set[int] = set()

        for side in [OrderSide.BUY, OrderSide.SELL]:
            targets = target_quotes.get(side, [])
            for target in targets:
                # Find matching active order on same side & layer
                found_match: Optional[ActiveOrder] = None
                for order in active_list:
                    if (
                        order.client_order_id not in matched_client_ids
                        and order.side == side
                        and order.layer == target.layer
                    ):
                        found_match = order
                        break

                if found_match:
                    matched_client_ids.add(found_match.client_order_id)
                    # Check deadband thresholds
                    price_diff_ticks = abs(found_match.price - target.price) / max(0.0001, tick_size)
                    size_diff_pct = abs(found_match.size - target.size) / max(0.0001, found_match.size)
                    age = now - found_match.timestamp

                    # Replace only if deadband breached or order is excessively stale
                    if (
                        price_diff_ticks >= self.price_deadband_ticks
                        or size_diff_pct >= self.size_drift_pct
                        or age >= self.stale_order_max_age_sec
                    ):
                        cancels_to_execute.append(found_match.client_order_id)
                        quotes_to_place.append(target)
                else:
                    # No active order for this layer -> place new
                    quotes_to_place.append(target)

        # Cancel any orphan or leftover active orders
        for order in active_list:
            if order.client_order_id not in matched_client_ids:
                cancels_to_execute.append(order.client_order_id)

        return cancels_to_execute, quotes_to_place

    def register_order(self, order: ActiveOrder):
        """Registers a successfully submitted order in active state."""
        self.active_orders[order.client_order_id] = order

    def remove_order(self, client_order_id: int):
        """Removes an order from active state upon cancel or full fill."""
        self.active_orders.pop(client_order_id, None)

    def clear_all(self):
        """Clears all active orders from memory."""
        self.active_orders.clear()


class LighterPaperSimulator:
    """
    High-fidelity simulation engine for paper trading against real Lighter orderbooks.
    Simulates:
    - Order queuing
    - Fills when public market trades cross simulated orders
    - Position inventory and average entry pricing
    - Realized and unrealized PnL
    """

    def __init__(self, initial_cash_usd: float = 10_000.0):
        self.cash_usd = initial_cash_usd
        self.inventory: float = 0.0
        self.avg_entry_price: float = 0.0
        self.total_volume_usd: float = 0.0
        self.total_realized_pnl: float = 0.0
        self.fill_count: int = 0

    def process_market_trade(
        self,
        trade_price: float,
        trade_size: float,
        is_buyer_maker: bool,
        active_orders: Dict[int, ActiveOrder],
    ) -> List[Tuple[ActiveOrder, float, float]]:
        """
        Evaluates whether a real market execution triggers a fill on our simulated quotes.
        Returns list of (filled_order, fill_qty, realized_pnl).
        """
        fills: List[Tuple[ActiveOrder, float, float]] = []

        for client_id, order in list(active_orders.items()):
            should_fill = False
            fill_qty = min(order.size, trade_size)

            if order.side == OrderSide.BUY:
                # Buy fill triggers if trade executed at or below our bid
                if trade_price <= order.price:
                    should_fill = True
            elif order.side == OrderSide.SELL:
                # Sell fill triggers if trade executed at or above our ask
                if trade_price >= order.price:
                    should_fill = True

            if should_fill and fill_qty > 0:
                realized_pnl = self._execute_fill(order.side, order.price, fill_qty)
                fills.append((order, fill_qty, realized_pnl))

        return fills

    def _execute_fill(self, side: OrderSide, price: float, qty: float) -> float:
        """Updates internal paper inventory and calculates realized PnL."""
        usd_value = price * qty
        self.total_volume_usd += usd_value
        self.fill_count += 1
        realized_pnl = 0.0

        if side == OrderSide.BUY:
            # Adding long / covering short
            if self.inventory < 0:
                # Covering short position
                cover_qty = min(abs(self.inventory), qty)
                realized_pnl = (self.avg_entry_price - price) * cover_qty
                self.total_realized_pnl += realized_pnl

            new_inventory = self.inventory + qty
            if new_inventory > 0:
                total_cost = (max(0.0, self.inventory) * self.avg_entry_price) + (qty * price)
                self.avg_entry_price = total_cost / new_inventory
            elif new_inventory == 0:
                self.avg_entry_price = 0.0

            self.inventory = new_inventory
            self.cash_usd -= usd_value

        else:  # SELL
            # Adding short / selling long
            if self.inventory > 0:
                # Selling long position
                sell_qty = min(self.inventory, qty)
                realized_pnl = (price - self.avg_entry_price) * sell_qty
                self.total_realized_pnl += realized_pnl

            new_inventory = self.inventory - qty
            if new_inventory < 0:
                total_cost = (max(0.0, abs(self.inventory)) * self.avg_entry_price) + (qty * price)
                self.avg_entry_price = total_cost / abs(new_inventory)
            elif new_inventory == 0:
                self.avg_entry_price = 0.0

            self.inventory = new_inventory
            self.cash_usd += usd_value

        return realized_pnl

    def get_unrealized_pnl(self, mid_price: float) -> float:
        """Calculates unrealized PnL based on current mid price."""
        if self.inventory == 0 or self.avg_entry_price == 0:
            return 0.0
        return (mid_price - self.avg_entry_price) * self.inventory


class LighterExecutionEngine:
    """
    Master Execution Engine for Lighter DEX.
    Supports seamless switching between Paper Trading Simulation and Live Trading.
    """

    def __init__(
        self,
        base_url: str = "https://mainnet.zklighter.elliot.ai",
        account_index: int = 0,
        api_key_index: int = 2,
        api_private_key: str = "",
        market_index: int = 0,
        price_decimals: int = 2,
        size_decimals: int = 4,
        tick_size: float = 0.01,
        is_paper_mode: bool = True,
        on_fill_callback: Optional[Callable] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.account_index = account_index
        self.api_key_index = api_key_index
        self.api_private_key = api_private_key
        self.market_index = market_index
        self.price_decimals = price_decimals
        self.size_decimals = size_decimals
        self.tick_size = tick_size
        self.is_paper_mode = is_paper_mode
        self.on_fill_callback = on_fill_callback

        self.oms = DeadbandOMS()
        self.simulator = LighterPaperSimulator()
        self._client_order_counter = int(time.time() * 1000) % 100_000_000
        self.signer_client = None

    async def _ensure_signer(self) -> None:
        """Lazily initializes SignerClient within the active asyncio event loop."""
        if not self.is_paper_mode and self.signer_client is None and self.api_private_key and self.account_index > 0:
            try:
                import lighter
                self.signer_client = lighter.SignerClient(
                    url=self.base_url,
                    api_private_keys={self.api_key_index: self.api_private_key},
                    account_index=self.account_index,
                )
                logger.info(f"[EXEC] Lighter SignerClient initialized for account {self.account_index}")
            except Exception as e:
                logger.error(f"[EXEC] Failed to initialize lighter-sdk SignerClient: {e}. Defaulting to paper mode.")
                self.is_paper_mode = True

    def get_next_client_order_id(self) -> int:
        """Generates unique sequential client order index."""
        self._client_order_counter += 1
        return self._client_order_counter

    def scale_price_to_int(self, price: float) -> int:
        """Scales float price to Lighter integer representation."""
        return int(round(price * (10 ** self.price_decimals)))

    def scale_size_to_int(self, size: float) -> int:
        """Scales float size to Lighter integer representation."""
        return int(round(size * (10 ** self.size_decimals)))

    async def execute_diff(
        self,
        cancels: List[int],
        placements: List[TargetQuote],
    ) -> Tuple[int, int]:
        """
        Executes order cancellations and new quote placements.
        Returns: (cancels_count, placements_count)
        """
        canceled_count = 0
        placed_count = 0

        # 1. Execute Cancellations
        if cancels:
            for client_id in cancels:
                success = await self._cancel_order(client_id)
                if success:
                    self.oms.remove_order(client_id)
                    canceled_count += 1

        # 2. Execute Placements
        if placements:
            for quote in placements:
                client_id = self.get_next_client_order_id()
                order_id = await self._place_order(client_id, quote)
                if order_id:
                    self.oms.register_order(
                        ActiveOrder(
                            client_order_id=client_id,
                            order_id=order_id,
                            side=quote.side,
                            price=quote.price,
                            size=quote.size,
                            layer=quote.layer,
                            timestamp=time.time(),
                            is_simulated=self.is_paper_mode,
                        )
                    )
                    placed_count += 1

        return canceled_count, placed_count

    async def _place_order(self, client_order_id: int, quote: TargetQuote) -> Optional[str]:
        """Submits a single limit order to Lighter or the simulator."""
        if self.is_paper_mode:
            return f"sim_{client_order_id}"

        await self._ensure_signer()
        if self.signer_client:
            try:
                price_int = self.scale_price_to_int(quote.price)
                size_int = self.scale_size_to_int(quote.size)
                is_ask = (quote.side == OrderSide.SELL)

                # Order type: Limit Post-Only
                order_type = getattr(self.signer_client, "ORDER_TYPE_LIMIT", 0)
                time_in_force = getattr(self.signer_client, "ORDER_TIME_IN_FORCE_POST_ONLY", 2)

                tx, tx_hash, err = await self.signer_client.create_order(
                    market_index=self.market_index,
                    client_order_index=client_order_id,
                    base_amount=size_int,
                    price=price_int,
                    is_ask=is_ask,
                    order_type=order_type,
                    time_in_force=time_in_force,
                    api_key_index=self.api_key_index,
                )
                if err:
                    logger.warning(f"[EXEC] Order create rejected: {err}")
                    return None

                return str(tx_hash or client_order_id)
            except Exception as e:
                logger.error(f"[EXEC] Failed to create order {client_order_id}: {e}")
                return None

        return None

    async def _cancel_order(self, client_order_id: int) -> bool:
        """Cancels a single order."""
        if self.is_paper_mode:
            return True

        await self._ensure_signer()
        if self.signer_client:
            try:
                tx, tx_hash, err = await self.signer_client.cancel_order(
                    market_index=self.market_index,
                    order_index=client_order_id,
                    api_key_index=self.api_key_index,
                )
                if err:
                    logger.warning(f"[EXEC] Order cancel rejected for {client_order_id}: {err}")
                    return False
                return True
            except Exception as e:
                logger.error(f"[EXEC] Failed to cancel order {client_order_id}: {e}")
                return False

        return True

    async def cancel_all_orders(self) -> int:
        """Emergency cancellation of all active quotes."""
        active_ids = list(self.oms.active_orders.keys())
        count = len(active_ids)

        if self.is_paper_mode:
            self.oms.clear_all()
            return count

        await self._ensure_signer()
        if self.signer_client:
            try:
                # Cancel each active order
                for cid in active_ids:
                    await self._cancel_order(cid)
            except Exception as e:
                logger.error(f"[EXEC] Error in cancel_all_orders: {e}")

        self.oms.clear_all()
        return count

    def handle_market_trade(self, price: float, size: float, is_buyer_maker: bool = False):
        """Processes real-time trade event for paper simulation fills."""
        if not self.is_paper_mode:
            return

        fills = self.simulator.process_market_trade(
            trade_price=price,
            trade_size=size,
            is_buyer_maker=is_buyer_maker,
            active_orders=self.oms.active_orders,
        )

        for order, fill_qty, realized_pnl in fills:
            # Remove filled order from OMS
            self.oms.remove_order(order.client_order_id)
            # Invoke fill callback if registered
            if self.on_fill_callback:
                self.on_fill_callback(
                    order=order,
                    fill_qty=fill_qty,
                    fill_price=order.price,
                    realized_pnl=realized_pnl,
                )

    async def execute_taker_snipe(
        self,
        side: OrderSide,
        price: float,
        size: float,
        reason: str = "CATALYST_SNIPE",
    ) -> Dict[str, Any]:
        """
        Executes an immediate directional taker snipe order on the DEX.
        Used for instantaneous catalyst reactions when breaking news hits.
        """
        client_id = self.get_next_client_order_id()
        usd_value = price * size
        is_ask = (side == OrderSide.SELL)

        if self.is_paper_mode:
            realized_pnl = self.simulator._execute_fill(side, price, size)
            sim_order = ActiveOrder(
                client_order_id=client_id,
                order_id=f"taker_{client_id}",
                side=side,
                price=price,
                size=size,
                layer=-1,
                timestamp=time.time(),
                is_simulated=True,
            )
            if self.on_fill_callback:
                self.on_fill_callback(
                    order=sim_order,
                    fill_qty=size,
                    fill_price=price,
                    realized_pnl=realized_pnl,
                    is_maker=False,
                )
            return {
                "success": True,
                "client_order_id": client_id,
                "order_id": f"taker_{client_id}",
                "side": side.value,
                "price": price,
                "size": size,
                "usd_value": usd_value,
                "realized_pnl": realized_pnl,
                "is_maker": False,
                "mode": "PAPER",
                "reason": reason,
            }

        await self._ensure_signer()
        if self.signer_client:
            try:
                price_int = self.scale_price_to_int(price)
                size_int = self.scale_size_to_int(size)
                order_type = getattr(self.signer_client, "ORDER_TYPE_MARKET", 1)
                time_in_force = getattr(self.signer_client, "ORDER_TIME_IN_FORCE_IOC", 1)

                tx, tx_hash, err = await self.signer_client.create_order(
                    market_index=self.market_index,
                    client_order_index=client_id,
                    base_amount=size_int,
                    price=price_int,
                    is_ask=is_ask,
                    order_type=order_type,
                    time_in_force=time_in_force,
                    api_key_index=self.api_key_index,
                )
                if err:
                    logger.error(f"[EXEC] Taker snipe rejected: {err}")
                    return {"success": False, "error": str(err)}

                return {
                    "success": True,
                    "client_order_id": client_id,
                    "order_id": str(tx_hash or client_id),
                    "side": side.value,
                    "price": price,
                    "size": size,
                    "usd_value": usd_value,
                    "realized_pnl": 0.0,
                    "is_maker": False,
                    "mode": "LIVE",
                    "reason": reason,
                }
            except Exception as e:
                logger.error(f"[EXEC] Failed to execute taker snipe: {e}")
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "Signer client not initialized"}


class LighterWebSocketStreamer:
    """
    WebSocket client streaming real-time Level 2 orderbook depth and trades
    from zkLighter (wss://mainnet.zklighter.elliot.ai/stream).
    Includes automatic keepalive ping frames every 60s.
    """

    def __init__(
        self,
        ws_url: str = "wss://mainnet.zklighter.elliot.ai/stream",
        market_index: int = 0,
        on_orderbook_callback: Optional[Callable[[L2OrderBook], None]] = None,
        on_trade_callback: Optional[Callable[[float, float, bool], None]] = None,
        on_heartbeat_callback: Optional[Callable[[], None]] = None,
    ):
        self.ws_url = ws_url
        self.market_index = market_index
        self.on_orderbook = on_orderbook_callback
        self.on_trade = on_trade_callback
        self.on_heartbeat = on_heartbeat_callback
        self.is_running = False
        self.current_book = L2OrderBook(market_index=market_index)

    async def start(self):
        """Runs the WebSocket subscription and event processing loop with auto-reconnect."""
        self.is_running = True
        logger.info(f"[WS] Connecting to Lighter WebSocket at {self.ws_url}...")

        while self.is_running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.ws_url, heartbeat=30.0) as ws:
                        logger.info("[WS] Connected. Subscribing to market channels...")

                        # 1. Subscribe to order_book channel
                        book_sub = {
                            "type": "subscribe",
                            "channel": f"order_book:{self.market_index}",
                        }
                        await ws.send_json(book_sub)

                        # 2. Subscribe to trade channel
                        trade_sub = {
                            "type": "subscribe",
                            "channel": f"trade:{self.market_index}",
                        }
                        await ws.send_json(trade_sub)

                        # Start background ping task
                        ping_task = asyncio.create_task(self._ping_loop(ws))

                        try:
                            async for msg in ws:
                                if not self.is_running:
                                    break

                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    self._handle_message(msg.data)
                                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                    logger.warning("[WS] WebSocket closed or error received.")
                                    break
                        finally:
                            ping_task.cancel()

            except Exception as e:
                logger.warning(f"[WS] Connection error: {e}. Reconnecting in 3 seconds...")
                await asyncio.sleep(3.0)

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse):
        """Sends application-level ping frame every 60 seconds."""
        while self.is_running and not ws.closed:
            try:
                await asyncio.sleep(60.0)
                await ws.send_json({"type": "ping"})
            except asyncio.CancelledError:
                break
            except Exception:
                break

    def _handle_message(self, raw_text: str):
        """Parses incoming WebSocket message JSON."""
        if self.on_heartbeat:
            self.on_heartbeat()

        try:
            data = json.loads(raw_text)
        except Exception:
            return

        msg_type = data.get("type", "")
        channel = data.get("channel", "")

        # Handle pong
        if msg_type == "pong":
            return

        # Handle Orderbook update
        if f"order_book:{self.market_index}" in channel or "order_book" in channel:
            ob_data = data.get("order_book", {})
            raw_bids = ob_data.get("bids", [])
            raw_asks = ob_data.get("asks", [])

            bids = [
                OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
                for b in raw_bids
                if "price" in b and "size" in b
            ]
            asks = [
                OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
                for a in raw_asks
                if "price" in a and "size" in a
            ]

            # Ensure proper sorting
            bids.sort(key=lambda x: x.price, reverse=True)
            asks.sort(key=lambda x: x.price, reverse=False)

            self.current_book = L2OrderBook(
                market_index=self.market_index,
                bids=bids,
                asks=asks,
                timestamp=time.time(),
                nonce=ob_data.get("nonce", 0),
            )

            if self.on_orderbook:
                self.on_orderbook(self.current_book)

        # Handle Public Trade update
        elif f"trade:{self.market_index}" in channel or "trade" in channel:
            trades = data.get("trades", [])
            if isinstance(data.get("trade"), dict):
                trades = [data["trade"]]

            for t in trades:
                if "price" in t and "size" in t:
                    p = float(t["price"])
                    s = float(t["size"])
                    is_ask = t.get("is_maker_ask", False)
                    if self.on_trade:
                        self.on_trade(p, s, is_ask)

    def stop(self):
        """Stops the WebSocket streamer."""
        self.is_running = False
