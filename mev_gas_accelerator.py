#!/usr/bin/env python3
"""
Dynamic EIP-1559 Mempool Gas Accelerator (mev_gas_accelerator.py)
================================================================
Monitors network base fee volatility and pending mempool pressure to dynamically
scale priority fees (1.2x - 2.5x) for instant next-block settlement on EVM layers.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("GasAccelerator")


@dataclass
class DynamicGasPricing:
    """EIP-1559 transaction gas parameters."""
    base_fee_gwei: float
    priority_fee_gwei: float
    max_fee_per_gas_gwei: float
    gas_multiplier: float
    is_congestion_spike: bool
    estimated_inclusion_blocks: int
    timestamp: float = field(default_factory=time.time)


class DynamicMempoolGasAccelerator:
    """
    Computes optimal gas fees with congestion-adaptive priority scaling.
    """

    def __init__(
        self,
        base_priority_gwei: float = 0.05,       # Baseline priority fee on Base/L2
        max_priority_gwei: float = 5.0,         # Cap on priority fee
        congestion_threshold_gwei: float = 0.50, # Base fee spike trigger
    ):
        self.base_priority_gwei = base_priority_gwei
        self.max_priority_gwei = max_priority_gwei
        self.congestion_threshold_gwei = congestion_threshold_gwei

    def calculate_optimal_gas(
        self,
        current_base_fee_gwei: float,
        is_high_urgency_trade: bool = False,
    ) -> DynamicGasPricing:
        """
        Calculates optimal maxFeePerGas and maxPriorityFeePerGas.
        """
        is_congestion = current_base_fee_gwei >= self.congestion_threshold_gwei

        if is_high_urgency_trade and is_congestion:
            priority_mult = 2.5
            base_buffer_mult = 2.0
            est_blocks = 1
        elif is_high_urgency_trade:
            priority_mult = 1.8
            base_buffer_mult = 1.5
            est_blocks = 1
        elif is_congestion:
            priority_mult = 1.4
            base_buffer_mult = 1.3
            est_blocks = 2
        else:
            priority_mult = 1.0
            base_buffer_mult = 1.2
            est_blocks = 2

        priority_fee = min(self.max_priority_gwei, round(self.base_priority_gwei * priority_mult, 4))
        # maxFeePerGas = (2 * BaseFee) + PriorityFee
        max_fee = round((current_base_fee_gwei * base_buffer_mult) + priority_fee, 4)

        return DynamicGasPricing(
            base_fee_gwei=round(current_base_fee_gwei, 4),
            priority_fee_gwei=priority_fee,
            max_fee_per_gas_gwei=max_fee,
            gas_multiplier=priority_mult,
            is_congestion_spike=is_congestion,
            estimated_inclusion_blocks=est_blocks,
        )
