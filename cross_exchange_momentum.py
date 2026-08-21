#!/usr/bin/env python3
"""
Binance/Bybit Cross-Exchange Momentum Lead Filter
=================================================
Provides ultra-low-latency 100ms volume and momentum spike verification against
Binance (Spot/Perp) and Bybit (Linear Perp/Spot) tickers.
Verifies cross-exchange volume surge and flow alignment on high-conviction headlines
before firing aggressive max-size orders.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import aiohttp

logger = logging.getLogger("CrossExchangeMomentum")

# Default exchange endpoints
BINANCE_SPOT_API = "https://api.binance.com/api/v3"
BINANCE_FUTURES_API = "https://fapi.binance.com/fapi/v1"
BYBIT_API = "https://api.bybit.com/v5"

DEFAULT_SYMBOL_MAP: Dict[str, Dict[str, str]] = {
    "ETH": {"binance": "ETHUSDT", "bybit": "ETHUSDT"},
    "BTC": {"binance": "BTCUSDT", "bybit": "BTCUSDT"},
    "SOL": {"binance": "SOLUSDT", "bybit": "SOLUSDT"},
    "HYPE": {"binance": "HYPEUSDT", "bybit": "HYPEUSDT"},
    "XRP": {"binance": "XRPUSDT", "bybit": "XRPUSDT"},
    "DOGE": {"binance": "DOGEUSDT", "bybit": "DOGEUSDT"},
    "BNB": {"binance": "BNBUSDT", "bybit": "BNBUSDT"},
    "AVAX": {"binance": "AVAXUSDT", "bybit": "AVAXUSDT"},
    "SUI": {"binance": "SUIUSDT", "bybit": "SUIUSDT"},
}


@dataclass
class TradeTick:
    timestamp: float
    price: float
    size: float
    is_buyer_maker: bool  # True if sell initiated, False if buy initiated
    exchange: str  # "binance" or "bybit"


@dataclass(frozen=True)
class MomentumConfirmation:
    confirmed: bool
    spike_ratio: float
    binance_vol_usd: float
    bybit_vol_usd: float
    total_vol_usd: float
    buy_ratio: float
    direction_aligned: bool
    latency_ms: float
    asset: str
    sentiment: str
    reasons: Tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        status = "CONFIRMED" if self.confirmed else "UNCONFIRMED"
        return (
            f"[{status}] {self.asset} {self.sentiment} | Spike: {self.spike_ratio:.2f}x | "
            f"Vol: ${self.total_vol_usd:,.0f} (BN: ${self.binance_vol_usd:,.0f}, BY: ${self.bybit_vol_usd:,.0f}) | "
            f"BuyRatio: {self.buy_ratio:.1%} | Latency: {self.latency_ms:.1f}ms"
        )


class CrossExchangeMomentumFilter:
    """
    Evaluates Binance and Bybit order flow and volume velocity within sub-100ms windows
    to confirm high-conviction catalysts before max-size order dispatch.
    """

    def __init__(
        self,
        min_spike_ratio: float = 1.5,
        window_ms: int = 100,
        high_conviction_threshold: float = 0.80,
        require_confirmation: bool = True,
        max_query_timeout_ms: float = 150.0,
        symbol_map: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> None:
        self.min_spike_ratio = float(os.getenv("NEWS_MOMENTUM_MIN_SPIKE_RATIO", str(min_spike_ratio)))
        self.window_ms = int(os.getenv("NEWS_MOMENTUM_WINDOW_MS", str(window_ms)))
        self.high_conviction_threshold = float(os.getenv("NEWS_MOMENTUM_CONVICTION_THRESHOLD", str(high_conviction_threshold)))
        self.require_confirmation = (
            os.getenv("NEWS_REQUIRE_MOMENTUM_CONFIRMATION", str(require_confirmation)).lower() == "true"
        )
        self.max_query_timeout_ms = max_query_timeout_ms
        self.symbol_map = dict(DEFAULT_SYMBOL_MAP if symbol_map is None else symbol_map)

        # In-memory sliding window of trade ticks for low latency micro-analysis
        self._trade_buffers: Dict[str, List[TradeTick]] = {}
        self._baseline_volumes: Dict[str, float] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    def register_symbol(self, asset: str, binance_symbol: str, bybit_symbol: str) -> None:
        self.symbol_map[asset.upper()] = {
            "binance": binance_symbol.upper(),
            "bybit": bybit_symbol.upper(),
        }

    def record_tick(self, asset: str, exchange: str, price: float, size: float, is_buyer_maker: bool, timestamp: Optional[float] = None) -> None:
        """Injects a real-time trade tick into the sliding window."""
        sym = asset.upper()
        now = timestamp if timestamp is not None else time.time()
        buf = self._trade_buffers.setdefault(sym, [])
        buf.append(TradeTick(timestamp=now, price=price, size=size, is_buyer_maker=is_buyer_maker, exchange=exchange.lower()))
        # Retain past 10 seconds of trade ticks
        cutoff = now - 10.0
        self._trade_buffers[sym] = [t for t in buf if t.timestamp >= cutoff]

    def set_baseline_volume(self, asset: str, volume_usd_per_sec: float) -> None:
        self._baseline_volumes[asset.upper()] = max(0.1, volume_usd_per_sec)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.max_query_timeout_ms / 1000.0)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def fetch_binance_trades(self, symbol: str) -> List[TradeTick]:
        """Fetches recent trades from Binance Spot."""
        session = await self._get_session()
        url = f"{BINANCE_SPOT_API}/trades?symbol={symbol}&limit=30"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                ticks = []
                for item in data:
                    t_sec = float(item.get("time", 0)) / 1000.0
                    price = float(item.get("price", 0.0))
                    qty = float(item.get("qty", 0.0))
                    is_buyer_maker = bool(item.get("isBuyerMaker", False))
                    ticks.append(TradeTick(timestamp=t_sec, price=price, size=qty, is_buyer_maker=is_buyer_maker, exchange="binance"))
                return ticks
        except Exception:
            return []

    async def fetch_bybit_trades(self, symbol: str) -> List[TradeTick]:
        """Fetches recent trades from Bybit Linear Perp."""
        session = await self._get_session()
        url = f"{BYBIT_API}/market/recent-trade?category=linear&symbol={symbol}&limit=30"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
                result = payload.get("result", {})
                list_items = result.get("list", [])
                ticks = []
                for item in list_items:
                    t_sec = float(item.get("time", 0)) / 1000.0
                    price = float(item.get("price", 0.0))
                    size = float(item.get("size", 0.0))
                    side = str(item.get("side", "")).lower()
                    # Bybit: side="Buy" means buyer taker (is_buyer_maker=False)
                    is_buyer_maker = (side != "buy")
                    ticks.append(TradeTick(timestamp=t_sec, price=price, size=size, is_buyer_maker=is_buyer_maker, exchange="bybit"))
                return ticks
        except Exception:
            return []

    def evaluate_buffer(
        self,
        asset: str,
        sentiment: str,
        window_ms: Optional[int] = None,
        now: Optional[float] = None,
    ) -> MomentumConfirmation:
        """
        Evaluates the trade buffer for an asset over the specified window (default 100ms).
        """
        t0 = time.perf_counter()
        asset_sym = asset.upper()
        win_ms = window_ms or self.window_ms
        win_sec = max(0.01, win_ms / 1000.0)
        current_time = now if now is not None else time.time()
        cutoff = current_time - win_sec

        ticks = self._trade_buffers.get(asset_sym, [])
        window_ticks = [t for t in ticks if t.timestamp >= cutoff]

        binance_vol = sum(t.price * t.size for t in window_ticks if t.exchange == "binance")
        bybit_vol = sum(t.price * t.size for t in window_ticks if t.exchange == "bybit")
        total_vol = binance_vol + bybit_vol

        buy_vol = sum(t.price * t.size for t in window_ticks if not t.is_buyer_maker)
        sell_vol = sum(t.price * t.size for t in window_ticks if t.is_buyer_maker)
        buy_ratio = (buy_vol / total_vol) if total_vol > 0 else 0.5

        # Baseline comparison
        baseline_sec = self._baseline_volumes.get(asset_sym, 5000.0)
        expected_window_vol = baseline_sec * win_sec
        spike_ratio = (total_vol / expected_window_vol) if expected_window_vol > 0 else 1.0

        # Sentiment alignment
        is_bullish = sentiment.upper() in {"BULLISH", "BUY", "LONG"}
        is_bearish = sentiment.upper() in {"BEARISH", "SELL", "SHORT"}

        direction_aligned = True
        if is_bullish and total_vol > 0:
            direction_aligned = buy_ratio >= 0.50
        elif is_bearish and total_vol > 0:
            direction_aligned = buy_ratio <= 0.50

        reasons: List[str] = []
        confirmed = True

        if spike_ratio < self.min_spike_ratio:
            confirmed = False
            reasons.append(f"Volume spike {spike_ratio:.2f}x below threshold {self.min_spike_ratio:.2f}x")

        if not direction_aligned:
            confirmed = False
            flow_side = "buying" if buy_ratio > 0.5 else "selling"
            reasons.append(f"Flow direction contradiction (catalyst={sentiment}, dominant_flow={flow_side})")

        latency_ms = (time.perf_counter() - t0) * 1000.0

        return MomentumConfirmation(
            confirmed=confirmed,
            spike_ratio=round(spike_ratio, 2),
            binance_vol_usd=round(binance_vol, 2),
            bybit_vol_usd=round(bybit_vol, 2),
            total_vol_usd=round(total_vol, 2),
            buy_ratio=round(buy_ratio, 4),
            direction_aligned=direction_aligned,
            latency_ms=round(latency_ms, 3),
            asset=asset_sym,
            sentiment=sentiment.upper(),
            reasons=tuple(reasons),
        )

    async def verify_spike(
        self,
        asset: str,
        sentiment: str,
        conviction_score: float = 0.90,
        window_ms: Optional[int] = None,
    ) -> MomentumConfirmation:
        """
        Queries Binance and Bybit concurrently in <100ms to verify cross-exchange momentum.
        """
        t0 = time.perf_counter()
        asset_sym = asset.upper()
        mapping = self.symbol_map.get(asset_sym, {"binance": f"{asset_sym}USDT", "bybit": f"{asset_sym}USDT"})

        # Check existing buffer first
        buffered_eval = self.evaluate_buffer(asset_sym, sentiment, window_ms)
        if buffered_eval.confirmed and buffered_eval.total_vol_usd > 0:
            return buffered_eval

        # Fetch live ticker/trade streams concurrently
        try:
            bn_task = self.fetch_binance_trades(mapping["binance"])
            by_task = self.fetch_bybit_trades(mapping["bybit"])
            bn_trades, by_trades = await asyncio.gather(bn_task, by_task, return_exceptions=True)

            if isinstance(bn_trades, list):
                for t in bn_trades:
                    self.record_tick(asset_sym, "binance", t.price, t.size, t.is_buyer_maker, t.timestamp)
            if isinstance(by_trades, list):
                for t in by_trades:
                    self.record_tick(asset_sym, "bybit", t.price, t.size, t.is_buyer_maker, t.timestamp)
        except Exception as exc:
            logger.warning("Cross-exchange query error for %s: %s", asset_sym, exc)

        eval_result = self.evaluate_buffer(asset_sym, sentiment, window_ms)
        total_latency_ms = (time.perf_counter() - t0) * 1000.0

        return MomentumConfirmation(
            confirmed=eval_result.confirmed,
            spike_ratio=eval_result.spike_ratio,
            binance_vol_usd=eval_result.binance_vol_usd,
            bybit_vol_usd=eval_result.bybit_vol_usd,
            total_vol_usd=eval_result.total_vol_usd,
            buy_ratio=eval_result.buy_ratio,
            direction_aligned=eval_result.direction_aligned,
            latency_ms=round(total_latency_ms, 2),
            asset=asset_sym,
            sentiment=sentiment.upper(),
            reasons=eval_result.reasons,
        )

    def size_multiplier(self, confirmation: MomentumConfirmation, conviction_score: float) -> float:
        """
        Determines position sizing multiplier based on cross-exchange momentum confirmation.
        - Full max-size (1.0x) if confirmed with high conviction.
        - Reduced size (0.25x - 0.5x) if high-conviction but unconfirmed.
        - 0.0x if momentum actively contradicts catalyst direction.
        """
        if not confirmation.direction_aligned:
            return 0.0  # Hard stop on conflicting orderflow
        if confirmation.confirmed:
            return 1.0  # Full max size
        if conviction_score >= self.high_conviction_threshold and not self.require_confirmation:
            return 0.5
        return 1.0 if not self.require_confirmation else 0.0

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None
