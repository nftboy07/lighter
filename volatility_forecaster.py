#!/usr/bin/env python3
"""
GARCH(1,1) & Parkinson Volatility Squeeze Forecaster (volatility_forecaster.py)
=============================================================================
Calculates rolling high-frequency Parkinson realized volatility and GARCH(1,1)
conditional variance to detect volatility squeezes before breakouts.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("VolatilityForecaster")


@dataclass
class VolatilityForecast:
    """Intraday volatility forecast and trading adjustments."""
    symbol: str
    current_volatility_pct: float
    garch_forecast_volatility_pct: float
    is_volatility_squeeze: bool         # Compression preceding explosive move
    is_extreme_expansion: bool          # Violent ongoing spike
    recommended_grid_spacing_mult: float  # Multiplier for MM grid layer spacing (e.g. 1.5x - 2.5x)
    recommended_tp_target_mult: float    # Multiplier for Sniper take-profit target (e.g. 1.5x - 3.0x)
    timestamp: float = field(default_factory=time.time)


class GARCHVolatilityForecaster:
    """
    Rolling GARCH(1,1) and Parkinson Volatility Model.
    """

    def __init__(
        self,
        omega: float = 0.000002,   # Long-term baseline variance
        alpha: float = 0.10,       # Reaction to recent shock
        beta: float = 0.85,        # Persistence of past variance
        squeeze_threshold_pct: float = 12.0,  # Below 12% annualized = Squeeze
        expansion_threshold_pct: float = 65.0, # Above 65% annualized = High Expansion
    ):
        self.omega = omega
        self.alpha = alpha
        self.beta = beta
        self.squeeze_threshold_pct = squeeze_threshold_pct
        self.expansion_threshold_pct = expansion_threshold_pct

        # Price history: symbol -> deque of (high, low, close)
        self._price_history: Dict[str, deque] = {}
        self._last_variance: Dict[str, float] = {}

    def update_candle(self, symbol: str, high: float, low: float, close: float) -> None:
        """Records 1-minute or 5-second candle for volatility calculation."""
        sym = symbol.upper()
        if sym not in self._price_history:
            self._price_history[sym] = deque(maxlen=100)
            self._last_variance[sym] = 0.0004  # Initial baseline variance (~20% annual)

        self._price_history[sym].append((high, low, close))

    def calculate_parkinson_volatility(self, symbol: str) -> float:
        """
        Calculates Parkinson High-Low Realized Volatility:
        sigma = sqrt( 1 / (4 * ln(2) * N) * sum( (ln(High / Low))^2 ) )
        """
        sym = symbol.upper()
        history = self._price_history.get(sym, deque())
        if len(history) < 2:
            return 25.0  # Default baseline annualized vol 25%

        sum_hl = 0.0
        n = len(history)
        for h, l, _ in history:
            if h > 0 and l > 0 and h >= l:
                ratio = math.log(h / l)
                sum_hl += ratio * ratio

        parkinson_variance = (1.0 / (4.0 * math.log(2.0) * n)) * sum_hl
        annualized_vol_pct = math.sqrt(max(1e-8, parkinson_variance * 525600.0)) * 100.0
        return round(annualized_vol_pct, 2)

    def forecast_volatility(self, symbol: str) -> VolatilityForecast:
        """
        Calculates current Parkinson vol and forecasts next-period GARCH variance.
        """
        sym = symbol.upper()
        current_vol = self.calculate_parkinson_volatility(sym)

        # GARCH(1,1) step: sigma^2_t = omega + alpha * r^2_{t-1} + beta * sigma^2_{t-1}
        history = self._price_history.get(sym, deque())
        prev_var = self._last_variance.get(sym, 0.0004)

        if len(history) >= 2:
            p_prev = history[-2][2]
            p_curr = history[-1][2]
            ret = math.log(p_curr / p_prev) if p_prev > 0 and p_curr > 0 else 0.0
            new_var = self.omega + (self.alpha * ret * ret) + (self.beta * prev_var)
        else:
            new_var = prev_var

        self._last_variance[sym] = new_var
        forecast_vol_pct = round(math.sqrt(max(1e-8, new_var * 525600.0)) * 100.0, 2)

        # Squeeze vs Expansion classification
        is_squeeze = current_vol <= self.squeeze_threshold_pct
        is_expansion = current_vol >= self.expansion_threshold_pct

        if is_expansion:
            grid_mult = 2.0
            tp_mult = 2.5
        elif is_squeeze:
            grid_mult = 0.75
            tp_mult = 1.8  # Expecting explosive breakout from squeeze
        else:
            grid_mult = 1.0
            tp_mult = 1.0

        return VolatilityForecast(
            symbol=sym,
            current_volatility_pct=current_vol,
            garch_forecast_volatility_pct=forecast_vol_pct,
            is_volatility_squeeze=is_squeeze,
            is_extreme_expansion=is_expansion,
            recommended_grid_spacing_mult=grid_mult,
            recommended_tp_target_mult=tp_mult,
        )
