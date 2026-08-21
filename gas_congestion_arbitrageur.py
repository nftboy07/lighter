#!/usr/bin/env python3
"""
L2 Rollup Batch Congestion & Gas Surge Arbitrageur (gas_congestion_arbitrageur.py)
================================================================================
Monitors L2 sequencer batch posting intervals and EIP-1559 base fee surges across
Arbitrum, Base, and zkLighter. Widens market maker quotes during micro-delays to
capture high-margin retail flow dislocations (+20-50 bps).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("GasCongestionArb")


@dataclass
class CongestionMetrics:
    """Consolidated L2 mempool and rollup sequencer metrics."""
    network: str
    current_gas_gwei: float
    baseline_gas_gwei: float
    gas_spike_ratio: float            # current / baseline
    is_congested: bool                # gas ratio >= 2.0x
    recommended_spread_multiplier: float # Multiplier to widen quotes (e.g. 1.5x - 2.5x)
    expected_extra_edge_bps: float
    timestamp: float = field(default_factory=time.time)


class L2GasCongestionArbitrageur:
    """
    Exploits rollup sequencer batch congestion and gas spikes.
    """

    def __init__(
        self,
        baseline_gas_gwei: float = 0.05,       # Baseline L2 gas (0.05 Gwei)
        congestion_spike_threshold: float = 2.0, # 2.0x spike = Congested
    ):
        self.baseline_gas_gwei = baseline_gas_gwei
        self.congestion_spike_threshold = congestion_spike_threshold

    def evaluate_congestion(
        self,
        network: str,
        current_gas_gwei: float,
    ) -> CongestionMetrics:
        """
        Calculates quote spread expansion recommendations during network congestion.
        """
        net = network.upper()
        gas_ratio = (current_gas_gwei / self.baseline_gas_gwei) if self.baseline_gas_gwei > 0 else 1.0
        is_congested = gas_ratio >= self.congestion_spike_threshold

        if is_congested:
            spread_mult = round(min(3.0, 1.0 + (gas_ratio - 1.0) * 0.5), 2)
            extra_edge_bps = round((spread_mult - 1.0) * 20.0, 1)
            logger.info("⚡ [L2 Congestion] %s Gas Surge: %.3f Gwei (%.1fx) -> Widening Spreads to %.2fx (+%.1f bps edge)", net, current_gas_gwei, gas_ratio, spread_mult, extra_edge_bps)
        else:
            spread_mult = 1.0
            extra_edge_bps = 0.0

        return CongestionMetrics(
            network=net,
            current_gas_gwei=round(current_gas_gwei, 4),
            baseline_gas_gwei=round(self.baseline_gas_gwei, 4),
            gas_spike_ratio=round(gas_ratio, 2),
            is_congested=is_congested,
            recommended_spread_multiplier=spread_mult,
            expected_extra_edge_bps=extra_edge_bps,
        )
