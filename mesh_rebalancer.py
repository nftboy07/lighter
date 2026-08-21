#!/usr/bin/env python3
"""
Autonomous Cross-Exchange & Subaccount Mesh Rebalancer (mesh_rebalancer.py)
===========================================================================
Monitors margin utilization across all 3 subaccount shards and external venues.
Automatically plans and executes optimal internal and cross-venue margin transfers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("MeshRebalancer")


@dataclass
class RebalanceTransferAction:
    """Planned collateral transfer action."""
    source_shard: str
    target_shard: str
    transfer_amount_usd: float
    reason: str
    is_executed: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class MeshBalanceStatus:
    """Consolidated state across all subaccount shards."""
    shard_balances: Dict[str, float]
    total_mesh_capital_usd: float
    max_imbalance_pct: float
    recommended_actions: List[RebalanceTransferAction]
    is_balanced: bool
    timestamp: float = field(default_factory=time.time)


class AutonomousMeshRebalancer:
    """
    Multi-Shard Collateral Balancing & Capital Mesh Router.
    """

    def __init__(
        self,
        target_allocation_ratios: Optional[Dict[str, float]] = None,
        imbalance_threshold_pct: float = 15.0, # Rebalance if shard deviates > 15% from target
    ):
        # Default allocation targets: 40% Sniper (#737649), 40% MM (#281474976497685), 20% Treasury (#281474976497686)
        self.target_allocation_ratios = target_allocation_ratios or {
            "Subaccount_737649_Sniper": 0.40,
            "Subaccount_MM": 0.40,
            "Subaccount_Treasury": 0.20,
        }
        self.imbalance_threshold_pct = imbalance_threshold_pct

    def evaluate_mesh(self, current_balances: Dict[str, float]) -> MeshBalanceStatus:
        """
        Calculates optimal transfer mesh to restore target capital distribution.
        """
        total_cap = sum(current_balances.values())
        if total_cap <= 0:
            return MeshBalanceStatus(
                shard_balances=current_balances,
                total_mesh_capital_usd=0.0,
                max_imbalance_pct=0.0,
                recommended_actions=[],
                is_balanced=True,
            )

        actions: List[RebalanceTransferAction] = []
        max_dev = 0.0

        surplus_shards: List[Tuple[str, float]] = []
        deficit_shards: List[Tuple[str, float]] = []

        for shard, target_ratio in self.target_allocation_ratios.items():
            current_bal = current_balances.get(shard, 0.0)
            target_bal = total_cap * target_ratio
            diff = current_bal - target_bal
            dev_pct = (abs(diff) / total_cap) * 100.0
            max_dev = max(max_dev, dev_pct)

            if diff > (total_cap * (self.imbalance_threshold_pct / 100.0)):
                surplus_shards.append((shard, diff))
            elif diff < -(total_cap * (self.imbalance_threshold_pct / 100.0)):
                deficit_shards.append((shard, abs(diff)))

        # Plan transfers from surplus to deficit shards
        for src, s_amt in surplus_shards:
            for dst, d_amt in deficit_shards:
                transfer_sz = round(min(s_amt, d_amt), 2)
                if transfer_sz >= 1.0:
                    actions.append(
                        RebalanceTransferAction(
                            source_shard=src,
                            target_shard=dst,
                            transfer_amount_usd=transfer_sz,
                            reason=f"Mesh rebalance to restore {dst} target ratio",
                        )
                    )

        is_balanced = len(actions) == 0

        status = MeshBalanceStatus(
            shard_balances={k: round(v, 2) for k, v in current_balances.items()},
            total_mesh_capital_usd=round(total_cap, 2),
            max_imbalance_pct=round(max_dev, 2),
            recommended_actions=actions,
            is_balanced=is_balanced,
        )

        return status
