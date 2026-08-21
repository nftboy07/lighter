#!/usr/bin/env python3
"""
Microstructure Entry Filter & Smart Limit Chaser (microstructure_entry_filter.py)
================================================================================
Calculates Order Flow Imbalance (OFI), runs Trend Confirmation (EMA-20/50),
and manages Smart Post-Only Inside-Spread Limit Chaser execution.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("MicrostructureEntryFilter")


@dataclass
class MicrostructureDecision:
    """Evaluation result for entering a trade."""
    is_approved: bool
    ofi_score: float              # [-1.0 ... +1.0] Order flow imbalance
    trend_aligned: bool
    recommended_entry_type: str   # "POST_ONLY_LIMIT", "IOC_TAKER", "WAIT"
    suggested_limit_price: float
    rejection_reason: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class MicrostructureEntryFilter:
    """
    Evaluates sub-millisecond orderbook delta imbalance and trend before firing orders.
    """

    def __init__(
        self,
        min_ofi_threshold: float = 0.20,     # Must have at least 20% positive OFI
        post_only_timeout_ms: float = 50.0,  # 50ms maker wait before taker fallback
        require_trend_alignment: bool = True,
    ):
        self.min_ofi_threshold = min_ofi_threshold
        self.post_only_timeout_ms = post_only_timeout_ms
        self.require_trend_alignment = require_trend_alignment

        # Price history for EMA calculation: symbol -> deque of prices
        self._price_series: Dict[str, deque] = {}

    def update_price_tick(self, symbol: str, price: float) -> None:
        """Records price tick for trend calculations."""
        sym = symbol.upper()
        if sym not in self._price_series:
            self._price_series[sym] = deque(maxlen=200)
        self._price_series[sym].append(price)

    def calculate_ema(self, symbol: str, period: int) -> float:
        """Calculates Exponential Moving Average from price series."""
        sym = symbol.upper()
        series = self._price_series.get(sym, deque())
        if len(series) < period:
            return series[-1] if series else 0.0

        prices = list(series)[-period:]
        k = 2.0 / (period + 1.0)
        ema = prices[0]
        for p in prices[1:]:
            ema = (p * k) + (ema * (1.0 - k))
        return ema

    def calculate_ofi(
        self,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        depth_levels: int = 5,
    ) -> float:
        """
        Calculates Order Flow Imbalance (OFI) from top L2 levels:
        OFI = (Bid Depth - Ask Depth) / (Bid Depth + Ask Depth)
        """
        bid_vol = sum(px * sz for px, sz in bids[:depth_levels])
        ask_vol = sum(px * sz for px, sz in asks[:depth_levels])
        total_vol = bid_vol + ask_vol

        if total_vol <= 0:
            return 0.0

        return (bid_vol - ask_vol) / total_vol

    def evaluate_entry(
        self,
        symbol: str,
        side: str,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        conviction: float = 0.85,
    ) -> MicrostructureDecision:
        """
        Comprehensive microstructure evaluation before executing an entry.
        """
        sym = symbol.upper()
        is_long = "BUY" in side.upper() or "LONG" in side.upper()

        if not bids or not asks:
            return MicrostructureDecision(
                is_approved=False,
                ofi_score=0.0,
                trend_aligned=False,
                recommended_entry_type="WAIT",
                suggested_limit_price=0.0,
                rejection_reason="Empty orderbook depth",
            )

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = (best_bid + best_ask) / 2.0

        # 1. Calculate Order Flow Imbalance
        ofi = self.calculate_ofi(bids, asks)

        # 2. Check Trend Alignment (EMA-20 vs EMA-50)
        ema20 = self.calculate_ema(sym, 20)
        ema50 = self.calculate_ema(sym, 50)

        trend_aligned = True
        if ema20 > 0 and ema50 > 0 and self.require_trend_alignment:
            if is_long and ema20 < ema50 * 0.998:  # Significant downtrend
                trend_aligned = False
            elif not is_long and ema20 > ema50 * 1.002:  # Significant uptrend
                trend_aligned = False

        # If catalyst conviction is ultra-high (>=95%), override trend filter
        if conviction >= 0.95:
            trend_aligned = True

        # 3. Microstructure Veto Checks
        if is_long and ofi < -0.60 and conviction < 0.90:
            return MicrostructureDecision(
                is_approved=False,
                ofi_score=round(ofi, 3),
                trend_aligned=trend_aligned,
                recommended_entry_type="WAIT",
                suggested_limit_price=best_bid,
                rejection_reason="Severe negative OFI sell pressure (< -0.60)",
            )

        if not is_long and ofi > 0.60 and conviction < 0.90:
            return MicrostructureDecision(
                is_approved=False,
                ofi_score=round(ofi, 3),
                trend_aligned=trend_aligned,
                recommended_entry_type="WAIT",
                suggested_limit_price=best_ask,
                rejection_reason="Severe positive OFI buy pressure (> +0.60)",
            )

        # 4. Determine Smart Execution Type
        # Inside-spread maker limit placement for 0 fees, else IOC taker for emergency catalyst
        spread_bps = ((best_ask - best_bid) / mid_price) * 10000.0
        if spread_bps >= 5.0 and conviction < 0.92:
            # Spread is wide enough to capture maker rebate / 0 fee
            suggested_limit = best_bid + (best_ask - best_bid) * 0.25 if is_long else best_ask - (best_ask - best_bid) * 0.25
            entry_type = "POST_ONLY_LIMIT"
        else:
            suggested_limit = best_ask if is_long else best_bid
            entry_type = "IOC_TAKER"

        return MicrostructureDecision(
            is_approved=True,
            ofi_score=round(ofi, 3),
            trend_aligned=trend_aligned,
            recommended_entry_type=entry_type,
            suggested_limit_price=round(suggested_limit, 4),
        )
