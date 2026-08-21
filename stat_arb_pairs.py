#!/usr/bin/env python3
"""
Statistical Arbitrage & Cointegration Pair Trading Engine (stat_arb_pairs.py)
=============================================================================
Calculates rolling price ratios, cointegration mean & standard deviation,
and detects mean-reversion trading opportunities when Z-Score divergence >= 2.5 sigma.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("StatArbPairs")


class PairAction(str, Enum):
    LONG_A_SHORT_B = "LONG_A_SHORT_B"  # Ratio A/B is undervalued (Z <= -2.5) -> Long A, Short B
    SHORT_A_LONG_B = "SHORT_A_LONG_B"  # Ratio A/B is overvalued (Z >= +2.5) -> Short A, Long B


@dataclass(frozen=True)
class PairOpportunity:
    """Detected statistical arbitrage divergence."""
    pair_name: str  # e.g. "SOL/ETH"
    asset_a: str
    asset_b: str
    action: PairAction
    current_ratio: float
    rolling_mean: float
    rolling_std: float
    z_score: float
    entry_price_a: float
    entry_price_b: float
    notional_leg_usd: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class ActivePairPosition:
    """Tracks an open delta-neutral pair position."""
    position_id: str
    pair_name: str
    asset_a: str
    asset_b: str
    action: PairAction
    entry_ratio: float
    entry_z_score: float
    entry_price_a: float
    entry_price_b: float
    notional_usd: float
    opened_at: float = field(default_factory=time.time)
    status: str = "OPEN"
    realized_pnl_usd: float = 0.0


class StatisticalArbitragePairEngine:
    """
    Monitors pairs of correlated assets, calculates dynamic Z-Scores, and manages mean-reversion positions.
    """

    def __init__(
        self,
        lookback_periods: int = 60,
        entry_z_threshold: float = 2.5,
        exit_z_threshold: float = 0.5,
        default_leg_notional_usd: float = 200.0,
    ):
        self.lookback_periods = lookback_periods
        self.entry_z_threshold = entry_z_threshold
        self.exit_z_threshold = exit_z_threshold
        self.default_leg_notional_usd = default_leg_notional_usd

        # Ratio histories: pair_name -> deque of float ratios
        self.ratio_histories: Dict[str, deque] = {}
        self.active_pair_positions: Dict[str, ActivePairPosition] = {}

    def update_prices(
        self,
        asset_a: str,
        price_a: float,
        asset_b: str,
        price_b: float,
    ) -> Optional[PairOpportunity]:
        """
        Ingests latest prices for a pair and evaluates for statistical divergence.
        """
        if price_a <= 0 or price_b <= 0:
            return None

        pair_name = f"{asset_a.upper()}/{asset_b.upper()}"
        ratio = price_a / price_b

        if pair_name not in self.ratio_histories:
            self.ratio_histories[pair_name] = deque(maxlen=self.lookback_periods)

        history = self.ratio_histories[pair_name]
        history.append(ratio)

        if len(history) < 10:  # Need minimum sample size
            return None

        mean = sum(history) / len(history)
        variance = sum((r - mean) ** 2 for r in history) / len(history)
        std = math.sqrt(variance)

        if std <= 0.00001:
            return None

        z_score = (ratio - mean) / std

        # Check for trade opportunity
        if z_score <= -self.entry_z_threshold:
            return PairOpportunity(
                pair_name=pair_name,
                asset_a=asset_a.upper(),
                asset_b=asset_b.upper(),
                action=PairAction.LONG_A_SHORT_B,
                current_ratio=round(ratio, 6),
                rolling_mean=round(mean, 6),
                rolling_std=round(std, 6),
                z_score=round(z_score, 2),
                entry_price_a=price_a,
                entry_price_b=price_b,
                notional_leg_usd=self.default_leg_notional_usd,
            )
        elif z_score >= self.entry_z_threshold:
            return PairOpportunity(
                pair_name=pair_name,
                asset_a=asset_a.upper(),
                asset_b=asset_b.upper(),
                action=PairAction.SHORT_A_LONG_B,
                current_ratio=round(ratio, 6),
                rolling_mean=round(mean, 6),
                rolling_std=round(std, 6),
                z_score=round(z_score, 2),
                entry_price_a=price_a,
                entry_price_b=price_b,
                notional_leg_usd=self.default_leg_notional_usd,
            )

        return None

    def should_exit_pair(
        self,
        pos: ActivePairPosition,
        current_price_a: float,
        current_price_b: float,
    ) -> Tuple[bool, str, float]:
        """
        Checks if open pair position has reverted to mean.
        """
        if current_price_a <= 0 or current_price_b <= 0:
            return False, "INVALID_PRICES", 0.0

        pair_name = pos.pair_name
        history = self.ratio_histories.get(pair_name)
        if not history or len(history) < 10:
            return False, "INSUFFICIENT_HISTORY", 0.0

        current_ratio = current_price_a / current_price_b
        mean = sum(history) / len(history)
        std = math.sqrt(sum((r - mean) ** 2 for r in history) / len(history))

        if std <= 0.00001:
            return False, "LOW_VARIANCE", 0.0

        z_score = (current_ratio - mean) / std

        # Exit condition: Z-score converged to neutral band
        if abs(z_score) <= self.exit_z_threshold:
            # Estimate gross PnL
            ret_a = (current_price_a - pos.entry_price_a) / pos.entry_price_a
            ret_b = (current_price_b - pos.entry_price_b) / pos.entry_price_b
            if pos.action == PairAction.LONG_A_SHORT_B:
                pnl = (ret_a - ret_b) * pos.notional_usd
            else:
                pnl = (ret_b - ret_a) * pos.notional_usd
            return True, "MEAN_REVERTED", round(pnl, 4)

        return False, "HOLD", 0.0
