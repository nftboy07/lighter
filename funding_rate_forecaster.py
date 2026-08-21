#!/usr/bin/env python3
"""
Cross-Exchange Funding Rate Predictor (funding_rate_forecaster.py)
=================================================================
Forecasts the next 8-hour perpetual funding rate 1 hour in advance using
autoregressive premium-index momentum models, front-running funding payouts.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("FundingForecaster")


@dataclass
class FundingForecast:
    """Predicted next 8h funding rate."""
    symbol: str
    current_funding_rate_8h: float
    predicted_next_funding_rate_8h: float
    predicted_annual_yield_pct: float
    expected_direction: str            # "RISING", "FALLING", "STABLE"
    confidence_score: float
    is_arbitrage_profitable: bool      # >= 25% APR
    timestamp: float = field(default_factory=time.time)


class FundingRateForecaster:
    """
    Predicts next 8h funding rate payouts from premium index drift.
    """

    def __init__(
        self,
        min_arb_apr_threshold: float = 25.0,
        momentum_decay_alpha: float = 0.35,
    ):
        self.min_arb_apr_threshold = min_arb_apr_threshold
        self.momentum_decay_alpha = momentum_decay_alpha

        # Symbol -> deque of (premium_index, timestamp)
        self._premium_history: Dict[str, deque] = {}

    def record_premium_tick(self, symbol: str, perp_price: float, spot_price: float) -> None:
        """Records perpetual premium/discount index: (Perp - Spot) / Spot."""
        sym = symbol.upper()
        if sym not in self._premium_history:
            self._premium_history[sym] = deque(maxlen=60)

        if spot_price > 0:
            premium = (perp_price - spot_price) / spot_price
            self._premium_history[sym].append((premium, time.time()))

    def forecast_next_funding(
        self,
        symbol: str,
        current_funding_rate_8h: float,
    ) -> FundingForecast:
        """
        Calculates predicted funding rate using Exponentially Weighted Premium Momentum.
        """
        sym = symbol.upper()
        history = list(self._premium_history.get(sym, []))

        if len(history) < 2:
            predicted_rate = current_funding_rate_8h
            confidence = 0.50
        else:
            # Calculate EWMA of premium index
            premiums = [p[0] for p in history]
            ewma_prem = premiums[0]
            for p in premiums[1:]:
                ewma_prem = (self.momentum_decay_alpha * p) + ((1.0 - self.momentum_decay_alpha) * ewma_prem)

            # Funding rate clamps to TWAP premium + interest rate clamp
            predicted_rate = (0.70 * current_funding_rate_8h) + (0.30 * (ewma_prem / 3.0))
            confidence = min(0.95, 0.50 + (len(history) * 0.01))

        predicted_apr = predicted_rate * 3.0 * 365.0 * 100.0

        if predicted_rate > current_funding_rate_8h * 1.05:
            direction = "RISING"
        elif predicted_rate < current_funding_rate_8h * 0.95:
            direction = "FALLING"
        else:
            direction = "STABLE"

        is_profitable = predicted_apr >= self.min_arb_apr_threshold

        forecast = FundingForecast(
            symbol=sym,
            current_funding_rate_8h=round(current_funding_rate_8h, 6),
            predicted_next_funding_rate_8h=round(predicted_rate, 6),
            predicted_annual_yield_pct=round(predicted_apr, 2),
            expected_direction=direction,
            confidence_score=round(confidence, 2),
            is_arbitrage_profitable=is_profitable,
        )

        return forecast
