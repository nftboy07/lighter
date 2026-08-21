#!/usr/bin/env python3
"""
zkLighter On-Chain Liquidation Cascade Hunter (liquidation_hunter.py)
====================================================================
Monitors zkLighter clearinghouse liquidation stream and forced orderbook fills,
sniping distressed liquidations at discount margins ahead of public market makers.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("LiquidationHunter")


class LiquidationSide(str, Enum):
    LONG_LIQUIDATED = "LONG_LIQUIDATED"    # Long forced to sell -> buy opportunity at discount
    SHORT_LIQUIDATED = "SHORT_LIQUIDATED"  # Short forced to buy -> sell opportunity at premium


@dataclass(frozen=True)
class LiquidationEvent:
    """Detected clearinghouse liquidation."""
    event_id: str
    symbol: str
    side: LiquidationSide
    bankruptcy_price: float
    mark_price: float
    size_base: float
    notional_usd: float
    discount_pct: float
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class LiquidationSnipeOrder:
    """Calculated snipe order to capture liquidation discount."""
    event_id: str
    symbol: str
    action: str  # "BUY" or "SELL"
    snipe_price: float
    target_exit_price: float
    size_base: float
    notional_usd: float
    expected_profit_usd: float
    discount_bps: float
    timestamp: float = field(default_factory=time.time)


class LiquidationHunterEngine:
    """
    Clearinghouse event processor that identifies profitable liquidation discounts
    and generates immediate snipe & exit order pairs.
    """

    def __init__(
        self,
        min_notional_usd: float = 100.0,
        min_discount_bps: float = 25.0,  # 0.25% minimum discount to trigger snipe
        target_profit_bps: float = 50.0,  # 0.50% profit target for immediate flip
        max_position_size_usd: float = 1000.0,
    ):
        self.min_notional_usd = min_notional_usd
        self.min_discount_bps = min_discount_bps
        self.target_profit_bps = target_profit_bps
        self.max_position_size_usd = max_position_size_usd

        self.recent_liquidations: List[LiquidationEvent] = []
        self.executed_snipes: List[LiquidationSnipeOrder] = []

    def evaluate_liquidation(
        self,
        event_id: str,
        symbol: str,
        side: LiquidationSide,
        bankruptcy_price: float,
        mark_price: float,
        size_base: float,
    ) -> Optional[LiquidationSnipeOrder]:
        """
        Evaluates a liquidation event and generates a snipe order if discount exceeds threshold.
        """
        if mark_price <= 0 or bankruptcy_price <= 0 or size_base <= 0:
            return None

        notional = mark_price * size_base
        if notional < self.min_notional_usd:
            return None

        snipe_notional = min(notional, self.max_position_size_usd)
        snipe_size = snipe_notional / mark_price

        # Case 1: Long Liquidation (forced sell dumped below market -> we BUY at discount)
        if side == LiquidationSide.LONG_LIQUIDATED:
            discount_pct = max(0.0, (mark_price - bankruptcy_price) / mark_price * 100.0)
            discount_bps = discount_pct * 100.0
            if discount_bps >= self.min_discount_bps:
                snipe_price = bankruptcy_price
                target_exit = mark_price * (1.0 + (self.target_profit_bps / 10000.0))
                expected_profit = (target_exit - snipe_price) * snipe_size
                order = LiquidationSnipeOrder(
                    event_id=event_id,
                    symbol=symbol.upper(),
                    action="BUY",
                    snipe_price=round(snipe_price, 4),
                    target_exit_price=round(target_exit, 4),
                    size_base=round(snipe_size, 6),
                    notional_usd=round(snipe_notional, 2),
                    expected_profit_usd=round(expected_profit, 4),
                    discount_bps=round(discount_bps, 1),
                )
                self.executed_snipes.append(order)
                logger.info("⚡ [LiquidationHunter] Sniping Long Liquidation on %s: Buy @ $%.2f (Discount: %.1f bps, Exp PnL: $%.2f)", symbol, snipe_price, discount_bps, expected_profit)
                return order

        # Case 2: Short Liquidation (forced buy spiked above market -> we SELL at premium)
        elif side == LiquidationSide.SHORT_LIQUIDATED:
            premium_pct = max(0.0, (bankruptcy_price - mark_price) / mark_price * 100.0)
            premium_bps = premium_pct * 100.0
            if premium_bps >= self.min_discount_bps:
                snipe_price = bankruptcy_price
                target_exit = mark_price * (1.0 - (self.target_profit_bps / 10000.0))
                expected_profit = (snipe_price - target_exit) * snipe_size
                order = LiquidationSnipeOrder(
                    event_id=event_id,
                    symbol=symbol.upper(),
                    action="SELL",
                    snipe_price=round(snipe_price, 4),
                    target_exit_price=round(target_exit, 4),
                    size_base=round(snipe_size, 6),
                    notional_usd=round(snipe_notional, 2),
                    expected_profit_usd=round(expected_profit, 4),
                    discount_bps=round(premium_bps, 1),
                )
                self.executed_snipes.append(order)
                logger.info("⚡ [LiquidationHunter] Sniping Short Liquidation on %s: Sell @ $%.2f (Premium: %.1f bps, Exp PnL: $%.2f)", symbol, snipe_price, premium_bps, expected_profit)
                return order

        return None
