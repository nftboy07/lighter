#!/usr/bin/env python3
"""
Multi-DEX Cross-Chain Yield & Fast Bridge Router (cross_chain_liquidity_bridger.py)
==================================================================================
Monitors funding rate differentials and basis yields across zkLighter, Hyperliquid,
Arbitrum, and Base, calculating net profitable fast bridge routes (Across / CCTP).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("CrossChainBridger")


@dataclass
class CrossChainArbRoute:
    """Actionable cross-chain liquidity and yield route."""
    symbol: str
    source_chain: str
    target_chain: str
    target_protocol: str             # "Hyperliquid", "zkLighter", "Aevo", "GMX"
    funding_spread_apr_pct: float     # e.g. +38.0% APR spread
    estimated_bridge_fee_usd: float
    estimated_bridge_time_sec: float
    net_annual_profit_usd: float
    is_actionable: bool
    recommended_bridge_usd: float
    timestamp: float = field(default_factory=time.time)


class CrossChainLiquidityBridger:
    """
    Evaluates multi-chain perp yield and fast bridge routing.
    """

    def __init__(
        self,
        min_net_spread_apr_pct: float = 20.0,   # Minimum 20% net APR
        default_bridge_fee_usd: float = 1.50,   # Standard CCTP / Across fee
        min_bridge_amount_usd: float = 50.0,
    ):
        self.min_net_spread_apr_pct = min_net_spread_apr_pct
        self.default_bridge_fee_usd = default_bridge_fee_usd
        self.min_bridge_amount_usd = min_bridge_amount_usd

    def evaluate_bridge_opportunity(
        self,
        symbol: str,
        source_funding_apr_pct: float,
        target_funding_apr_pct: float,
        source_chain: str = "Base",
        target_chain: str = "Arbitrum",
        target_protocol: str = "Hyperliquid",
        available_collateral_usd: float = 200.0,
    ) -> CrossChainArbRoute:
        """
        Calculates net cross-chain arbitrage profitability after bridge fees.
        """
        sym = symbol.upper()
        spread_apr = target_funding_apr_pct - source_funding_apr_pct

        bridge_amount = max(0.0, available_collateral_usd * 0.50)
        annual_gross_profit = bridge_amount * (spread_apr / 100.0)
        net_profit_usd = annual_gross_profit - self.default_bridge_fee_usd

        is_actionable = (spread_apr >= self.min_net_spread_apr_pct) and (bridge_amount >= self.min_bridge_amount_usd)

        route = CrossChainArbRoute(
            symbol=sym,
            source_chain=source_chain,
            target_chain=target_chain,
            target_protocol=target_protocol,
            funding_spread_apr_pct=round(spread_apr, 2),
            estimated_bridge_fee_usd=self.default_bridge_fee_usd,
            estimated_bridge_time_sec=12.0,  # ~12s CCTP/Across fast transfer
            net_annual_profit_usd=round(net_profit_usd, 2),
            is_actionable=is_actionable,
            recommended_bridge_usd=round(bridge_amount, 2) if is_actionable else 0.0,
        )

        if is_actionable:
            logger.info("🌉 [Cross-Chain Route] %s: %s -> %s (%s) Spread +%.1f%% APR ($%.2f net/yr)", sym, source_chain, target_chain, target_protocol, spread_apr, net_profit_usd)

        return route
