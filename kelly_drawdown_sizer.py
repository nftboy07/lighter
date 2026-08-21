#!/usr/bin/env python3
"""
Adaptive Kelly Bet Sizing & Drawdown Compounding Curve (kelly_drawdown_sizer.py)
================================================================================
Calculates mathematical position sizing using the Fractional Kelly Criterion:
f* = (p * (b + 1) - 1) / b
Applies dynamic deleveraging dampening during drawdown phases to prevent ruin.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("KellySizer")


@dataclass
class KellySizingRecommendation:
    """Optimal trade sizing derived from Kelly criterion."""
    symbol: str
    optimal_kelly_fraction: float    # Raw Kelly f*
    conservative_half_kelly: float   # 0.5 * f*
    recommended_position_usd: float  # Dollar position size
    effective_leverage: float
    current_drawdown_dampener: float # 1.0 (no DD) down to 0.4 (severe DD)
    reason: str
    timestamp: float = field(default_factory=time.time)


class AdaptiveKellyDrawdownSizer:
    """
    Fractional Kelly Criterion & Drawdown Sizing Engine.
    """

    def __init__(
        self,
        max_kelly_fraction: float = 0.25,     # Cap single-trade allocation to 25% of equity
        fractional_multiplier: float = 0.50,  # Half-Kelly for institutional safety
        max_allowed_leverage: float = 5.0,
    ):
        self.max_kelly_fraction = max_kelly_fraction
        self.fractional_multiplier = fractional_multiplier
        self.max_allowed_leverage = max_allowed_leverage

    def calculate_trade_size(
        self,
        symbol: str,
        total_portfolio_usd: float,
        win_rate_p: float = 0.65,             # e.g. 65% win probability
        win_loss_payoff_b: float = 2.0,       # e.g. $2 reward for every $1 risked
        current_drawdown_pct: float = 0.0,    # Drawdown % from peak equity
    ) -> KellySizingRecommendation:
        """
        Calculates optimal position sizing:
        f* = (p * (b + 1) - 1) / b
        """
        sym = symbol.upper()
        p = max(0.01, min(0.99, win_rate_p))
        b = max(0.1, win_loss_payoff_b)

        raw_kelly = (p * (b + 1.0) - 1.0) / b
        raw_kelly = max(0.0, raw_kelly)

        # Apply Half-Kelly
        half_kelly = raw_kelly * self.fractional_multiplier
        half_kelly = min(self.max_kelly_fraction, half_kelly)

        # Drawdown dampener: Scale down if in drawdown
        # 0% DD -> 1.0x, 10% DD -> 0.7x, 20% DD -> 0.4x
        dd_dampener = max(0.35, 1.0 - (current_drawdown_pct / 100.0) * 3.0)

        effective_alloc = half_kelly * dd_dampener
        pos_usd = round(total_portfolio_usd * effective_alloc, 2)
        leverage = round(min(self.max_allowed_leverage, (pos_usd / max(1.0, total_portfolio_usd)) * 5.0), 2)

        reason = f"Half-Kelly: {half_kelly*100:.1f}% | DD Dampener: {dd_dampener:.2f}x (WinRate: {p*100:.0f}%, Payoff: {b:.1f}x)"

        return KellySizingRecommendation(
            symbol=sym,
            optimal_kelly_fraction=round(raw_kelly, 4),
            conservative_half_kelly=round(half_kelly, 4),
            recommended_position_usd=pos_usd,
            effective_leverage=leverage,
            current_drawdown_dampener=round(dd_dampener, 2),
            reason=reason,
        )
