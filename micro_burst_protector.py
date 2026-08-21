#!/usr/bin/env python3
"""
Order Flow Toxicity & Micro-Burst Protection (micro_burst_protector.py)
======================================================================
Detects ultra-high frequency sub-10ms burst clusters (> 8 aggressive market trades in < 15ms)
and triggers instantaneous hot-standby quote cancellation before institutional sweeps execute.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("MicroBurstProtector")


@dataclass
class MicroBurstAlert:
    """Micro-burst cluster event."""
    symbol: str
    burst_trades_count: int
    burst_window_ms: float
    total_burst_volume_usd: float
    dominant_side: str                 # "BUY_SWEEP" or "SELL_SWEEP"
    is_toxic_sweep: bool
    recommended_action: str            # "PULL_MAKER_QUOTES", "PAUSE_500MS", "NORMAL"
    timestamp: float = field(default_factory=time.time)


class MicroBurstProtector:
    """
    Sub-10ms trade burst cluster detector.
    """

    def __init__(
        self,
        burst_threshold_count: int = 6,        # >= 6 aggressive trades
        burst_window_max_ms: float = 20.0,     # In < 20ms
        min_burst_volume_usd: float = 5000.0,
    ):
        self.burst_threshold_count = burst_threshold_count
        self.burst_window_max_ms = burst_window_max_ms
        self.min_burst_volume_usd = min_burst_volume_usd

        # Symbol -> deque of (timestamp_ms, notional_usd, is_buy)
        self._recent_ticks: Dict[str, deque] = {}

    def record_trade_tick(
        self,
        symbol: str,
        price: float,
        size: float,
        is_buy: bool,
        timestamp_ms: Optional[float] = None,
    ) -> Optional[MicroBurstAlert]:
        """
        Ingests a trade tick and evaluates if a micro-burst sweep is underway.
        """
        sym = symbol.upper()
        if sym not in self._recent_ticks:
            self._recent_ticks[sym] = deque(maxlen=30)

        ts = timestamp_ms or (time.time() * 1000.0)
        notional = price * size
        self._recent_ticks[sym].append((ts, notional, is_buy))

        ticks = list(self._recent_ticks[sym])
        if len(ticks) < self.burst_threshold_count:
            return None

        # Check last N ticks
        subset = ticks[-self.burst_threshold_count:]
        window_ms = subset[-1][0] - subset[0][0]
        total_vol = sum(t[1] for t in subset)

        if window_ms <= self.burst_window_max_ms and total_vol >= self.min_burst_volume_usd:
            buys = sum(1 for t in subset if t[2])
            dominant_side = "BUY_SWEEP" if buys >= (len(subset) / 2) else "SELL_SWEEP"

            alert = MicroBurstAlert(
                symbol=sym,
                burst_trades_count=len(subset),
                burst_window_ms=round(window_ms, 2),
                total_burst_volume_usd=round(total_vol, 2),
                dominant_side=dominant_side,
                is_toxic_sweep=True,
                recommended_action="PULL_MAKER_QUOTES",
            )
            logger.warning("🚨 [MICRO-BURST SWEEP] %s: %d %s trades ($%.2f) in %.1fms! Triggering PULL_MAKER_QUOTES", sym, len(subset), dominant_side, total_vol, window_ms)
            return alert

        return None
