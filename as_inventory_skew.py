#!/usr/bin/env python3
"""
Avellaneda-Stoikov Dynamic Inventory Skew Engine (as_inventory_skew.py)
======================================================================
Computes optimal reservation prices R(s, q, t) = s - q * gamma * sigma^2 * (T - t)
and optimal bid/ask spreads delta^a, delta^b across all 30+ tradable assets.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ASInventorySkew")


@dataclass
class ASQuotingParameters:
    """Optimal Avellaneda-Stoikov bid/ask quotes and reservation price."""
    symbol: str
    mid_price: float
    current_inventory_q: float        # Base asset position (+ for long, - for short)
    reservation_price: float           # R(s, q, t)
    optimal_bid_price: float           # R - delta^b
    optimal_ask_price: float           # R + delta^a
    half_spread_bps: float
    inventory_skew_bps: float          # Offset from mid price in bps
    timestamp: float = field(default_factory=time.time)


class ASInventorySkewEngine:
    """
    Multi-Asset Avellaneda-Stoikov Market Making Quoting Calculator.
    """

    def __init__(
        self,
        risk_aversion_gamma: float = 0.1,      # Risk aversion parameter (gamma)
        order_arrival_intensity_k: float = 1.5, # Liquidity parameter (kappa)
        terminal_time_T: float = 1.0,          # Quoting horizon in days
    ):
        self.risk_aversion_gamma = risk_aversion_gamma
        self.order_arrival_intensity_k = order_arrival_intensity_k
        self.terminal_time_T = terminal_time_T

    def calculate_quotes(
        self,
        symbol: str,
        mid_price: float,
        inventory_q: float,
        annualized_volatility_sigma: float = 0.65,
        elapsed_time_t: float = 0.0,
    ) -> ASQuotingParameters:
        """
        Calculates reservation price and optimal quoting spreads:
        R(s, q, t) = s - q * gamma * sigma^2 * (T - t)
        Spread = gamma * sigma^2 * (T - t) + (2 / gamma) * ln(1 + gamma / k)
        """
        sym = symbol.upper()
        time_left = max(0.01, self.terminal_time_T - elapsed_time_t)
        sigma2 = annualized_volatility_sigma ** 2

        # 1. Reservation price
        inventory_offset = inventory_q * self.risk_aversion_gamma * sigma2 * time_left
        reservation_px = mid_price - inventory_offset

        # 2. Optimal half-spread
        spread_term = (2.0 / self.risk_aversion_gamma) * math.log(1.0 + (self.risk_aversion_gamma / self.order_arrival_intensity_k))
        optimal_half_spread = (self.risk_aversion_gamma * sigma2 * time_left + spread_term) / 2.0
        optimal_half_spread_px = mid_price * (optimal_half_spread / 100.0)

        opt_bid = round(reservation_px - optimal_half_spread_px, 4)
        opt_ask = round(reservation_px + optimal_half_spread_px, 4)

        skew_bps = ((reservation_px - mid_price) / mid_price) * 10000.0
        half_spread_bps = (optimal_half_spread_px / mid_price) * 10000.0

        return ASQuotingParameters(
            symbol=sym,
            mid_price=mid_price,
            current_inventory_q=inventory_q,
            reservation_price=round(reservation_px, 4),
            optimal_bid_price=opt_bid,
            optimal_ask_price=opt_ask,
            half_spread_bps=round(half_spread_bps, 2),
            inventory_skew_bps=round(skew_bps, 2),
        )
