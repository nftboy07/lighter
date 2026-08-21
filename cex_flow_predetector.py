#!/usr/bin/env python3
"""
CEX Order Flow Early-Spike Pre-Detector (cex_flow_predetector.py)
================================================================
Monitors live Binance and Coinbase aggregated trade streams, detecting massive volume sweeps
(>$500k in <50ms) to provide a 100-300ms advance lead before public news headlines.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("CEXFlowPreDetector")


@dataclass(frozen=True)
class VolumeSweepAlert:
    """Detected CEX volume sweep indicating impending news or breakout."""
    symbol: str
    exchange: str
    direction: str  # "BUY_SPIKE" or "SELL_SPIKE"
    volume_usd: float
    duration_ms: float
    velocity_usd_per_sec: float
    price_change_pct: float
    confidence_score: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class TradeTick:
    """Aggregated trade tick from CEX."""
    timestamp_ms: float
    price: float
    quantity: float
    is_buyer_maker: bool  # True = Sell market order, False = Buy market order


class CEXFlowPreDetector:
    """
    Ingests high-frequency trades from Binance/Coinbase and emits early lead signals.
    """

    def __init__(
        self,
        min_sweep_volume_usd: float = 250000.0,  # $250k sweep in window
        sweep_window_ms: float = 50.0,           # 50ms window
        min_price_move_pct: float = 0.10,        # 0.10% move in window
    ):
        self.min_sweep_volume_usd = min_sweep_volume_usd
        self.sweep_window_ms = sweep_window_ms
        self.min_price_move_pct = min_price_move_pct

        # Ticks buffers: symbol -> deque of TradeTick
        self._trade_windows: Dict[str, deque] = {}

    def on_trade_tick(
        self,
        symbol: str,
        price: float,
        quantity: float,
        is_buyer_maker: bool,
        exchange: str = "Binance",
        timestamp_ms: Optional[float] = None,
    ) -> Optional[VolumeSweepAlert]:
        """
        Processes a live trade tick and detects if a volume sweep occurred.
        """
        now_ms = timestamp_ms if timestamp_ms is not None else (time.time() * 1000.0)
        sym = symbol.upper()

        if sym not in self._trade_windows:
            self._trade_windows[sym] = deque(maxlen=500)

        window = self._trade_windows[sym]
        window.append(TradeTick(
            timestamp_ms=now_ms,
            price=price,
            quantity=quantity,
            is_buyer_maker=is_buyer_maker,
        ))

        # Filter ticks within sweep window
        cutoff_ms = now_ms - self.sweep_window_ms
        recent_ticks = [t for t in window if t.timestamp_ms >= cutoff_ms]

        if len(recent_ticks) < 2:
            return None

        buy_vol = sum(t.price * t.quantity for t in recent_ticks if not t.is_buyer_maker)
        sell_vol = sum(t.price * t.quantity for t in recent_ticks if t.is_buyer_maker)
        net_vol = buy_vol - sell_vol

        first_p = recent_ticks[0].price
        last_p = recent_ticks[-1].price
        price_change_pct = ((last_p - first_p) / first_p) * 100.0
        duration_ms = max(1.0, recent_ticks[-1].timestamp_ms - recent_ticks[0].timestamp_ms)
        velocity = (abs(net_vol) / duration_ms) * 1000.0

        if buy_vol >= self.min_sweep_volume_usd and price_change_pct >= self.min_price_move_pct:
            alert = VolumeSweepAlert(
                symbol=sym,
                exchange=exchange,
                direction="BUY_SPIKE",
                volume_usd=round(buy_vol, 2),
                duration_ms=round(duration_ms, 2),
                velocity_usd_per_sec=round(velocity, 2),
                price_change_pct=round(price_change_pct, 3),
                confidence_score=min(0.99, 0.70 + (buy_vol / 1000000.0) * 0.25),
            )
            logger.warning("🚨 [CEXFlowPreDetector] Aggressive BUY SWEEP on %s (%s): $%.0f in %.1fms (+%.2f%%)", sym, exchange, buy_vol, duration_ms, price_change_pct)
            return alert

        elif sell_vol >= self.min_sweep_volume_usd and price_change_pct <= -self.min_price_move_pct:
            alert = VolumeSweepAlert(
                symbol=sym,
                exchange=exchange,
                direction="SELL_SPIKE",
                volume_usd=round(sell_vol, 2),
                duration_ms=round(duration_ms, 2),
                velocity_usd_per_sec=round(velocity, 2),
                price_change_pct=round(price_change_pct, 3),
                confidence_score=min(0.99, 0.70 + (sell_vol / 1000000.0) * 0.25),
            )
            logger.warning("🚨 [CEXFlowPreDetector] Aggressive SELL SWEEP on %s (%s): $%.0f in %.1fms (%.2f%%)", sym, exchange, sell_vol, duration_ms, price_change_pct)
            return alert

        return None
