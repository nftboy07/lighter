#!/usr/bin/env python3
"""
Cross-DEX Hyperliquid <-> zkLighter Price-Lag Arbitrage Engine
=============================================================
Institutional-grade latency arbitrage detector between Hyperliquid (leading price discovery)
and zkLighter (L2 DEX).

Key Features:
- Real-time mark price streaming & polling for BTC, ETH, SOL, HYPE on Hyperliquid.
- Synchronized comparison with zkLighter Level-2 Orderbook (mid, best bid, best ask).
- Dynamic detection of price-lag opportunities where Hyperliquid leads by >= 0.25% (25 bps).
- Generates structured `CrossDexArbOpportunity` signals with net edge, target size, and confidence score.
- High-performance zero-allocation evaluation loop with stale quote protection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import aiohttp

logger = logging.getLogger("CrossDexArbitrage")

# Default API endpoints
HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"
ZKLIGHTER_WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
ZKLIGHTER_API_URL = "https://mainnet.zklighter.elliot.ai"

# Supported target assets
DEFAULT_ASSETS = ["BTC", "ETH", "SOL", "HYPE"]

# Market metadata for zkLighter DEX
DEFAULT_MARKET_CONFIGS: Dict[str, Dict[str, Any]] = {
    "ETH": {"market_index": 0, "price_decimals": 2, "size_decimals": 3, "min_size": 0.001, "tick_size": 0.01},
    "BTC": {"market_index": 1, "price_decimals": 1, "size_decimals": 4, "min_size": 0.0001, "tick_size": 0.1},
    "SOL": {"market_index": 2, "price_decimals": 2, "size_decimals": 2, "min_size": 0.01, "tick_size": 0.01},
    "HYPE": {"market_index": 3, "price_decimals": 3, "size_decimals": 2, "min_size": 0.1, "tick_size": 0.001},
}


class ArbDirection(str, Enum):
    """Direction of Cross-DEX arbitrage trade."""
    BUY_ZKLIGHTER_SELL_HL = "BUY_ZKLIGHTER_SELL_HL"  # HL price leads higher; Buy zkLighter ask, hedge on HL
    SELL_ZKLIGHTER_BUY_HL = "SELL_ZKLIGHTER_BUY_HL"  # HL price leads lower; Sell zkLighter bid, hedge on HL


@dataclass(frozen=True)
class CrossDexArbOpportunity:
    """Structured signal emitted when Hyperliquid price-lag spread >= threshold."""
    asset: str
    direction: ArbDirection
    hl_price: float
    zklighter_mid: float
    zklighter_best_bid: float
    zklighter_best_ask: float
    spread_pct: float                # Raw spread decimal (e.g. 0.0032 = 0.32%)
    spread_bps: float                # Spread in basis points (e.g. 32.0 bps)
    executable_spread_bps: float     # Spread vs actionable ask/bid price
    net_edge_bps: float              # Spread net of roundtrip fees & slippage
    target_size: float               # Suggested order size based on depth
    estimated_profit_usd: float      # Projected gross profit
    hl_timestamp: float
    zklighter_timestamp: float
    latency_lag_ms: float
    confidence_score: float          # 0.0 to 1.0 based on liquidity & spread magnitude
    urgency: str                     # "CRITICAL", "HIGH", "MEDIUM"
    timestamp: float = field(default_factory=time.time)

    @property
    def is_actionable(self) -> bool:
        return self.net_edge_bps > 0.0 and self.target_size > 0.0

    def summary(self) -> str:
        return (
            f"[{self.urgency}] {self.asset} {self.direction.value} | "
            f"HL: ${self.hl_price:,.3f} vs zkL: ${self.zklighter_mid:,.3f} "
            f"(Bid: ${self.zklighter_best_bid:,.3f} / Ask: ${self.zklighter_best_ask:,.3f}) | "
            f"Spread: {self.spread_bps:.1f} bps (Net: {self.net_edge_bps:.1f} bps) | "
            f"Est PnL: ${self.estimated_profit_usd:,.2f} on {self.target_size} {self.asset} | "
            f"Lag: {self.latency_lag_ms:.1f}ms"
        )


@dataclass
class HyperliquidPriceState:
    asset: str
    mark_price: float = 0.0
    mid_price: float = 0.0
    funding_rate: float = 0.0
    timestamp: float = 0.0
    update_count: int = 0


@dataclass
class ZkLighterBookState:
    asset: str
    market_index: int
    best_bid: float = 0.0
    best_ask: float = float("inf")
    best_bid_size: float = 0.0
    best_ask_size: float = 0.0
    mid_price: float = 0.0
    timestamp: float = 0.0
    update_count: int = 0

    def update_book(
        self,
        best_bid: float,
        best_ask: float,
        best_bid_size: float = 0.0,
        best_ask_size: float = 0.0,
        timestamp: Optional[float] = None,
    ) -> None:
        self.best_bid = max(0.0, best_bid)
        self.best_ask = best_ask if best_ask > 0 else float("inf")
        self.best_bid_size = max(0.0, best_bid_size)
        self.best_ask_size = max(0.0, best_ask_size)
        self.timestamp = timestamp if timestamp is not None else time.time()
        self.update_count += 1

        if self.best_bid > 0 and self.best_ask < float("inf"):
            self.mid_price = (self.best_bid + self.best_ask) / 2.0
        elif self.best_bid > 0:
            self.mid_price = self.best_bid
        elif self.best_ask < float("inf"):
            self.mid_price = self.best_ask
        else:
            self.mid_price = 0.0


class CrossDexArbitrageEngine:
    """
    Cross-DEX Arbitrage Engine monitoring price-lag between Hyperliquid and zkLighter.
    
    Detects when Hyperliquid (price discovery leader) moves ahead of zkLighter orderbooks
    by >= min_spread_bps (default 25 bps / 0.25%).
    """

    def __init__(
        self,
        assets: Optional[Sequence[str]] = None,
        min_spread_bps: float = 25.0,        # 25 bps = 0.25%
        max_staleness_sec: float = 2.5,       # Max age of price feeds before deemed stale
        roundtrip_fee_bps: float = 5.0,       # Total estimated maker/taker fees + slippage
        min_notional_usd: float = 50.0,       # Min notional to consider actionable
        max_notional_usd: float = 25000.0,    # Max order size cap
        on_opportunity: Optional[Callable[[CrossDexArbOpportunity], Any]] = None,
        market_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.assets = list(assets or DEFAULT_ASSETS)
        self.min_spread_bps = float(os.getenv("CROSS_DEX_MIN_SPREAD_BPS", str(min_spread_bps)))
        self.max_staleness_sec = float(os.getenv("CROSS_DEX_MAX_STALENESS_SEC", str(max_staleness_sec)))
        self.roundtrip_fee_bps = float(os.getenv("CROSS_DEX_FEE_BPS", str(roundtrip_fee_bps)))
        self.min_notional_usd = min_notional_usd
        self.max_notional_usd = max_notional_usd
        self.on_opportunity = on_opportunity
        self.market_configs = dict(market_configs or DEFAULT_MARKET_CONFIGS)

        # State storage
        self.hl_states: Dict[str, HyperliquidPriceState] = {
            asset: HyperliquidPriceState(asset=asset) for asset in self.assets
        }
        self.zkl_states: Dict[str, ZkLighterBookState] = {
            asset: ZkLighterBookState(
                asset=asset,
                market_index=self.market_configs.get(asset, {}).get("market_index", idx),
            )
            for idx, asset in enumerate(self.assets)
        }

        # Opportunity history and queue
        self.recent_opportunities: List[CrossDexArbOpportunity] = []
        self.max_history_len = 500
        self.opportunity_queue: asyncio.Queue[CrossDexArbOpportunity] = asyncio.Queue()

        # Engine lifecycle
        self.is_running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._background_tasks: List[asyncio.Task] = []

        # Metrics
        self.total_evaluations = 0
        self.total_opportunities_found = 0
        self.opportunities_by_asset: Dict[str, int] = {asset: 0 for asset in self.assets}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "Institutional-CrossDexArb/1.0"}
            )
        return self._session

    def update_hyperliquid_price(
        self,
        asset: str,
        price: float,
        mid_price: Optional[float] = None,
        funding_rate: float = 0.0,
        timestamp: Optional[float] = None,
    ) -> Optional[CrossDexArbOpportunity]:
        """
        Updates Hyperliquid mark price for an asset and immediately triggers arbitrage evaluation.
        """
        norm_asset = asset.upper()
        if norm_asset not in self.hl_states:
            self.hl_states[norm_asset] = HyperliquidPriceState(asset=norm_asset)
        
        now = timestamp if timestamp is not None else time.time()
        state = self.hl_states[norm_asset]
        state.mark_price = float(price)
        state.mid_price = float(mid_price if mid_price is not None else price)
        state.funding_rate = float(funding_rate)
        state.timestamp = now
        state.update_count += 1

        return self.evaluate_arbitrage(norm_asset, now=now)

    def update_zklighter_book(
        self,
        asset: str,
        best_bid: float,
        best_ask: float,
        best_bid_size: float = 1.0,
        best_ask_size: float = 1.0,
        timestamp: Optional[float] = None,
    ) -> Optional[CrossDexArbOpportunity]:
        """
        Updates zkLighter best bid/ask prices and immediately triggers arbitrage evaluation.
        """
        norm_asset = asset.upper()
        if norm_asset not in self.zkl_states:
            idx = self.market_configs.get(norm_asset, {}).get("market_index", len(self.zkl_states))
            self.zkl_states[norm_asset] = ZkLighterBookState(asset=norm_asset, market_index=idx)

        now = timestamp if timestamp is not None else time.time()
        book = self.zkl_states[norm_asset]
        book.update_book(
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            timestamp=now,
        )

        return self.evaluate_arbitrage(norm_asset, now=now)

    def evaluate_arbitrage(
        self,
        asset: str,
        now: Optional[float] = None,
    ) -> Optional[CrossDexArbOpportunity]:
        """
        Evaluates whether Hyperliquid leads zkLighter by >= min_spread_bps (25 bps).
        
        Returns `CrossDexArbOpportunity` if opportunity conditions are satisfied, else None.
        """
        norm_asset = asset.upper()
        self.total_evaluations += 1
        current_time = now if now is not None else time.time()

        hl_state = self.hl_states.get(norm_asset)
        zkl_state = self.zkl_states.get(norm_asset)

        if not hl_state or not zkl_state:
            return None

        # Price sanity validation
        hl_price = hl_state.mark_price
        if hl_price <= 0.0:
            return None

        best_bid = zkl_state.best_bid
        best_ask = zkl_state.best_ask
        zkl_mid = zkl_state.mid_price

        if zkl_mid <= 0.0 or best_bid <= 0.0 or best_ask == float("inf") or best_bid > best_ask:
            return None

        # Staleness filter
        hl_age = current_time - hl_state.timestamp
        zkl_age = current_time - zkl_state.timestamp
        if hl_age > self.max_staleness_sec or zkl_age > self.max_staleness_sec:
            logger.debug(f"[Arb] Stale feed for {norm_asset}: HL age={hl_age:.2f}s, zkL age={zkl_age:.2f}s")
            return None

        latency_lag_ms = max(0.0, (hl_state.timestamp - zkl_state.timestamp) * 1000.0)

        # Calculate spreads
        # Mid spread: (HL - zkL_mid) / zkL_mid
        raw_mid_spread = (hl_price - zkl_mid) / zkl_mid
        mid_spread_bps = raw_mid_spread * 10_000.0
        abs_mid_spread_bps = abs(mid_spread_bps)

        opportunity: Optional[CrossDexArbOpportunity] = None

        # Case 1: Hyperliquid leading UP -> Buy on zkLighter (Ask), hedge on HL
        # Condition: HL price is significantly above zkLighter ask or mid
        if mid_spread_bps >= self.min_spread_bps and hl_price > best_ask:
            exec_spread_bps = ((hl_price - best_ask) / best_ask) * 10_000.0
            net_edge_bps = exec_spread_bps - self.roundtrip_fee_bps
            
            # Sizing based on zkLighter ask depth and limits
            max_asset_size = self.max_notional_usd / max(best_ask, 1.0)
            target_size = min(zkl_state.best_ask_size, max_asset_size) if zkl_state.best_ask_size > 0 else (self.min_notional_usd / best_ask)
            notional = target_size * best_ask

            if notional >= self.min_notional_usd:
                est_profit = (net_edge_bps / 10_000.0) * notional
                confidence = min(1.0, max(0.5, (abs_mid_spread_bps / (self.min_spread_bps * 2.0))))
                urgency = "CRITICAL" if abs_mid_spread_bps >= 50.0 else ("HIGH" if abs_mid_spread_bps >= 35.0 else "MEDIUM")

                opportunity = CrossDexArbOpportunity(
                    asset=norm_asset,
                    direction=ArbDirection.BUY_ZKLIGHTER_SELL_HL,
                    hl_price=hl_price,
                    zklighter_mid=zkl_mid,
                    zklighter_best_bid=best_bid,
                    zklighter_best_ask=best_ask,
                    spread_pct=raw_mid_spread,
                    spread_bps=abs_mid_spread_bps,
                    executable_spread_bps=exec_spread_bps,
                    net_edge_bps=net_edge_bps,
                    target_size=round(target_size, 4),
                    estimated_profit_usd=round(est_profit, 2),
                    hl_timestamp=hl_state.timestamp,
                    zklighter_timestamp=zkl_state.timestamp,
                    latency_lag_ms=latency_lag_ms,
                    confidence_score=round(confidence, 2),
                    urgency=urgency,
                    timestamp=current_time,
                )

        # Case 2: Hyperliquid leading DOWN -> Sell on zkLighter (Bid), hedge on HL
        # Condition: HL price is significantly below zkLighter bid or mid
        elif (-mid_spread_bps) >= self.min_spread_bps and hl_price < best_bid:
            exec_spread_bps = ((best_bid - hl_price) / best_bid) * 10_000.0
            net_edge_bps = exec_spread_bps - self.roundtrip_fee_bps

            # Sizing based on zkLighter bid depth and limits
            max_asset_size = self.max_notional_usd / max(best_bid, 1.0)
            target_size = min(zkl_state.best_bid_size, max_asset_size) if zkl_state.best_bid_size > 0 else (self.min_notional_usd / best_bid)
            notional = target_size * best_bid

            if notional >= self.min_notional_usd:
                est_profit = (net_edge_bps / 10_000.0) * notional
                confidence = min(1.0, max(0.5, (abs_mid_spread_bps / (self.min_spread_bps * 2.0))))
                urgency = "CRITICAL" if abs_mid_spread_bps >= 50.0 else ("HIGH" if abs_mid_spread_bps >= 35.0 else "MEDIUM")

                opportunity = CrossDexArbOpportunity(
                    asset=norm_asset,
                    direction=ArbDirection.SELL_ZKLIGHTER_BUY_HL,
                    hl_price=hl_price,
                    zklighter_mid=zkl_mid,
                    zklighter_best_bid=best_bid,
                    zklighter_best_ask=best_ask,
                    spread_pct=raw_mid_spread,
                    spread_bps=abs_mid_spread_bps,
                    executable_spread_bps=exec_spread_bps,
                    net_edge_bps=net_edge_bps,
                    target_size=round(target_size, 4),
                    estimated_profit_usd=round(est_profit, 2),
                    hl_timestamp=hl_state.timestamp,
                    zklighter_timestamp=zkl_state.timestamp,
                    latency_lag_ms=latency_lag_ms,
                    confidence_score=round(confidence, 2),
                    urgency=urgency,
                    timestamp=current_time,
                )

        if opportunity:
            self._record_opportunity(opportunity)

        return opportunity

    def _record_opportunity(self, opportunity: CrossDexArbOpportunity) -> None:
        """Stores opportunity in memory, updates metrics, and dispatches callbacks."""
        self.total_opportunities_found += 1
        self.opportunities_by_asset[opportunity.asset] = (
            self.opportunities_by_asset.get(opportunity.asset, 0) + 1
        )
        self.recent_opportunities.append(opportunity)
        if len(self.recent_opportunities) > self.max_history_len:
            self.recent_opportunities.pop(0)

        logger.info(f"[CROSS-DEX ARB] Found Opportunity: {opportunity.summary()}")

        try:
            self.opportunity_queue.put_nowait(opportunity)
        except Exception:
            pass

        if self.on_opportunity:
            try:
                res = self.on_opportunity(opportunity)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                logger.error(f"[Arb] Error in opportunity callback: {e}")

    # =========================================================================
    # Live REST / WebSocket Ingestion Parsers
    # =========================================================================

    def parse_hyperliquid_all_mids(self, data: Any) -> List[Tuple[str, float]]:
        """
        Parses Hyperliquid 'allMids' response into list of (asset, mid_price).
        Data format: dict mapping symbol name to string price.
        """
        results: List[Tuple[str, float]] = []
        if not isinstance(data, dict):
            return results

        # Sometimes wrapped under {'data': {'mids': {...}}}
        mids_dict = data.get("data", {}).get("mids", data) if "data" in data else data
        if not isinstance(mids_dict, dict):
            mids_dict = data

        for asset in self.assets:
            if asset in mids_dict:
                try:
                    price = float(mids_dict[asset])
                    results.append((asset, price))
                except (ValueError, TypeError):
                    continue
        return results

    def parse_hyperliquid_meta_and_contexts(self, data: Any) -> List[Tuple[str, float, float]]:
        """
        Parses Hyperliquid 'metaAndAssetCtxs' response into (asset, mark_price, funding_rate).
        Data format: [ {universe: [{name: 'BTC', ...}]}, [ {markPx: '...', funding: '...'}, ... ] ]
        """
        results: List[Tuple[str, float, float]] = []
        if not isinstance(data, (list, tuple)) or len(data) < 2:
            return results

        universe_meta = data[0].get("universe", []) if isinstance(data[0], dict) else []
        asset_ctxs = data[1] if isinstance(data[1], list) else []

        for idx, asset_meta in enumerate(universe_meta):
            if idx >= len(asset_ctxs):
                break
            name = asset_meta.get("name", "").upper()
            if name in self.assets:
                ctx = asset_ctxs[idx]
                try:
                    mark_px = float(ctx.get("markPx", 0.0))
                    funding = float(ctx.get("funding", 0.0))
                    if mark_px > 0:
                        results.append((name, mark_px, funding))
                except (ValueError, TypeError):
                    continue
        return results

    def parse_zklighter_orderbook_msg(self, data: Any) -> Optional[Tuple[str, float, float, float, float]]:
        """
        Parses zkLighter WebSocket orderbook snapshot/delta message.
        Returns: (asset, best_bid, best_ask, best_bid_size, best_ask_size) or None.
        """
        if not isinstance(data, dict):
            return None

        market_id = data.get("market_id") or data.get("market_index")
        # Reverse lookup asset from market_index
        target_asset: Optional[str] = None
        for asset, config in self.market_configs.items():
            if config.get("market_index") == market_id:
                target_asset = asset
                break

        if not target_asset and "symbol" in data:
            sym = str(data["symbol"]).upper().replace("USD", "").replace("USDT", "").replace("-PERP", "")
            if sym in self.assets:
                target_asset = sym

        if not target_asset:
            return None

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        best_bid = 0.0
        best_bid_size = 0.0
        if bids and len(bids) > 0:
            top_bid = bids[0]
            if isinstance(top_bid, (list, tuple)) and len(top_bid) >= 2:
                best_bid = float(top_bid[0])
                best_bid_size = float(top_bid[1])
            elif isinstance(top_bid, dict):
                best_bid = float(top_bid.get("price", 0.0))
                best_bid_size = float(top_bid.get("size", 0.0))

        best_ask = float("inf")
        best_ask_size = 0.0
        if asks and len(asks) > 0:
            top_ask = asks[0]
            if isinstance(top_ask, (list, tuple)) and len(top_ask) >= 2:
                best_ask = float(top_ask[0])
                best_ask_size = float(top_ask[1])
            elif isinstance(top_ask, dict):
                best_ask = float(top_ask.get("price", float("inf")))
                best_ask_size = float(top_ask.get("size", 0.0))

        return (target_asset, best_bid, best_ask, best_bid_size, best_ask_size)

    # =========================================================================
    # Live WebSocket Listeners
    # =========================================================================

    async def _hyperliquid_ws_loop(self) -> None:
        """Connects to Hyperliquid WS and listens to allMids channel."""
        while self.is_running:
            try:
                session = await self._get_session()
                async with session.ws_connect(HYPERLIQUID_WS_URL, heartbeat=20.0) as ws:
                    logger.info("[Arb] Connected to Hyperliquid WebSocket feed")
                    sub_msg = {"method": "subscribe", "subscription": {"type": "allMids"}}
                    await ws.send_str(json.dumps(sub_msg))

                    async for msg in ws:
                        if not self.is_running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                                if payload.get("channel") == "allMids":
                                    mids = self.parse_hyperliquid_all_mids(payload.get("data", {}))
                                    now = time.time()
                                    for asset, price in mids:
                                        self.update_hyperliquid_price(asset, price, timestamp=now)
                            except Exception as e:
                                logger.debug(f"[Arb] HL WS parse error: {e}")
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Arb] Hyperliquid WS disconnected: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3.0)

    async def _hyperliquid_rest_poll_loop(self, interval_sec: float = 1.0) -> None:
        """Polls Hyperliquid REST info endpoint as fallback / redundant high-accuracy stream."""
        while self.is_running:
            try:
                session = await self._get_session()
                payload = {"type": "metaAndAssetCtxs"}
                async with session.post(HYPERLIQUID_API_URL, json=payload, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = self.parse_hyperliquid_meta_and_contexts(data)
                        now = time.time()
                        for asset, mark_px, funding in items:
                            self.update_hyperliquid_price(asset, mark_px, funding_rate=funding, timestamp=now)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[Arb] HL REST poll exception: {e}")
            await asyncio.sleep(interval_sec)

    async def start(self, start_ws: bool = True, start_rest_poll: bool = True) -> None:
        """Starts live price ingestion loops."""
        if self.is_running:
            return
        self.is_running = True
        logger.info(f"[Arb] Starting Cross-DEX Arbitrage Engine for {self.assets} (Threshold: {self.min_spread_bps} bps)")

        if start_ws:
            self._background_tasks.append(asyncio.create_task(self._hyperliquid_ws_loop()))
        if start_rest_poll:
            self._background_tasks.append(asyncio.create_task(self._hyperliquid_rest_poll_loop()))

    async def stop(self) -> None:
        """Gracefully shuts down all streaming tasks and sessions."""
        self.is_running = False
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("[Arb] Cross-DEX Arbitrage Engine stopped")

    def get_metrics(self) -> Dict[str, Any]:
        """Returns comprehensive diagnostic and performance metrics."""
        return {
            "total_evaluations": self.total_evaluations,
            "total_opportunities_found": self.total_opportunities_found,
            "opportunities_by_asset": dict(self.opportunities_by_asset),
            "min_spread_bps": self.min_spread_bps,
            "max_staleness_sec": self.max_staleness_sec,
            "roundtrip_fee_bps": self.roundtrip_fee_bps,
            "tracked_assets": self.assets,
            "recent_opportunities_count": len(self.recent_opportunities),
            "latest_opportunity": self.recent_opportunities[-1].summary() if self.recent_opportunities else None,
        }
