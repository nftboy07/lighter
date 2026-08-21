#!/usr/bin/env python3
"""
Self-Learning NLP Catalyst Engine with PnL Feedback Loop (self_learning_catalyst.py)
===================================================================================
Applies reinforcement weighting to news sources, keyword tokens, and sentiment scores
based on real post-trade price follow-through and realized PnL.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("SelfLearningCatalyst")


@dataclass
class TradeOutcome:
    """Historical trade outcome used for reinforcement learning."""
    trade_id: str
    source: str
    headline: str
    keywords: List[str]
    sentiment: str
    initial_conviction: float
    realized_pnl_usd: float
    pnl_pct: float
    mfe_pct: float  # Maximum Favorable Excursion
    mae_pct: float  # Maximum Adverse Excursion
    timestamp: float = field(default_factory=time.time)


class SelfLearningCatalystEngine:
    """
    Tracks and updates historical win rates and token coefficients to dynamically
    scale future trade conviction.
    """

    def __init__(self, persistence_file: str = "catalyst_learning.json"):
        self.persistence_file = persistence_file
        self.source_weights: Dict[str, float] = {
            "treenews": 1.15,
            "bloomberg": 1.25,
            "reuters": 1.20,
            "coindesk": 0.95,
            "cointelegraph": 0.90,
            "twitter": 0.85,
        }
        self.keyword_multipliers: Dict[str, float] = {}
        self.trade_history: List[TradeOutcome] = []
        self._load_state()

    def _load_state(self) -> None:
        """Loads learned weights from disk if available."""
        if os.path.exists(self.persistence_file):
            try:
                with open(self.persistence_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.source_weights.update(data.get("source_weights", {}))
                    self.keyword_multipliers.update(data.get("keyword_multipliers", {}))
            except Exception as e:
                logger.warning("Failed to load catalyst learning state: %s", e)

    def save_state(self) -> None:
        """Persists learned weights to disk."""
        try:
            with open(self.persistence_file, "w", encoding="utf-8") as f:
                json.dump({
                    "source_weights": self.source_weights,
                    "keyword_multipliers": self.keyword_multipliers,
                    "total_trades_analyzed": len(self.trade_history),
                    "updated_at": time.time(),
                }, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save catalyst learning state: %s", e)

    def get_adjusted_conviction(
        self,
        base_conviction: float,
        source: str,
        keywords: List[str],
    ) -> float:
        """
        Calculates optimized conviction score by applying learned source and token weights.
        """
        src = (source or "generic").lower()
        src_mult = self.source_weights.get(src, 1.0)

        # Average keyword weight
        kw_mults = [self.keyword_multipliers.get(kw.lower(), 1.0) for kw in keywords if kw]
        avg_kw_mult = (sum(kw_mults) / len(kw_mults)) if kw_mults else 1.0

        adjusted = base_conviction * src_mult * avg_kw_mult
        return round(max(0.10, min(0.99, adjusted)), 4)

    def record_trade_outcome(self, outcome: TradeOutcome) -> None:
        """
        Updates reinforcement learning weights after a trade closes.
        Positive PnL -> increases source & keyword weights.
        Negative PnL -> decreases source & keyword weights.
        """
        self.trade_history.append(outcome)
        is_win = outcome.realized_pnl_usd > 0 or outcome.pnl_pct > 0.1
        learning_rate = 0.03

        # 1. Update source weight
        src = (outcome.source or "generic").lower()
        curr_src = self.source_weights.get(src, 1.0)
        if is_win:
            new_src = curr_src + learning_rate * min(1.5, outcome.pnl_pct / 2.0)
        else:
            new_src = curr_src - learning_rate * min(1.5, abs(outcome.pnl_pct) / 2.0)
        self.source_weights[src] = round(max(0.50, min(2.0, new_src)), 4)

        # 2. Update keyword multipliers
        for kw in outcome.keywords:
            k = kw.lower()
            curr_kw = self.keyword_multipliers.get(k, 1.0)
            if is_win:
                new_kw = curr_kw + learning_rate * 0.5
            else:
                new_kw = curr_kw - learning_rate * 0.5
            self.keyword_multipliers[k] = round(max(0.50, min(2.0, new_kw)), 4)

        self.save_state()
        logger.info("🧠 [SelfLearning] Updated weights for source '%s' (weight=%.3f) across %d keywords", src, self.source_weights[src], len(outcome.keywords))
