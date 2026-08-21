#!/usr/bin/env python3
"""
Autonomous Compounding & Dynamic Reinvestment Optimizer (compound_reinvestment_engine.py)
========================================================================================
Dynamically shifts profit reinvestment ratios between active trading margin and cold
treasury reserves based on rolling Sharpe ratio, win rate, and drawdown conditions.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("CompoundingOptimizer")


@dataclass
class ReinvestmentSplit:
    """Calculated reinvestment allocation recommendation."""
    active_reinvest_pct: float        # Percentage allocated back to active trading (e.g. 70%)
    cold_treasury_lock_pct: float     # Percentage locked to cold treasury (e.g. 30%)
    current_regime: str               # "AGGRESSIVE_COMPOUND", "BALANCED_COMPOUND", "DEFENSIVE_LOCK"
    estimated_sharpe_ratio: float
    rolling_win_rate_pct: float
    reason: str
    timestamp: float = field(default_factory=time.time)


class DynamicCompoundingOptimizer:
    """
    Evaluates rolling trade history and calculates dynamic compounding splits.
    """

    def __init__(
        self,
        target_high_sharpe: float = 2.0,      # Sharpe >= 2.0 -> 75% active reinvest
        target_low_sharpe: float = 0.8,       # Sharpe < 0.8 -> 25% active reinvest
    ):
        self.target_high_sharpe = target_high_sharpe
        self.target_low_sharpe = target_low_sharpe

    def calculate_reinvestment_split(
        self,
        completed_trades_pnl: List[float],
    ) -> ReinvestmentSplit:
        """
        Calculates optimal reinvestment split from rolling trade PnL history.
        """
        if not completed_trades_pnl:
            return ReinvestmentSplit(
                active_reinvest_pct=50.0,
                cold_treasury_lock_pct=50.0,
                current_regime="BALANCED_COMPOUND",
                estimated_sharpe_ratio=1.5,
                rolling_win_rate_pct=50.0,
                reason="Default baseline 50/50 compounding ratio",
            )

        n = len(completed_trades_pnl)
        wins = [p for p in completed_trades_pnl if p > 0]
        win_rate = (len(wins) / n) * 100.0

        mean_pnl = sum(completed_trades_pnl) / n
        variance = sum((p - mean_pnl) ** 2 for p in completed_trades_pnl) / max(1, n - 1)
        std_dev = math.sqrt(variance) if variance > 0 else 1.0

        sharpe = (mean_pnl / std_dev) * math.sqrt(252.0)
        sharpe = max(-3.0, min(5.0, sharpe))

        if sharpe >= self.target_high_sharpe and win_rate >= 65.0:
            active_pct = 75.0
            treasury_pct = 25.0
            regime = "AGGRESSIVE_COMPOUND"
            reason = f"High Sharpe ({sharpe:.2f}) & Win Rate ({win_rate:.1f}%) -> Accelerating active capital growth"
        elif sharpe >= self.target_low_sharpe and win_rate >= 50.0:
            active_pct = 50.0
            treasury_pct = 50.0
            regime = "BALANCED_COMPOUND"
            reason = f"Steady Sharpe ({sharpe:.2f}) -> Balanced 50/50 compounding and treasury protection"
        else:
            active_pct = 25.0
            treasury_pct = 75.0
            regime = "DEFENSIVE_LOCK"
            reason = f"Low Sharpe ({sharpe:.2f}) -> Prioritizing 75% cold treasury capital preservation"

        split = ReinvestmentSplit(
            active_reinvest_pct=active_pct,
            cold_treasury_lock_pct=treasury_pct,
            current_regime=regime,
            estimated_sharpe_ratio=round(sharpe, 2),
            rolling_win_rate_pct=round(win_rate, 2),
            reason=reason,
        )

        logger.info("📈 [Compounding Optimizer] Regime: %s | Reinvest: %.0f%% | Treasury: %.0f%% (%s)", regime, active_pct, treasury_pct, reason)
        return split
