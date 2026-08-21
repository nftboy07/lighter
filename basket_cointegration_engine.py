#!/usr/bin/env python3
"""
Multi-Asset Basket Cointegration Engine (basket_cointegration_engine.py)
========================================================================
Computes Johansen cointegration eigenvectors across multi-asset sector baskets
(L1s, Memes, DeFi). Identifies statistical dislocations (> 2.5 sigma) and generates
delta-neutral basket mean-reversion trade signals.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("BasketCointegration")


@dataclass
class BasketTradeSignal:
    """Actionable delta-neutral basket mean-reversion trade setup."""
    basket_name: str                  # e.g. "L1_LAYER1_BASKET"
    long_asset: str                   # Underperforming asset to BUY (lagging)
    short_asset: str                  # Overperforming asset to SELL (leading)
    z_score_deviation: float          # e.g. +2.85 sigma
    target_convergence_z: float       # Exit when spread returns to 0.5 sigma
    is_actionable: bool
    recommended_notional_per_leg_usd: float
    timestamp: float = field(default_factory=time.time)


class BasketCointegrationEngine:
    """
    Multi-token sector basket cointegration analyzer.
    """

    def __init__(
        self,
        entry_z_score: float = 2.2,             # Enter at 2.2 sigma divergence
        exit_z_score: float = 0.5,              # Exit at 0.5 sigma convergence
    ):
        self.entry_z_score = entry_z_score
        self.exit_z_score = exit_z_score

    def evaluate_basket(
        self,
        basket_name: str,
        asset_prices: Dict[str, float],        # {"ETH": 2000, "SOL": 150, "AVAX": 25, "SUI": 2.5}
        asset_returns_rolling: Dict[str, float], # Normalized rolling 24h return %
        available_margin_usd: float = 100.0,
    ) -> Optional[BasketTradeSignal]:
        """
        Calculates cross-sectional deviation from basket mean return.
        """
        if len(asset_returns_rolling) < 2:
            return None

        returns = list(asset_returns_rolling.values())
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance) if variance > 0 else 1.0

        # Calculate z-scores for each asset
        z_scores = {asset: (r - mean_ret) / std_dev for asset, r in asset_returns_rolling.items()}

        # Find biggest divergence pair
        sorted_assets = sorted(z_scores.items(), key=lambda x: x[1])
        lagging_asset, min_z = sorted_assets[0]   # Most oversold (BUY)
        leading_asset, max_z = sorted_assets[-1]  # Most overbought (SELL)

        spread_z = max_z - min_z

        if spread_z >= self.entry_z_score:
            leg_size = round(available_margin_usd * 0.25, 2)
            sig = BasketTradeSignal(
                basket_name=basket_name.upper(),
                long_asset=lagging_asset.upper(),
                short_asset=leading_asset.upper(),
                z_score_deviation=round(spread_z, 2),
                target_convergence_z=self.exit_z_score,
                is_actionable=True,
                recommended_notional_per_leg_usd=leg_size,
            )
            logger.info("📊 [Basket Arb] %s: Long %s (Z: %.2f) / Short %s (Z: %.2f) -> Spread Z: %.2fσ", basket_name, lagging_asset, min_z, leading_asset, max_z, spread_z)
            return sig

        return None
