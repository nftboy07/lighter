#!/usr/bin/env python3
"""
Liquidity Black Hole & Hidden Wall Shadowing Engine (hidden_wall_shadow.py)
==========================================================================
Identifies iceberg and hidden institutional orders by tracking continuous trade volume
prints that significantly exceed the displayed visible top-of-book depth.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("HiddenWallShadow")


@dataclass
class IcebergOrderDetection:
    """Detected institutional iceberg order."""
    symbol: str
    price_level: float
    side: str                          # "BUY_ICEBERG" or "SELL_ICEBERG"
    displayed_visible_size: float
    cumulative_executed_size: float
    estimated_total_hidden_usd: float
    iceberg_ratio: float               # Executed / Displayed (> 3.0x = Iceberg)
    is_confirmed_iceberg: bool
    timestamp: float = field(default_factory=time.time)


class HiddenWallShadowEngine:
    """
    Detects hidden institutional liquidity and shadow orderbook depth.
    """

    def __init__(
        self,
        min_iceberg_ratio: float = 2.5,        # 2.5x more volume than displayed
        min_hidden_notional_usd: float = 15000.0,
    ):
        self.min_iceberg_ratio = min_iceberg_ratio
        self.min_hidden_notional_usd = min_hidden_notional_usd

        # (Symbol, price_level, side) -> (displayed_size, executed_size, timestamp)
        self._price_accumulators: Dict[Tuple[str, float, str], Tuple[float, float, float]] = {}

    def record_displayed_depth(self, symbol: str, price: float, visible_size: float, is_buy: bool) -> None:
        """Records initial visible depth at a price level."""
        key = (symbol.upper(), round(price, 4), "BUY" if is_buy else "SELL")
        if key not in self._price_accumulators:
            self._price_accumulators[key] = (visible_size, 0.0, time.time())
        else:
            _, exec_sz, ts = self._price_accumulators[key]
            self._price_accumulators[key] = (visible_size, exec_sz, ts)

    def record_trade_fill(self, symbol: str, price: float, trade_size: float, is_buy: bool) -> Optional[IcebergOrderDetection]:
        """
        Records trade fills and checks if executed volume exceeds visible displayed size.
        """
        sym = symbol.upper()
        # For a buyer-initiated trade (is_buy=True), it matches against an Ask (SELL) limit order
        side = "SELL" if is_buy else "BUY"
        key = (sym, round(price, 4), side)

        if key not in self._price_accumulators:
            self._price_accumulators[key] = (trade_size * 0.5, trade_size, time.time())
        else:
            vis_sz, exec_sz, ts = self._price_accumulators[key]
            self._price_accumulators[key] = (vis_sz, exec_sz + trade_size, ts)

        vis_sz, exec_sz, _ = self._price_accumulators[key]
        ratio = (exec_sz / vis_sz) if vis_sz > 0 else 1.0
        hidden_usd = exec_sz * price

        if ratio >= self.min_iceberg_ratio and hidden_usd >= self.min_hidden_notional_usd:
            iceberg_side = f"{side}_ICEBERG"
            detection = IcebergOrderDetection(
                symbol=sym,
                price_level=price,
                side=iceberg_side,
                displayed_visible_size=round(vis_sz, 4),
                cumulative_executed_size=round(exec_sz, 4),
                estimated_total_hidden_usd=round(hidden_usd, 2),
                iceberg_ratio=round(ratio, 2),
                is_confirmed_iceberg=True,
            )
            logger.info("🧊 [ICEBERG DETECTED] %s: $%.2f %s executing @ $%.2f (%.1fx displayed depth)", sym, hidden_usd, iceberg_side, price, ratio)
            return detection

        return None
