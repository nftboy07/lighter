#!/usr/bin/env python3
"""
Autonomous Hourly Profit-Harvesting Daemon (profit_harvesting_daemon.py)
=======================================================================
Runs in the background, autonomously monitoring realized profits across strategy shards
and sweeping excess gains to the Treasury Subaccount (#281474976497686) via on-chain transfer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ProfitHarvestingDaemon")


@dataclass
class HarvestExecution:
    """Audit record of an automated profit harvest."""
    harvest_id: str
    from_account_index: int
    to_account_index: int
    harvested_usd: float
    retained_usd: float
    tx_hash: str
    status: str
    timestamp: float = field(default_factory=time.time)


class AutonomousProfitHarvestingDaemon:
    """
    Autonomous hourly background worker that locks in profits without user intervention.
    """

    def __init__(
        self,
        subaccount_manager: Any,
        harvest_interval_seconds: float = 3600.0,  # Run every hour
        profit_threshold_pct: float = 15.0,        # Harvest when profit >= 15%
        treasury_account_index: int = 281474976497686,
        min_harvest_usd: float = 10.0,
    ):
        self.subaccount_manager = subaccount_manager
        self.harvest_interval_seconds = harvest_interval_seconds
        self.profit_threshold_pct = profit_threshold_pct
        self.treasury_account_index = treasury_account_index
        self.min_harvest_usd = min_harvest_usd

        self.last_run_time: float = 0.0
        self.harvest_history: List[HarvestExecution] = []
        self.baseline_collateral: Dict[int, float] = {}

    def set_baseline(self, account_index: int, amount_usd: float) -> None:
        """Sets the baseline capital benchmark for an account."""
        self.baseline_collateral[account_index] = max(1.0, amount_usd)

    async def run_harvest_cycle(self, is_paper: bool = False) -> List[HarvestExecution]:
        """
        Evaluates Sniper (#737649) and MM (#281474976497685) shards for harvestable profits.
        """
        self.last_run_time = time.time()
        executions: List[HarvestExecution] = []

        shards_to_check = [737649, 281474976497685]
        for acc_idx in shards_to_check:
            st = self.subaccount_manager.get_state(acc_idx)
            if not st:
                continue

            current_collat = st.collateral_usd
            baseline = self.baseline_collateral.get(acc_idx, current_collat)

            if current_collat > baseline:
                excess = current_collat - baseline
                excess_pct = (excess / baseline) * 100.0

                if excess_pct >= self.profit_threshold_pct and excess >= self.min_harvest_usd:
                    harvest_amount = round(excess * 0.50, 2)  # Harvest 50% of profit, retain 50% for compounding
                    retained_amount = round(current_collat - harvest_amount, 2)

                    # Execute transfer
                    res = await self.subaccount_manager.transfer_collateral(
                        from_account_index=acc_idx,
                        to_account_index=self.treasury_account_index,
                        amount_usd=harvest_amount,
                        is_paper=is_paper,
                    )

                    if res.get("success"):
                        harvest_id = f"harvest_{acc_idx}_{int(time.time()*1000)}"
                        rec = HarvestExecution(
                            harvest_id=harvest_id,
                            from_account_index=acc_idx,
                            to_account_index=self.treasury_account_index,
                            harvested_usd=harvest_amount,
                            retained_usd=retained_amount,
                            tx_hash=str(res.get("tx_hash", "0xsim")),
                            status="COMPLETED",
                        )
                        self.harvest_history.append(rec)
                        executions.append(rec)
                        # Reset baseline upward
                        self.baseline_collateral[acc_idx] = retained_amount
                        logger.info("💰 [HarvestDaemon] Swept $%.2f from #%d to Treasury #%d. New baseline: $%.2f", harvest_amount, acc_idx, self.treasury_account_index, retained_amount)

        return executions
