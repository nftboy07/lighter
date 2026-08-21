#!/usr/bin/env python3
"""
VPIN Real-Time Flow Toxicity Index (vpin_toxicity_analyzer.py)
==============================================================
Calculates Volume-Synchronized Probability of Toxicity (VPIN) from microsecond
trade bars to detect informed adverse flow before sudden market dumps.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("VPINToxicity")


@dataclass
class VolumeBucket:
    """A volume-synchronized bucket of buy/sell volume."""
    bucket_id: int
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    total_volume: float = 0.0
    is_completed: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class VPINMetrics:
    """Real-time VPIN metrics for an individual market."""
    symbol: str
    vpin_score: float                  # 0.0 - 1.0 (Toxicity Index)
    is_toxic_flow: bool                # VPIN >= 0.65 (Informed dump/pump incoming)
    recommended_action: str            # "NORMAL_QUOTING", "WIDEN_SPREADS", "PULL_QUOTES"
    completed_buckets_count: int
    timestamp: float = field(default_factory=time.time)


class VPINToxicityAnalyzer:
    """
    Volume-Synchronized Probability of Toxicity Calculator.
    """

    def __init__(
        self,
        bucket_size: float = 50.0,            # Base asset units per volume bucket
        num_buckets: int = 20,                # Rolling window of buckets
        toxicity_threshold: float = 0.65,     # Threshold for toxic flow alert
        widen_threshold: float = 0.50,        # Threshold for widening spreads
    ):
        self.bucket_size = bucket_size
        self.num_buckets = num_buckets
        self.toxicity_threshold = toxicity_threshold
        self.widen_threshold = widen_threshold

        # Symbol -> deque of completed VolumeBucket
        self._buckets: Dict[str, deque] = {}
        self._current_bucket: Dict[str, VolumeBucket] = {}
        self._bucket_counter: Dict[str, int] = {}

    def record_trade(
        self,
        symbol: str,
        price: float,
        size: float,
        is_buy: bool,
    ) -> Optional[VPINMetrics]:
        """
        Ingests a single trade tick and accumulates volume into synchronized buckets.
        """
        sym = symbol.upper()
        if sym not in self._buckets:
            self._buckets[sym] = deque(maxlen=self.num_buckets)
            self._bucket_counter[sym] = 0
            self._current_bucket[sym] = VolumeBucket(bucket_id=0)

        curr = self._current_bucket[sym]
        remaining = size

        while remaining > 0:
            space = self.bucket_size - curr.total_volume
            fill = min(remaining, space)

            if is_buy:
                curr.buy_volume += fill
            else:
                curr.sell_volume += fill
            curr.total_volume += fill
            remaining -= fill

            if curr.total_volume >= self.bucket_size:
                curr.is_completed = True
                self._buckets[sym].append(curr)
                self._bucket_counter[sym] += 1
                curr = VolumeBucket(bucket_id=self._bucket_counter[sym])
                self._current_bucket[sym] = curr

        return self.calculate_vpin(sym)

    def calculate_vpin(self, symbol: str) -> VPINMetrics:
        """
        Calculates VPIN = sum(|V_tau^B - V_tau^S|) / (N * BucketSize).
        """
        sym = symbol.upper()
        history = self._buckets.get(sym, deque())

        if not history:
            return VPINMetrics(
                symbol=sym,
                vpin_score=0.20,
                is_toxic_flow=False,
                recommended_action="NORMAL_QUOTING",
                completed_buckets_count=0,
            )

        total_imbalance = sum(abs(b.buy_volume - b.sell_volume) for b in history)
        total_volume = sum(b.total_volume for b in history)

        vpin = (total_imbalance / total_volume) if total_volume > 0 else 0.20
        vpin = round(min(1.0, max(0.0, vpin)), 3)

        is_toxic = vpin >= self.toxicity_threshold
        if is_toxic:
            action = "PULL_QUOTES"
            logger.warning("🚨 [VPIN Alert] %s Toxicity Spiked to %.3f -> Triggering PULL_QUOTES", sym, vpin)
        elif vpin >= self.widen_threshold:
            action = "WIDEN_SPREADS"
        else:
            action = "NORMAL_QUOTING"

        return VPINMetrics(
            symbol=sym,
            vpin_score=vpin,
            is_toxic_flow=is_toxic,
            recommended_action=action,
            completed_buckets_count=len(history),
        )
