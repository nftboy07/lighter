#!/usr/bin/env python3
"""
Liquidation Cascade Predictor & Wick Rebound Snatcher (liquidation_cascade_predictor.py)
=======================================================================================
Models Open Interest (OI) concentration and cascading liquidation thresholds to predict
the exact exhaustion price and place mean-reversion limit orders at the wick bottom.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("LiquidationPredictor")


@dataclass
class CascadeReboundSetup:
    """Actionable mean-reversion setup at predicted liquidation exhaustion."""
    symbol: str
    current_price: float
    predicted_exhaustion_price: float
    rebound_entry_price: float
    expected_bounce_pct: float         # e.g. +4.5% - +8.0%
    target_tp_price: float
    hard_sl_price: float
    is_actionable: bool
    side: str                          # "BUY/LONG" (for long liquidations) or "SELL/SHORT" (for short squeezes)
    total_liquidation_notional_usd: float
    timestamp: float = field(default_factory=time.time)


class LiquidationCascadePredictor:
    """
    Predicts cascading forced liquidation exhaustion levels.
    """

    def __init__(
        self,
        min_liquidation_notional_usd: float = 25000.0,   # Minimum $25k cascade
        rebound_cushion_pct: float = 0.15,               # Place limit order 0.15% above extreme wick
        expected_rebound_pct: float = 5.0,               # Default +5% mean-reversion target
    ):
        self.min_liquidation_notional_usd = min_liquidation_notional_usd
        self.rebound_cushion_pct = rebound_cushion_pct
        self.expected_rebound_pct = expected_rebound_pct

    def predict_cascade_rebound(
        self,
        symbol: str,
        current_price: float,
        liquidation_clusters: List[Tuple[float, float, str]],  # (trigger_price, notional_usd, "LONG_LIQ"/"SHORT_LIQ")
    ) -> Optional[CascadeReboundSetup]:
        """
        Calculates the deepest liquidation cluster and optimal rebound limit entry.
        """
        sym = symbol.upper()
        if not liquidation_clusters:
            return None

        # Filter clusters by size
        long_liqs = [c for c in liquidation_clusters if c[2] == "LONG_LIQ" and c[1] >= self.min_liquidation_notional_usd]
        short_liqs = [c for c in liquidation_clusters if c[2] == "SHORT_LIQ" and c[1] >= self.min_liquidation_notional_usd]

        if long_liqs:
            # Long liquidations push price down -> Find lowest cascade price
            deepest_liq = min(long_liqs, key=lambda x: x[0])
            exhaustion_px = deepest_liq[0]
            entry_px = round(exhaustion_px * (1.0 + (self.rebound_cushion_pct / 100.0)), 4)
            tp_px = round(entry_px * (1.0 + (self.expected_rebound_pct / 100.0)), 4)
            sl_px = round(exhaustion_px * 0.985, 4)  # 1.5% below exhaustion

            setup = CascadeReboundSetup(
                symbol=sym,
                current_price=current_price,
                predicted_exhaustion_price=exhaustion_px,
                rebound_entry_price=entry_px,
                expected_bounce_pct=self.expected_rebound_pct,
                target_tp_price=tp_px,
                hard_sl_price=sl_px,
                is_actionable=True,
                side="BUY/LONG",
                total_liquidation_notional_usd=deepest_liq[1],
            )
            logger.info("🌊 [Cascade Predictor] %s Long Liq Exhaustion @ $%.2f -> Pre-positioning BUY Limit @ $%.2f (TP: $%.2f)", sym, exhaustion_px, entry_px, tp_px)
            return setup

        elif short_liqs:
            # Short liquidations push price up -> Find highest cascade price
            highest_liq = max(short_liqs, key=lambda x: x[0])
            exhaustion_px = highest_liq[0]
            entry_px = round(exhaustion_px * (1.0 - (self.rebound_cushion_pct / 100.0)), 4)
            tp_px = round(entry_px * (1.0 - (self.expected_rebound_pct / 100.0)), 4)
            sl_px = round(exhaustion_px * 1.015, 4)

            setup = CascadeReboundSetup(
                symbol=sym,
                current_price=current_price,
                predicted_exhaustion_price=exhaustion_px,
                rebound_entry_price=entry_px,
                expected_bounce_pct=self.expected_rebound_pct,
                target_tp_price=tp_px,
                hard_sl_price=sl_px,
                is_actionable=True,
                side="SELL/SHORT",
                total_liquidation_notional_usd=highest_liq[1],
            )
            logger.info("🌊 [Cascade Predictor] %s Short Liq Exhaustion @ $%.2f -> Pre-positioning SELL Limit @ $%.2f (TP: $%.2f)", sym, exhaustion_px, entry_px, tp_px)
            return setup

        return None
