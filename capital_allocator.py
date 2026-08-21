#!/usr/bin/env python3
"""
Capital Growth Dynamic Deleveraging & Scaling Curve (capital_allocator.py)
========================================================================
Milestone-based dynamic capital scaling that automatically adjusts leverage caps
and allocation ratios across Subaccounts (#737649, #281474976497685, #281474976497686)
as portfolio equity grows from $5 -> $500 -> $5,000+.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("CapitalAllocator")


@dataclass
class CapitalTier:
    """Risk tier parameters defined by total account equity."""
    tier_name: str
    min_equity_usd: float
    max_equity_usd: float
    max_sniper_leverage: float
    max_mm_leverage: float
    max_arb_leverage: float
    target_sniper_pct: float
    target_mm_pct: float
    target_arb_pct: float
    description: str


class CapitalGrowthAllocator:
    """
    Dynamic capital curve adjusting leverage and risk as principal scales.
    """

    TIERS = [
        CapitalTier(
            tier_name="MICRO_BOOTSTRAP",
            min_equity_usd=0.0,
            max_equity_usd=100.0,
            max_sniper_leverage=5.0,
            max_mm_leverage=3.0,
            max_arb_leverage=2.0,
            target_sniper_pct=50.0,
            target_mm_pct=35.0,
            target_arb_pct=15.0,
            description="Aggressive bootstrapping for small capital accounts (<$100).",
        ),
        CapitalTier(
            tier_name="GROWTH_COMPOUNDER",
            min_equity_usd=100.0,
            max_equity_usd=1000.0,
            max_sniper_leverage=3.5,
            max_mm_leverage=2.0,
            max_arb_leverage=1.5,
            target_sniper_pct=40.0,
            target_mm_pct=40.0,
            target_arb_pct=20.0,
            description="Balanced geometric growth and volume points farming ($100-$1k).",
        ),
        CapitalTier(
            tier_name="INSTITUTIONAL_VAULT",
            min_equity_usd=1000.0,
            max_equity_usd=10000000.0,
            max_sniper_leverage=2.0,
            max_mm_leverage=1.25,
            max_arb_leverage=1.0,
            target_sniper_pct=30.0,
            target_mm_pct=40.0,
            target_arb_pct=30.0,
            description="Principal preservation, low leverage, and delta-neutral yield (>$1k).",
        ),
    ]

    def __init__(self, current_total_equity_usd: float = 5.52):
        self.current_total_equity_usd = current_total_equity_usd

    def get_current_tier(self, total_equity_usd: Optional[float] = None) -> CapitalTier:
        """Resolves current risk tier based on equity."""
        eq = total_equity_usd if total_equity_usd is not None else self.current_total_equity_usd
        for tier in self.TIERS:
            if tier.min_equity_usd <= eq < tier.max_equity_usd:
                return tier
        return self.TIERS[-1]

    def compute_shard_allocations(self, total_equity_usd: float) -> Dict[str, Any]:
        """
        Calculates dollar allocation and max allowable leverage for each shard.
        """
        tier = self.get_current_tier(total_equity_usd)

        sniper_usd = (total_equity_usd * tier.target_sniper_pct) / 100.0
        mm_usd = (total_equity_usd * tier.target_mm_pct) / 100.0
        arb_usd = (total_equity_usd * tier.target_arb_pct) / 100.0

        return {
            "total_equity_usd": round(total_equity_usd, 2),
            "tier_name": tier.tier_name,
            "description": tier.description,
            "shards": {
                "sniper_shard_737649": {
                    "allocated_usd": round(sniper_usd, 2),
                    "allocation_pct": tier.target_sniper_pct,
                    "max_leverage": tier.max_sniper_leverage,
                },
                "mm_shard_281474976497685": {
                    "allocated_usd": round(mm_usd, 2),
                    "allocation_pct": tier.target_mm_pct,
                    "max_leverage": tier.max_mm_leverage,
                },
                "arb_shard_281474976497686": {
                    "allocated_usd": round(arb_usd, 2),
                    "allocation_pct": tier.target_arb_pct,
                    "max_leverage": tier.max_arb_leverage,
                },
            },
        }
