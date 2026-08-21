#!/usr/bin/env python3
"""
Dynamic Volatility Grid MM & Points Maximizer (dynamic_grid_mm.py)
=================================================================
Automated multi-layer geometric grid quoting engine optimized for sideways markets,
maximizing 0-fee maker volume & Robinhood points while preventing adverse trending inventory.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("DynamicGridMM")


@dataclass(frozen=True)
class GridLevel:
    """Individual price level in the grid."""
    level_index: int
    side: str  # "BUY" or "SELL"
    price: float
    size: float
    notional_usd: float
    distance_pct: float


@dataclass
class GridState:
    """Current active grid structure."""
    symbol: str
    mid_price: float
    grid_spacing_pct: float
    num_layers: int
    buy_levels: List[GridLevel]
    sell_levels: List[GridLevel]
    total_bid_notional: float
    total_ask_notional: float
    inventory_skew_pct: float = 0.0
    updated_at: float = field(default_factory=time.time)


class DynamicGridMMEngine:
    """
    Constructs and shifts dynamic geometric quoting grids based on market volatility and inventory.
    """

    def __init__(
        self,
        base_layer_size_usd: float = 25.0,
        num_layers: int = 5,
        base_grid_spacing_pct: float = 0.15,  # 0.15% (15 bps) spacing
        max_inventory_skew_pct: float = 50.0,
    ):
        self.base_layer_size_usd = base_layer_size_usd
        self.num_layers = num_layers
        self.base_grid_spacing_pct = base_grid_spacing_pct
        self.max_inventory_skew_pct = max_inventory_skew_pct

    def generate_grid(
        self,
        symbol: str,
        mid_price: float,
        atr_multiplier: float = 1.0,
        current_inventory_usd: float = 0.0,
        max_position_usd: float = 500.0,
    ) -> GridState:
        """
        Calculates optimal geometric grid levels adjusted for ATR volatility and inventory skew.
        """
        if mid_price <= 0:
            raise ValueError("Mid price must be positive")

        # 1. Adjust grid spacing by volatility (higher ATR -> wider spacing)
        effective_spacing_pct = self.base_grid_spacing_pct * max(0.8, min(3.0, atr_multiplier))

        # 2. Calculate inventory skew (-100% to +100%)
        skew_ratio = 0.0
        if max_position_usd > 0:
            skew_ratio = max(-1.0, min(1.0, current_inventory_usd / max_position_usd))
        inventory_skew_pct = skew_ratio * self.max_inventory_skew_pct

        buy_levels: List[GridLevel] = []
        sell_levels: List[GridLevel] = []

        total_bid_notional = 0.0
        total_ask_notional = 0.0

        for i in range(1, self.num_layers + 1):
            # Distance from mid
            distance_pct = i * effective_spacing_pct

            # Buy side (skew downsizes bid size if long heavy)
            buy_price = mid_price * (1.0 - distance_pct / 100.0)
            bid_size_factor = max(0.2, 1.0 - max(0.0, skew_ratio))
            bid_notional = self.base_layer_size_usd * bid_size_factor
            bid_size = bid_notional / max(0.0001, buy_price)

            buy_levels.append(GridLevel(
                level_index=i,
                side="BUY",
                price=round(buy_price, 4),
                size=round(bid_size, 6),
                notional_usd=round(bid_notional, 2),
                distance_pct=round(distance_pct, 3),
            ))
            total_bid_notional += bid_notional

            # Sell side (skew downsizes ask size if short heavy)
            sell_price = mid_price * (1.0 + distance_pct / 100.0)
            ask_size_factor = max(0.2, 1.0 + min(0.0, skew_ratio))
            ask_notional = self.base_layer_size_usd * ask_size_factor
            ask_size = ask_notional / max(0.0001, sell_price)

            sell_levels.append(GridLevel(
                level_index=i,
                side="SELL",
                price=round(sell_price, 4),
                size=round(ask_size, 6),
                notional_usd=round(ask_notional, 2),
                distance_pct=round(distance_pct, 3),
            ))
            total_ask_notional += ask_notional

        return GridState(
            symbol=symbol.upper(),
            mid_price=mid_price,
            grid_spacing_pct=round(effective_spacing_pct, 3),
            num_layers=self.num_layers,
            buy_levels=buy_levels,
            sell_levels=sell_levels,
            total_bid_notional=round(total_bid_notional, 2),
            total_ask_notional=round(total_ask_notional, 2),
            inventory_skew_pct=round(inventory_skew_pct, 2),
        )
