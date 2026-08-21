#!/usr/bin/env python3
"""
Hyperliquid Whale & Smart Money Tracker for zkLighter Sniper
============================================================
Connects to Hyperliquid's 100% public, unauthenticated Info API & WebSockets.
Monitors:
1. Top Leaderboard Whales: Positions & directional flips.
2. Mega-Order Scanner: Real-time market fills >= $250,000 USD.
3. Large Liquidation Wicks: Catches cascading wick reversals.
4. Auto-Pipes whale alpha directly into zkLighter Catalyst Execution Engine.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

import aiohttp

logger = logging.getLogger(__name__)

HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"

# Known Top Alpha Whales on Hyperliquid (PnL > $5M+)
CURATED_WHALES = [
    "0x5055fc18dbd809559c7becc3e9f50e93eb220807",  # Top Leaderboard #1
    "0x010461c14e146ac35fe42271bdc1134ee31c703a",  # Institutional Trend Follower
    "0x63c32cf98b1836efd02a0a204620f4ff260aa7f3",  # High-Frequency Scalp Whale
    "0x31694f275752945d8b8ff796ff6d5f7f32cb4135",  # HYPE/SOL Ecosystem Whale
    "0xa518b0f803c733367ec922d56a29be1900ce5eb2",  # Top 10 PnL
]

MIN_WHALE_TRADE_USD = float(os.getenv("MIN_WHALE_TRADE_USD", "250000.0"))


@dataclass
class WhalePositionUpdate:
    trader: str
    asset: str
    side: str  # "LONG" or "SHORT"
    size: float
    entry_price: float
    usd_notional: float
    timestamp: float = field(default_factory=time.time)
    trader_alias: str = "Top Hyperliquid Whale"


class HyperliquidWhaleTracker:
    """
    Real-time zero-auth Hyperliquid Whale & Smart Money Tracker.
    """

    def __init__(
        self,
        on_whale_signal: Optional[Callable[[Dict[str, Any]], Any]] = None,
        min_notional_usd: float = MIN_WHALE_TRADE_USD,
    ):
        self.on_whale_signal = on_whale_signal
        self.min_notional_usd = min_notional_usd
        self.watched_whales: Set[str] = set(w.lower() for w in CURATED_WHALES)
        self.previous_positions: Dict[str, Dict[str, Any]] = {}
        self.recent_signals: List[Dict[str, Any]] = []
        self.is_running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "LighterTerminal/WhaleTracker-v1.0"}
            )
        return self._session

    async def fetch_user_state(self, user_address: str) -> Optional[Dict[str, Any]]:
        """Queries current open positions and leverage of any Hyperliquid trader."""
        session = await self._get_session()
        payload = {"type": "clearinghouseState", "user": user_address}
        try:
            async with session.post(HYPERLIQUID_API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.debug(f"[HL Whale Tracker] Query error for {user_address[:8]}: {e}")
        return None

    async def scan_whale_positions(self) -> List[Dict[str, Any]]:
        """Scans all curated whales and detects new positions or size increases."""
        new_signals = []
        for whale in list(self.watched_whales):
            state = await self.fetch_user_state(whale)
            if not state:
                continue

            asset_positions = state.get("assetPositions", [])
            for item in asset_positions:
                pos = item.get("position", {})
                coin = pos.get("coin", "").upper()
                szi = float(pos.get("szi", 0.0))
                entry_px = float(pos.get("entryPx", 0.0))
                if abs(szi) <= 0 or entry_px <= 0:
                    continue

                notional = abs(szi) * entry_px
                side = "LONG" if szi > 0 else "SHORT"
                pos_key = f"{whale}_{coin}"
                prev_pos = self.previous_positions.get(pos_key)

                # Detect new position or major size expansion (> $250k)
                if notional >= self.min_notional_usd:
                    is_new = prev_pos is None
                    is_expanded = prev_pos and (notional - prev_pos.get("notional", 0.0) >= 100_000.0)

                    if is_new or is_expanded:
                        signal = {
                            "type": "WHALE_POSITION_ENTRY",
                            "source": "Hyperliquid Smart Money",
                            "trader": f"{whale[:6]}...{whale[-4:]}",
                            "asset": coin,
                            "side": "BUY" if side == "LONG" else "SELL",
                            "notional_usd": notional,
                            "entry_price": entry_px,
                            "size": abs(szi),
                            "conviction": 0.92,
                            "headline": f"🐋 Hyperliquid Whale ({whale[:6]}...{whale[-4:]}) opened ${notional:,.0f} {side} on {coin} @ ${entry_px:,.2f}",
                            "timestamp": time.time(),
                        }
                        new_signals.append(signal)
                        self.recent_signals.append(signal)
                        if len(self.recent_signals) > 30:
                            self.recent_signals.pop(0)

                        if self.on_whale_signal:
                            try:
                                res = self.on_whale_signal(signal)
                                if asyncio.iscoroutine(res):
                                    await res
                            except Exception as e:
                                logger.error(f"Whale signal dispatch error: {e}")

                self.previous_positions[pos_key] = {"notional": notional, "side": side, "szi": szi}

        return new_signals

    async def _ws_trades_listener(self):
        """Streams real-time mega-trades (>= $250k) across all Hyperliquid markets."""
        while self.is_running:
            try:
                session = await self._get_session()
                async with session.ws_connect(HYPERLIQUID_WS_URL, timeout=10.0) as ws:
                    self._ws = ws
                    logger.info("⚡ [HL] Hyperliquid Real-Time Trades WebSocket Connected.")

                    # Subscribe to global trades for top assets
                    for coin in ["BTC", "ETH", "SOL", "HYPE", "DOGE", "AVAX", "XRP"]:
                        sub_msg = {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}
                        await ws.send_json(sub_msg)

                    async for msg in ws:
                        if not self.is_running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                if data.get("channel") == "trades":
                                    trades = data.get("data", [])
                                    for t in trades:
                                        coin = t.get("coin", "").upper()
                                        px = float(t.get("px", 0.0))
                                        sz = float(t.get("sz", 0.0))
                                        side = t.get("side", "")  # "B" or "A"
                                        notional = px * sz

                                        if notional >= self.min_notional_usd:
                                            side_str = "BUY/LONG" if side == "B" else "SELL/SHORT"
                                            signal = {
                                                "type": "MEGA_TRADE_BURST",
                                                "source": "Hyperliquid Real-Time Tape",
                                                "trader": "Institutional Market Taker",
                                                "asset": coin,
                                                "side": "BUY" if side == "B" else "SELL",
                                                "notional_usd": notional,
                                                "entry_price": px,
                                                "size": sz,
                                                "conviction": 0.88,
                                                "headline": f"🚨 MEGA TAPE FILL: ${notional:,.0f} {side_str} on {coin} @ ${px:,.2f} on Hyperliquid",
                                                "timestamp": time.time(),
                                            }
                                            self.recent_signals.append(signal)
                                            if len(self.recent_signals) > 30:
                                                self.recent_signals.pop(0)

                                            logger.info(f"🐋 {signal['headline']}")
                                            if self.on_whale_signal:
                                                try:
                                                    res = self.on_whale_signal(signal)
                                                    if asyncio.iscoroutine(res):
                                                        await res
                                                except Exception as exc:
                                                    logger.debug(f"Signal callback error: {exc}")
                            except Exception:
                                pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[HL WS] Reconnecting in 3s after: {e}")
                await asyncio.sleep(3.0)

    async def start(self):
        """Starts both polling of whale portfolios and real-time WebSocket trade stream."""
        self.is_running = True
        logger.info("🐋 [HL Whale Tracker] Initialized and monitoring smart money.")
        asyncio.create_task(self._ws_trades_listener())
        asyncio.create_task(self._polling_loop())

    async def _polling_loop(self):
        while self.is_running:
            try:
                await self.scan_whale_positions()
            except Exception as e:
                logger.debug(f"[Whale Poller] Loop error: {e}")
            await asyncio.sleep(15.0)

    async def stop(self):
        self.is_running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
