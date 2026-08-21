#!/usr/bin/env python3
"""
Microsecond Order Flow Imbalance (OFI) Fill Predictor (order_flow_imbalance_engine.py)
=====================================================================================
Calculates high-frequency Order Flow Imbalance (OFI) from top-of-book depth updates
using the Cont, Kukanov & Stoikov (2014) microstructure formula:
OFI_t = I_{P_t^b >= P_{t-1}^b} * q_t^b - I_{P_t^b <= P_{t-1}^b} * q_{t-1}^b
      - I_{P_t^a <= P_{t-1}^a} * q_t^a + I_{P_t^a >= P_{t-1}^a} * q_{t-1}^a

Derives real-time maker order fill probabilities and adverse selection risk.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("OFIFillPredictor")


@dataclass
class TopOfBookSnapshot:
    """Best bid/ask price and size state."""
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class OFIPrediction:
    """Real-time OFI score and fill probability forecast."""
    symbol: str
    current_ofi_score: float           # Positive = Buying pressure, Negative = Selling pressure
    cumulative_ofi_z: float            # Normalized OFI Z-score
    bid_fill_probability_pct: float    # Probability that a maker bid gets filled in next 500ms
    ask_fill_probability_pct: float    # Probability that a maker ask gets filled in next 500ms
    recommended_skew: str              # "SKEW_BUY", "SKEW_SELL", "NEUTRAL"
    timestamp: float = field(default_factory=time.time)


class MicrosecondOFIPredictor:
    """
    Computes Order Flow Imbalance and Maker Limit Fill Probabilities.
    """

    def __init__(
        self,
        rolling_window_size: int = 50,
        z_threshold: float = 1.8,
    ):
        self.rolling_window_size = rolling_window_size
        self.z_threshold = z_threshold

        # Symbol -> previous TopOfBookSnapshot
        self._last_tob: Dict[str, TopOfBookSnapshot] = {}
        # Symbol -> deque of recent OFI values
        self._ofi_history: Dict[str, deque] = {}

    def update_orderbook_top(
        self,
        symbol: str,
        bid_price: float,
        bid_size: float,
        ask_price: float,
        ask_size: float,
    ) -> OFIPrediction:
        """
        Ingests a top-of-book delta and computes instant OFI.
        """
        sym = symbol.upper()
        curr = TopOfBookSnapshot(bid_price, bid_size, ask_price, ask_size)

        if sym not in self._last_tob:
            self._last_tob[sym] = curr
            self._ofi_history[sym] = deque(maxlen=self.rolling_window_size)
            return OFIPrediction(
                symbol=sym,
                current_ofi_score=0.0,
                cumulative_ofi_z=0.0,
                bid_fill_probability_pct=50.0,
                ask_fill_probability_pct=50.0,
                recommended_skew="NEUTRAL",
            )

        prev = self._last_tob[sym]
        self._last_tob[sym] = curr

        # Cont, Kukanov & Stoikov (2014) OFI Formula
        # Bid delta
        if curr.bid_price > prev.bid_price:
            delta_bid = curr.bid_size
        elif curr.bid_price == prev.bid_price:
            delta_bid = curr.bid_size - prev.bid_size
        else:
            delta_bid = -prev.bid_size

        # Ask delta
        if curr.ask_price < prev.ask_price:
            delta_ask = curr.ask_size
        elif curr.ask_price == prev.ask_price:
            delta_ask = curr.ask_size - prev.ask_size
        else:
            delta_ask = -prev.ask_size

        ofi = delta_bid - delta_ask
        self._ofi_history[sym].append(ofi)

        history = list(self._ofi_history[sym])
        if len(history) >= 2:
            mean_ofi = sum(history) / len(history)
            var_ofi = sum((x - mean_ofi) ** 2 for x in history) / (len(history) - 1)
            std_ofi = math.sqrt(var_ofi) if var_ofi > 0 else 1.0
            ofi_z = (ofi - mean_ofi) / std_ofi
        else:
            ofi_z = ofi / 10.0 if ofi != 0 else 0.0

        # Logistic mapping to fill probability
        # High OFI (positive) means aggressive buyers are eating the asks -> Ask fill prob is high!
        ask_fill_prob = round(100.0 / (1.0 + math.exp(-0.8 * ofi_z)), 1)
        bid_fill_prob = round(100.0 - ask_fill_prob, 1)

        if ofi_z >= self.z_threshold:
            skew = "SKEW_BUY"   # Skew higher to capture upward momentum
        elif ofi_z <= -self.z_threshold:
            skew = "SKEW_SELL"  # Skew lower
        else:
            skew = "NEUTRAL"

        return OFIPrediction(
            symbol=sym,
            current_ofi_score=round(ofi, 2),
            cumulative_ofi_z=round(ofi_z, 2),
            bid_fill_probability_pct=bid_fill_prob,
            ask_fill_probability_pct=ask_fill_prob,
            recommended_skew=skew,
        )
