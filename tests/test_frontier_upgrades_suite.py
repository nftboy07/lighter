#!/usr/bin/env python3
"""
Unit and Integration Tests for:
1. AI Genetic Strategy & Parameter Auto-Tuner (genetic_optimizer.py)
2. Cross-DEX Smart Order Router (smart_order_router.py)
3. Dynamic EIP-1559 Mempool Gas Accelerator (mev_gas_accelerator.py)
===================================================================
"""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from genetic_optimizer import (
    GeneticStrategyOptimizer,
    StrategyGenome,
)
from smart_order_router import (
    CrossDEXSmartOrderRouter,
    RoutedOrderPlan,
)
from mev_gas_accelerator import (
    DynamicMempoolGasAccelerator,
    DynamicGasPricing,
)


# =============================================================================
# 1. GENETIC OPTIMIZER TESTS
# =============================================================================

def test_genetic_strategy_optimizer():
    optimizer = GeneticStrategyOptimizer(population_size=10, generations=2)
    assert len(optimizer.population) == 10

    # Simulate 20 trades with positive edge
    pnls = [15.2, -4.1, 22.0, -5.0, 18.5, 12.0, -3.2, 9.8, 30.1, -6.0]
    best_genome = optimizer.evolve_generation(pnls)

    assert best_genome.fitness_score > 0.0
    assert best_genome.tp_pct >= 1.5
    assert best_genome.sl_pct >= 0.8


# =============================================================================
# 2. SMART ORDER ROUTER TESTS
# =============================================================================

def test_smart_order_router():
    router = CrossDEXSmartOrderRouter()

    depths = {
        "zkLighter": {"best_price": 2000.0, "available_usd": 5000.0},
        "Hyperliquid": {"best_price": 2000.2, "available_usd": 3000.0},
        "Binance": {"best_price": 2000.1, "available_usd": 2000.0},
    }

    # Route $10,000 USD order
    plan = router.route_order(
        symbol="ETH",
        side="BUY/LONG",
        total_notional_usd=10000.0,
        venue_depths=depths,
    )

    assert plan.symbol == "ETH"
    assert len(plan.slices) == 3
    assert plan.total_notional_usd == 10000.0
    assert plan.weighted_avg_price > 1999.0


# =============================================================================
# 3. MEV GAS ACCELERATOR TESTS
# =============================================================================

def test_mev_gas_accelerator():
    accelerator = DynamicMempoolGasAccelerator(
        base_priority_gwei=0.05,
        congestion_threshold_gwei=0.50,
    )

    # 1. Normal low traffic
    gas_normal = accelerator.calculate_optimal_gas(current_base_fee_gwei=0.10, is_high_urgency_trade=False)
    assert gas_normal.is_congestion_spike is False
    assert gas_normal.priority_fee_gwei == 0.05

    # 2. High Urgency + Congestion Spike
    gas_spike = accelerator.calculate_optimal_gas(current_base_fee_gwei=1.20, is_high_urgency_trade=True)
    assert gas_spike.is_congestion_spike is True
    assert gas_spike.gas_multiplier == 2.5
    assert gas_spike.estimated_inclusion_blocks == 1
