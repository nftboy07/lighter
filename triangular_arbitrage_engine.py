#!/usr/bin/env python3
"""
CEX-DEX Triangular Arbitrage Loop Engine (triangular_arbitrage_engine.py)
========================================================================
Scans 3-way cross-currency triangular cycles:
e.g. USDC -> BaseToken1 (SOL) -> BaseToken2 (ETH) -> USDC
Calculates cross-rates, depth availability, and net profit after fees.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("TriangularArb")


@dataclass
class TriangularArbCycle:
    """Actionable 3-leg triangular arbitrage loop."""
    cycle_name: str                   # e.g. "USDC -> SOL -> ETH -> USDC"
    leg1_market: str                  # "SOL/USDC"
    leg2_market: str                  # "SOL/ETH"
    leg3_market: str                  # "ETH/USDC"
    implied_synthetic_rate: float
    direct_market_rate: float
    gross_dislocation_bps: float      # e.g. +14.5 bps
    estimated_fees_bps: float         # 0 bps on zkLighter maker / promotional taker
    net_profit_bps: float
    is_actionable: bool
    recommended_trade_usd: float
    timestamp: float = field(default_factory=time.time)


class TriangularArbitrageEngine:
    """
    Evaluates 3-way cross-currency synthetic rate loops.
    """

    def __init__(
        self,
        min_profit_threshold_bps: float = 6.0,  # Minimum 6 bps net profit
        estimated_fee_bps: float = 0.0,
    ):
        self.min_profit_threshold_bps = min_profit_threshold_bps
        self.estimated_fee_bps = estimated_fee_bps

    def evaluate_triangular_cycle(
        self,
        base1: str,                   # "SOL"
        base2: str,                   # "ETH"
        quote: str,                   # "USDC"
        px_base1_quote: float,        # SOL/USDC price (e.g. 150.0)
        px_base2_quote: float,        # ETH/USDC price (e.g. 2000.0)
        px_base1_base2: float,        # SOL/ETH cross price (e.g. 0.0750)
        available_margin_usd: float = 200.0,
    ) -> Optional[TriangularArbCycle]:
        """
        Computes implied synthetic cross-rate vs direct market cross-rate:
        Synthetic SOL/ETH = (SOL/USDC) / (ETH/USDC)
        Gross Dislocation = (Synthetic - Direct) / Direct * 10000 bps
        """
        if px_base2_quote <= 0 or px_base1_base2 <= 0:
            return None

        # Theoretical cross-rate
        synthetic_cross = px_base1_quote / px_base2_quote
        direct_cross = px_base1_base2

        dislocation_bps = ((synthetic_cross - direct_cross) / direct_cross) * 10000.0
        abs_dislocation = abs(dislocation_bps)
        net_profit_bps = abs_dislocation - (self.estimated_fee_bps * 3.0)

        is_actionable = net_profit_bps >= self.min_profit_threshold_bps

        cycle_name = f"{quote} -> {base1} -> {base2} -> {quote}"
        if dislocation_bps < 0:
            cycle_name = f"{quote} -> {base2} -> {base1} -> {quote}"

        cycle = TriangularArbCycle(
            cycle_name=cycle_name,
            leg1_market=f"{base1}/{quote}",
            leg2_market=f"{base1}/{base2}",
            leg3_market=f"{base2}/{quote}",
            implied_synthetic_rate=round(synthetic_cross, 6),
            direct_market_rate=round(direct_cross, 6),
            gross_dislocation_bps=round(abs_dislocation, 2),
            estimated_fees_bps=round(self.estimated_fee_bps * 3.0, 2),
            net_profit_bps=round(net_profit_bps, 2),
            is_actionable=is_actionable,
            recommended_trade_usd=round(available_margin_usd * 0.50, 2) if is_actionable else 0.0,
        )

        if is_actionable:
            logger.info("🔄 [Triangular Arb] %s Dislocation: +%.2f bps (Synthetic: %.6f vs Direct: %.6f)", cycle_name, net_profit_bps, synthetic_cross, direct_cross)

        return cycle
