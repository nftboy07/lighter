#!/usr/bin/env python3
"""
Dynamic Funding Rate & Borrow Cost Yield Optimizer (funding_borrow_optimizer.py)
================================================================================
Continuously calculates net annualized cash-and-carry basis yield across
perp funding rates (zkLighter, Hyperliquid) and DeFi borrow costs (Aave, Morpho).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("FundingBorrowOptimizer")


@dataclass
class NetYieldOpportunity:
    """Consolidated net yield spread across funding and lending protocols."""
    symbol: str
    perp_exchange: str               # "zkLighter" or "Hyperliquid"
    perp_funding_apr_pct: float       # Annualized funding rate (e.g. +35.0%)
    borrow_protocol: str             # "Aave_v3" or "Morpho"
    borrow_rate_apr_pct: float        # Cost of borrowing base/stable collateral (e.g. 4.5%)
    net_spread_apr_pct: float         # Net Profit APR = Funding - Borrow
    is_actionable: bool              # Net APR >= 20%
    recommended_collateral_usd: float
    timestamp: float = field(default_factory=time.time)


class FundingBorrowYieldOptimizer:
    """
    Optimizes net yields across perpetual funding rates and borrow rates.
    """

    def __init__(
        self,
        min_actionable_net_apr: float = 20.0,   # Minimum 20% net APR
        unwind_net_apr: float = 5.0,             # Unwind when net APR drops below 5%
    ):
        self.min_actionable_net_apr = min_actionable_net_apr
        self.unwind_net_apr = unwind_net_apr

    def evaluate_net_yield(
        self,
        symbol: str,
        perp_funding_8h_rate: float,         # e.g. 0.0003 (0.03% per 8 hours)
        borrow_rate_annual_pct: float = 4.5, # e.g. 4.5% APR borrow cost on Aave
        perp_exchange: str = "zkLighter",
        borrow_protocol: str = "Aave_v3",
        available_collateral_usd: float = 100.0,
    ) -> NetYieldOpportunity:
        """
        Calculates annualized net yield:
        Funding APR = 8h_Rate * 3 * 365 * 100
        Net APR = Funding APR - Borrow APR
        """
        sym = symbol.upper()
        # Annualize 8h funding rate (3 funding intervals per day * 365 days)
        funding_apr_pct = perp_funding_8h_rate * 3.0 * 365.0 * 100.0
        net_apr_pct = funding_apr_pct - borrow_rate_annual_pct

        is_actionable = net_apr_pct >= self.min_actionable_net_apr

        opp = NetYieldOpportunity(
            symbol=sym,
            perp_exchange=perp_exchange,
            perp_funding_apr_pct=round(funding_apr_pct, 2),
            borrow_protocol=borrow_protocol,
            borrow_rate_apr_pct=round(borrow_rate_annual_pct, 2),
            net_spread_apr_pct=round(net_apr_pct, 2),
            is_actionable=is_actionable,
            recommended_collateral_usd=round(available_collateral_usd * 0.50, 2) if is_actionable else 0.0,
        )

        if is_actionable:
            logger.info("🌾 [Yield Optimizer] %s Net Yield Spread: +%.2f%% APR (Funding: +%.1f%% | Borrow: -%.1f%%)", sym, net_apr_pct, funding_apr_pct, borrow_rate_annual_pct)

        return opp
