#!/usr/bin/env python3
"""
Cross-DEX Smart Order Router (SOR) (smart_order_router.py)
=========================================================
Splits large orders across zkLighter, Hyperliquid, and Binance to minimize
market impact and achieve near-zero slippage.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("SmartOrderRouter")


@dataclass
class RouteSlice:
    """Individual venue slice for an execution route."""
    exchange: str
    symbol: str
    side: str
    slice_size: float
    slice_notional_usd: float
    expected_price: float
    expected_slippage_bps: float


@dataclass
class RoutedOrderPlan:
    """Complete aggregated multi-exchange execution plan."""
    order_id: str
    symbol: str
    side: str
    total_size: float
    total_notional_usd: float
    slices: List[RouteSlice]
    weighted_avg_price: float
    avg_slippage_bps: float
    timestamp: float = field(default_factory=time.time)


class CrossDEXSmartOrderRouter:
    """
    Splits orders proportionally according to real-time depth across venues.
    """

    def __init__(
        self,
        venues: Optional[List[str]] = None,
        max_slippage_bps_cap: float = 35.0,
    ):
        self.venues = venues or ["zkLighter", "Hyperliquid", "Binance"]
        self.max_slippage_bps_cap = max_slippage_bps_cap

    def route_order(
        self,
        symbol: str,
        side: str,
        total_notional_usd: float,
        venue_depths: Dict[str, Dict[str, float]],  # venue -> {"best_price": float, "available_usd": float}
    ) -> RoutedOrderPlan:
        """
        Calculates optimal allocation across available venues.
        """
        order_id = f"sor_{symbol}_{int(time.time()*1000)}"
        slices: List[RouteSlice] = []

        total_available = sum(v.get("available_usd", 0.0) for v in venue_depths.values())
        if total_available <= 0:
            # Fallback equal allocation
            weight = 1.0 / len(venue_depths) if venue_depths else 1.0
            for ven, data in venue_depths.items():
                usd = total_notional_usd * weight
                px = data.get("best_price", 1.0)
                qty = usd / px if px > 0 else 0.0
                slices.append(
                    RouteSlice(
                        exchange=ven,
                        symbol=symbol,
                        side=side,
                        slice_size=round(qty, 6),
                        slice_notional_usd=round(usd, 2),
                        expected_price=px,
                        expected_slippage_bps=5.0,
                    )
                )
        else:
            # Proportional depth routing
            for ven, data in venue_depths.items():
                avail = data.get("available_usd", 0.0)
                share = avail / total_available
                usd = total_notional_usd * share
                px = data.get("best_price", 1.0)
                qty = usd / px if px > 0 else 0.0
                slices.append(
                    RouteSlice(
                        exchange=ven,
                        symbol=symbol,
                        side=side,
                        slice_size=round(qty, 6),
                        slice_notional_usd=round(usd, 2),
                        expected_price=px,
                        expected_slippage_bps=round(min(self.max_slippage_bps_cap, (usd / max(1.0, avail)) * 10.0), 1),
                    )
                )

        total_routed_usd = sum(s.slice_notional_usd for s in slices)
        weighted_px = sum(s.expected_price * s.slice_notional_usd for s in slices) / max(1.0, total_routed_usd)
        avg_slip = sum(s.expected_slippage_bps * s.slice_notional_usd for s in slices) / max(1.0, total_routed_usd)
        total_size = sum(s.slice_size for s in slices)

        plan = RoutedOrderPlan(
            order_id=order_id,
            symbol=symbol,
            side=side,
            total_size=round(total_size, 6),
            total_notional_usd=round(total_routed_usd, 2),
            slices=slices,
            weighted_avg_price=round(weighted_px, 4),
            avg_slippage_bps=round(avg_slip, 2),
        )

        logger.info("⚡ [SOR Router] Routed $%.2f %s (%s) across %d venues @ $%.2f (Slippage: %.1fbps)", total_routed_usd, symbol, side, len(slices), weighted_px, avg_slip)
        return plan
