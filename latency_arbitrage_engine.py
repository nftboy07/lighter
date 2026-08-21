#!/usr/bin/env python3
"""
Cross-Exchange Latency Lead Arbitrage Engine (latency_arbitrage_engine.py)
========================================================================
Detects 50-150ms lead-lag dislocations between primary CEX trade feeds (Binance/Coinbase)
and zkLighter orderbook repricing, capturing instant risk-free latency alpha.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("LatencyArbitrage")


@dataclass
class LatencyArbSignal:
    """Actionable latency arbitrage signal."""
    symbol: str
    cex_price: float
    dex_price: float
    spread_bps: float
    action_side: str                  # "BUY/LONG" (if CEX > DEX) or "SELL/SHORT" (if CEX < DEX)
    latency_delta_ms: float
    is_actionable: bool
    expected_net_profit_bps: float
    timestamp: float = field(default_factory=time.time)


class LatencyLeadArbitrageEngine:
    """
    Sub-millisecond latency lead-lag arb evaluator.
    """

    def __init__(
        self,
        min_dislocation_bps: float = 12.0,      # Minimum 12 bps price gap
        max_acceptable_latency_ms: float = 200.0, # Reject if signal is older than 200ms
        estimated_taker_fee_bps: float = 0.0,    # 0 fees on zkLighter maker / promotional taker
    ):
        self.min_dislocation_bps = min_dislocation_bps
        self.max_acceptable_latency_ms = max_acceptable_latency_ms
        self.estimated_taker_fee_bps = estimated_taker_fee_bps

        # Symbol -> (last_price, timestamp)
        self._cex_ticks: Dict[str, Tuple[float, float]] = {}
        self._dex_ticks: Dict[str, Tuple[float, float]] = {}

    def update_cex_tick(self, symbol: str, price: float, timestamp_ms: Optional[float] = None) -> None:
        """Records incoming fast CEX trade tick."""
        ts = timestamp_ms / 1000.0 if timestamp_ms else time.time()
        self._cex_ticks[symbol.upper()] = (price, ts)

    def update_dex_tick(self, symbol: str, price: float, timestamp_ms: Optional[float] = None) -> None:
        """Records zkLighter top-of-book tick."""
        ts = timestamp_ms / 1000.0 if timestamp_ms else time.time()
        self._dex_ticks[symbol.upper()] = (price, ts)

    def evaluate_arbitrage(self, symbol: str) -> Optional[LatencyArbSignal]:
        """
        Evaluates lead-lag price dislocation between CEX and zkLighter.
        """
        sym = symbol.upper()
        if sym not in self._cex_ticks or sym not in self._dex_ticks:
            return None

        cex_px, cex_ts = self._cex_ticks[sym]
        dex_px, dex_ts = self._dex_ticks[sym]

        now = time.time()
        latency_delta_ms = abs(cex_ts - dex_ts) * 1000.0

        if (now - cex_ts) * 1000.0 > self.max_acceptable_latency_ms:
            return None  # Stale tick

        spread_bps = ((cex_px - dex_px) / dex_px) * 10000.0
        abs_spread_bps = abs(spread_bps)

        if abs_spread_bps < self.min_dislocation_bps:
            return None

        # CEX leading higher -> Buy on DEX before it reprices up
        # CEX leading lower -> Sell on DEX before it reprices down
        action_side = "BUY/LONG" if spread_bps > 0 else "SELL/SHORT"
        net_profit_bps = abs_spread_bps - self.estimated_taker_fee_bps

        sig = LatencyArbSignal(
            symbol=sym,
            cex_price=cex_px,
            dex_price=dex_px,
            spread_bps=round(spread_bps, 1),
            action_side=action_side,
            latency_delta_ms=round(latency_delta_ms, 1),
            is_actionable=True,
            expected_net_profit_bps=round(net_profit_bps, 1),
        )

        logger.info("⚡ [Latency Arb] %s %s Signal: Spread %.1fbps (CEX: $%.2f | DEX: $%.2f | Lead: %.1fms)", sym, action_side, abs_spread_bps, cex_px, dex_px, latency_delta_ms)
        return sig
