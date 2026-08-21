#!/usr/bin/env python3
"""
HFT Fake Wall & Orderbook Spoofing Detector (spoofing_detector.py)
=================================================================
Tracks the lifespan of large resting limit orders (> $25k) in the L2/L3 book.
Identifies flickering and rapid cancellations (< 100ms lifespan) placed by
predatory algorithms to manipulate sentiment, preventing anchoring to fake walls.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("SpoofingDetector")


@dataclass
class RestingOrderTracker:
    """Tracks a single resting order's placement and lifespan."""
    order_id: str
    symbol: str
    side: str
    price: float
    size: float
    notional_usd: float
    placed_at: float = field(default_factory=time.time)
    cancelled_at: Optional[float] = None
    lifespan_ms: float = 0.0
    is_spoof: bool = False


@dataclass
class SpoofingMetrics:
    """Consolidated spoofing metrics for a market."""
    symbol: str
    spoof_ratio_pct: float             # % of large volume that was cancelled in < 100ms
    is_manipulated_book: bool          # Spoof ratio >= 30%
    verified_genuine_walls: List[Tuple[float, float, str]]  # Filtered genuine walls (price, usd, side)
    timestamp: float = field(default_factory=time.time)


class HFTSpoofingDetector:
    """
    Detects flash cancellations and predatory spoofing in the orderbook.
    """

    def __init__(
        self,
        min_wall_usd: float = 20000.0,          # Track orders >= $20k
        spoof_lifespan_threshold_ms: float = 120.0, # Cancelled in < 120ms = Spoof
        manipulation_threshold_pct: float = 30.0,
    ):
        self.min_wall_usd = min_wall_usd
        self.spoof_lifespan_threshold_ms = spoof_lifespan_threshold_ms
        self.manipulation_threshold_pct = manipulation_threshold_pct

        # Symbol -> order_id -> RestingOrderTracker
        self._active_orders: Dict[str, Dict[str, RestingOrderTracker]] = {}
        self._cancelled_history: Dict[str, List[RestingOrderTracker]] = {}

    def record_order_placement(
        self,
        symbol: str,
        order_id: str,
        side: str,
        price: float,
        size: float,
        timestamp_ms: Optional[float] = None,
    ) -> None:
        """Records an incoming large resting limit order."""
        sym = symbol.upper()
        notional_usd = price * size
        if notional_usd < self.min_wall_usd:
            return  # Skip small retail noise

        if sym not in self._active_orders:
            self._active_orders[sym] = {}
            self._cancelled_history[sym] = []

        now = (timestamp_ms / 1000.0) if timestamp_ms else time.time()
        tracker = RestingOrderTracker(
            order_id=order_id,
            symbol=sym,
            side=side,
            price=price,
            size=size,
            notional_usd=notional_usd,
            placed_at=now,
        )
        self._active_orders[sym][order_id] = tracker

    def record_order_cancellation(
        self,
        symbol: str,
        order_id: str,
        timestamp_ms: Optional[float] = None,
    ) -> Optional[RestingOrderTracker]:
        """Records order cancellation and measures lifespan."""
        sym = symbol.upper()
        tracker = self._active_orders.get(sym, {}).pop(order_id, None)
        if not tracker:
            return None

        now = (timestamp_ms / 1000.0) if timestamp_ms else time.time()
        tracker.cancelled_at = now
        tracker.lifespan_ms = (now - tracker.placed_at) * 1000.0
        tracker.is_spoof = tracker.lifespan_ms < self.spoof_lifespan_threshold_ms

        self._cancelled_history[sym].append(tracker)
        if tracker.is_spoof:
            logger.warning("🔍 [Spoof Alert] %s Spoof Wall Detected! $%.2f %s @ $%.2f cancelled in %.1fms", sym, tracker.notional_usd, tracker.side, tracker.price, tracker.lifespan_ms)

        return tracker

    def evaluate_genuine_liquidity(
        self,
        symbol: str,
        raw_walls: List[Tuple[float, float, str]],  # (price, notional_usd, side)
    ) -> SpoofingMetrics:
        """
        Filters out spoofed walls and returns only genuine resting liquidity.
        """
        sym = symbol.upper()
        history = self._cancelled_history.get(sym, [])[-50:]

        spoofs = [t for t in history if t.is_spoof]
        spoof_ratio = (len(spoofs) / len(history) * 100.0) if history else 0.0

        # Filter genuine walls (walls that haven't been rapidly flickered)
        spoof_prices = {round(t.price, 2) for t in spoofs}
        genuine_walls = [
            w for w in raw_walls
            if round(w[0], 2) not in spoof_prices
        ]

        return SpoofingMetrics(
            symbol=sym,
            spoof_ratio_pct=round(spoof_ratio, 2),
            is_manipulated_book=spoof_ratio >= self.manipulation_threshold_pct,
            verified_genuine_walls=genuine_walls,
        )
