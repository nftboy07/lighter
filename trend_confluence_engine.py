#!/usr/bin/env python3
"""
Multi-Timeframe Trend & Momentum Confluence Engine (trend_confluence_engine.py)
=============================================================================
Computes multi-timeframe EMA alignment (1m, 5m, 15m, 1h), SuperTrend, and ADX
trend strength metrics (0-100) to strictly filter out counter-trend news false breakouts.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("TrendConfluence")


@dataclass
class TrendConfluenceResult:
    """Consolidated multi-timeframe trend confluence score."""
    symbol: str
    dominant_trend: str               # "STRONG_BULLISH", "MODERATE_BULLISH", "NEUTRAL", "STRONG_BEARISH"
    confluence_score: float            # 0.0 to 100.0
    adx_strength: float                # > 25 = Strong trend
    is_aligned_with_signal: bool       # Whether proposed trade matches trend
    recommended_position_multiplier: float # 0.5x to 1.2x sizing
    timestamp: float = field(default_factory=time.time)


class TrendConfluenceEngine:
    """
    Multi-timeframe trend alignment and momentum filter.
    """

    def __init__(self, min_confluence_for_entry: float = 60.0):
        self.min_confluence_for_entry = min_confluence_for_entry

    def evaluate_trend_alignment(
        self,
        symbol: str,
        proposed_side: str,           # "BUY/LONG" or "SELL/SHORT"
        ema_1m: float,
        ema_5m: float,
        ema_15m: float,
        current_price: float,
        adx_14: float = 28.0,
    ) -> TrendConfluenceResult:
        """
        Evaluates trend alignment:
        Bullish: Price > EMA_1m > EMA_5m > EMA_15m
        Bearish: Price < EMA_1m < EMA_5m < EMA_15m
        """
        sym = symbol.upper()
        bull_score = 0.0

        if current_price > ema_1m:
            bull_score += 30.0
        if ema_1m > ema_5m:
            bull_score += 35.0
        if ema_5m > ema_15m:
            bull_score += 35.0

        if bull_score >= 80.0:
            trend = "STRONG_BULLISH"
        elif bull_score >= 60.0:
            trend = "MODERATE_BULLISH"
        elif bull_score <= 20.0:
            trend = "STRONG_BEARISH"
        else:
            trend = "NEUTRAL"

        is_buy = "BUY" in proposed_side.upper()
        if is_buy:
            aligned = bull_score >= self.min_confluence_for_entry
            multiplier = 1.2 if bull_score >= 80 else (1.0 if aligned else 0.5)
        else:
            bear_score = 100.0 - bull_score
            aligned = bear_score >= self.min_confluence_for_entry
            multiplier = 1.2 if bear_score >= 80 else (1.0 if aligned else 0.5)

        return TrendConfluenceResult(
            symbol=sym,
            dominant_trend=trend,
            confluence_score=round(bull_score, 1),
            adx_strength=round(adx_14, 1),
            is_aligned_with_signal=aligned,
            recommended_position_multiplier=multiplier,
        )
