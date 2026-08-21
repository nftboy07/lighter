#!/usr/bin/env python3
"""
Institutional Execution Algorithms: TWAP & Iceberg Order Slicer (institutional_execution_algo.py)
=================================================================================================
Slices large institutional orders into randomized micro-lots across time (TWAP) or
disguised hidden tranches (Iceberg) to minimize market impact and eliminate front-running.
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("InstitutionalExecution")


class ExecutionAlgoType(str, Enum):
    TWAP = "TWAP"
    ICEBERG = "ICEBERG"


@dataclass
class SliceTranche:
    """Individual slice tranche ready for dispatch."""
    tranche_index: int
    scheduled_time: float
    size_base: float
    notional_usd: float
    status: str = "PENDING"  # PENDING, FILLED, SKIPPED
    filled_price: float = 0.0
    filled_time: float = 0.0


@dataclass
class ExecutionPlan:
    """Consolidated execution plan for an institutional sliced order."""
    plan_id: str
    algo_type: ExecutionAlgoType
    symbol: str
    side: str  # "BUY" or "SELL"
    total_notional_usd: float
    total_size_base: float
    total_tranches: int
    duration_seconds: float
    tranches: List[SliceTranche]
    created_at: float = field(default_factory=time.time)
    completed_tranches: int = 0
    total_filled_usd: float = 0.0
    is_active: bool = True

    @property
    def progress_pct(self) -> float:
        if self.total_tranches == 0:
            return 100.0
        return (self.completed_tranches / self.total_tranches) * 100.0


class InstitutionalExecutionEngine:
    """
    Slices large orders into randomized TWAP or Iceberg tranches.
    """

    def __init__(self, default_tranche_variance_pct: float = 0.15):
        self.default_tranche_variance_pct = default_tranche_variance_pct
        self.active_plans: Dict[str, ExecutionPlan] = {}

    def build_twap_plan(
        self,
        symbol: str,
        side: str,
        total_notional_usd: float,
        current_price: float,
        duration_seconds: float = 120.0,
        num_tranches: int = 6,
        now: Optional[float] = None,
    ) -> ExecutionPlan:
        """
        Constructs a Time-Weighted Average Price (TWAP) randomized schedule.
        """
        if current_price <= 0 or total_notional_usd <= 0 or num_tranches <= 0:
            raise ValueError("Invalid parameters for TWAP plan")

        ts = now if now is not None else time.time()
        plan_id = f"twap_{symbol.lower()}_{int(ts*1000)}"
        total_size = total_notional_usd / current_price
        base_interval = duration_seconds / num_tranches
        base_slice_notional = total_notional_usd / num_tranches

        tranches: List[SliceTranche] = []
        allocated_notional = 0.0

        for i in range(num_tranches):
            # Add subtle randomization (+/- 15%) to confuse MEV detectors
            if i < num_tranches - 1:
                variance = 1.0 + random.uniform(-self.default_tranche_variance_pct, self.default_tranche_variance_pct)
                slice_notional = round(base_slice_notional * variance, 2)
            else:
                slice_notional = round(total_notional_usd - allocated_notional, 2)

            allocated_notional += slice_notional
            slice_size = slice_notional / current_price
            sched_time = ts + (i * base_interval) + random.uniform(-1.0, 1.0)

            tranches.append(SliceTranche(
                tranche_index=i + 1,
                scheduled_time=round(sched_time, 2),
                size_base=round(slice_size, 6),
                notional_usd=slice_notional,
            ))

        plan = ExecutionPlan(
            plan_id=plan_id,
            algo_type=ExecutionAlgoType.TWAP,
            symbol=symbol.upper(),
            side=side.upper(),
            total_notional_usd=total_notional_usd,
            total_size_base=round(total_size, 6),
            total_tranches=num_tranches,
            duration_seconds=duration_seconds,
            tranches=tranches,
            created_at=ts,
        )
        self.active_plans[plan_id] = plan
        return plan

    def build_iceberg_plan(
        self,
        symbol: str,
        side: str,
        total_notional_usd: float,
        current_price: float,
        visible_display_pct: float = 20.0,  # Only display 20% at a time
        now: Optional[float] = None,
    ) -> ExecutionPlan:
        """
        Constructs an Iceberg execution plan with disguised visible depth.
        """
        if current_price <= 0 or total_notional_usd <= 0:
            raise ValueError("Invalid parameters for Iceberg plan")

        ts = now if now is not None else time.time()
        plan_id = f"iceberg_{symbol.lower()}_{int(ts*1000)}"
        total_size = total_notional_usd / current_price
        num_tranches = max(2, int(math.ceil(100.0 / visible_display_pct)))
        base_slice_notional = total_notional_usd / num_tranches

        tranches: List[SliceTranche] = []
        allocated_notional = 0.0

        for i in range(num_tranches):
            if i < num_tranches - 1:
                slice_notional = round(base_slice_notional, 2)
            else:
                slice_notional = round(total_notional_usd - allocated_notional, 2)

            allocated_notional += slice_notional
            slice_size = slice_notional / current_price

            tranches.append(SliceTranche(
                tranche_index=i + 1,
                scheduled_time=ts,  # Iceberg tranches trigger upon previous fill
                size_base=round(slice_size, 6),
                notional_usd=slice_notional,
            ))

        plan = ExecutionPlan(
            plan_id=plan_id,
            algo_type=ExecutionAlgoType.ICEBERG,
            symbol=symbol.upper(),
            side=side.upper(),
            total_notional_usd=total_notional_usd,
            total_size_base=round(total_size, 6),
            total_tranches=num_tranches,
            duration_seconds=0.0,
            tranches=tranches,
            created_at=ts,
        )
        self.active_plans[plan_id] = plan
        return plan

    def record_tranche_fill(
        self,
        plan_id: str,
        tranche_index: int,
        filled_price: float,
    ) -> Optional[SliceTranche]:
        """Marks a tranche as filled and updates plan progress."""
        plan = self.active_plans.get(plan_id)
        if not plan:
            return None

        for t in plan.tranches:
            if t.tranche_index == tranche_index and t.status == "PENDING":
                t.status = "FILLED"
                t.filled_price = filled_price
                t.filled_time = time.time()
                plan.completed_tranches += 1
                plan.total_filled_usd += t.notional_usd
                if plan.completed_tranches >= plan.total_tranches:
                    plan.is_active = False
                return t

        return None
