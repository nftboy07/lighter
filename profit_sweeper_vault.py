#!/usr/bin/env python3
"""
Dynamic Bankroll Compounding & Profit Sweeper Vault (profit_sweeper_vault.py)
=============================================================================
Manages dynamic account compounding and automatic profit sweeps to a secure
treasury subaccount or cold vault when high-water mark profit thresholds are met.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("ProfitSweeperVault")


@dataclass
class SweepRecord:
    """Audit record of an executed profit sweep."""
    sweep_id: str
    amount_usd: float
    from_account_index: int
    to_account_index: int
    trigger_reason: str
    timestamp: float = field(default_factory=time.time)


class ProfitSweeperVaultManager:
    """
    Monitors account equity growth, scales Kelly margin dynamically, and sweeps
    excess profits to safeguard realized gains.
    """

    def __init__(
        self,
        base_target_capital_usd: float = 500.0,
        profit_sweep_threshold_pct: float = 20.0,  # Sweep when equity >= base * 1.20
        sweep_retention_pct: float = 80.0,          # Keep 80% of excess, sweep 20%
        min_sweep_usd: float = 25.0,
        treasury_account_index: int = 281474976497686,
    ):
        self.base_target_capital_usd = base_target_capital_usd
        self.profit_sweep_threshold_pct = profit_sweep_threshold_pct
        self.sweep_retention_pct = sweep_retention_pct
        self.min_sweep_usd = min_sweep_usd
        self.treasury_account_index = treasury_account_index

        self.high_water_mark_usd = base_target_capital_usd
        self.sweep_history: List[SweepRecord] = []

    def calculate_compound_multiplier(self, current_equity_usd: float) -> float:
        """
        Calculates position sizing multiplier based on bankroll growth:
        - Equity >= 2x base -> 1.5x sizing multiplier
        - Equity >= 1.5x base -> 1.25x sizing multiplier
        - Equity <= 0.8x base -> 0.75x defensive sizing multiplier
        """
        if self.base_target_capital_usd <= 0 or current_equity_usd <= 0:
            return 1.0
        ratio = current_equity_usd / self.base_target_capital_usd
        if ratio >= 2.0:
            return 1.50
        elif ratio >= 1.5:
            return 1.25
        elif ratio <= 0.8:
            return 0.75
        return 1.0

    def evaluate_profit_sweep(
        self,
        current_equity_usd: float,
        from_account_index: int = 737649,
    ) -> Optional[SweepRecord]:
        """
        Evaluates whether current account equity qualifies for an automated profit sweep.
        """
        if current_equity_usd > self.high_water_mark_usd:
            self.high_water_mark_usd = current_equity_usd

        profit_threshold_usd = self.base_target_capital_usd * (1.0 + self.profit_sweep_threshold_pct / 100.0)
        if current_equity_usd < profit_threshold_usd:
            return None

        excess_profit = current_equity_usd - self.base_target_capital_usd
        sweep_amount = excess_profit * (1.0 - self.sweep_retention_pct / 100.0)

        if sweep_amount < self.min_sweep_usd:
            return None

        sweep_id = f"sweep_{int(time.time()*1000)}"
        record = SweepRecord(
            sweep_id=sweep_id,
            amount_usd=round(sweep_amount, 2),
            from_account_index=from_account_index,
            to_account_index=self.treasury_account_index,
            trigger_reason=f"Account equity (${current_equity_usd:,.2f}) exceeded +{self.profit_sweep_threshold_pct:.0f}% threshold (${profit_threshold_usd:,.2f})",
        )
        self.sweep_history.append(record)
        logger.info("💰 [ProfitSweeper] Generated sweep: %s ($%.2f from #%d to #%d)", sweep_id, sweep_amount, from_account_index, self.treasury_account_index)
        return record
