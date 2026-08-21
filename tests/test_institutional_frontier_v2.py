#!/usr/bin/env python3
"""
Unit and Integration Tests for Institutional Frontier Suite v2:
1. VPIN Real-Time Flow Toxicity Index (vpin_toxicity_analyzer.py)
2. Dynamic Funding Rate & Borrow Cost Yield Optimizer (funding_borrow_optimizer.py)
3. Orderbook Liquidity Cluster & Magnet Target Engine (orderbook_cluster_heatmap.py)
4. One-Tap Emergency Flash Evacuation Engine (emergency_evacuate.py)
================================================================================
"""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vpin_toxicity_analyzer import (
    VPINToxicityAnalyzer,
    VPINMetrics,
)
from funding_borrow_optimizer import (
    FundingBorrowYieldOptimizer,
    NetYieldOpportunity,
)
from orderbook_cluster_heatmap import (
    OrderbookClusterEngine,
    OrderbookClusterSummary,
)
from emergency_evacuate import (
    EmergencyFlashEvacuator,
    EvacuationAudit,
)


# =============================================================================
# 1. VPIN TOXICITY ANALYZER TESTS
# =============================================================================

def test_vpin_toxicity_analyzer():
    analyzer = VPINToxicityAnalyzer(bucket_size=10.0, num_buckets=5, toxicity_threshold=0.65)

    # 1. Balanced trading flow (Low VPIN)
    for _ in range(5):
        analyzer.record_trade("ETH", price=2000.0, size=5.0, is_buy=True)
        analyzer.record_trade("ETH", price=2000.0, size=5.0, is_buy=False)

    metrics_clean = analyzer.calculate_vpin("ETH")
    assert metrics_clean.symbol == "ETH"
    assert metrics_clean.is_toxic_flow is False
    assert metrics_clean.recommended_action == "NORMAL_QUOTING"

    # 2. Toxic one-sided dump (High VPIN)
    for _ in range(10):
        analyzer.record_trade("ETH", price=1980.0, size=10.0, is_buy=False)

    metrics_toxic = analyzer.calculate_vpin("ETH")
    assert metrics_toxic.is_toxic_flow is True
    assert metrics_toxic.recommended_action == "PULL_QUOTES"


# =============================================================================
# 2. FUNDING & BORROW YIELD OPTIMIZER TESTS
# =============================================================================

def test_funding_borrow_optimizer():
    optimizer = FundingBorrowYieldOptimizer(min_actionable_net_apr=20.0)

    # High funding rate (+0.04% per 8 hours -> ~43.8% APR) with 4.5% Aave borrow
    opp = optimizer.evaluate_net_yield(
        symbol="SOL",
        perp_funding_8h_rate=0.0004,
        borrow_rate_annual_pct=4.5,
        available_collateral_usd=200.0,
    )

    assert opp.symbol == "SOL"
    assert opp.perp_funding_apr_pct > 40.0
    assert opp.net_spread_apr_pct > 35.0
    assert opp.is_actionable is True
    assert opp.recommended_collateral_usd == 100.0


# =============================================================================
# 3. ORDERBOOK LIQUIDITY CLUSTER TESTS
# =============================================================================

def test_orderbook_cluster_engine():
    engine = OrderbookClusterEngine(cluster_bin_pct=0.50, min_cluster_usd=5000.0)

    bids = [(1990.0, 10.0), (1989.0, 15.0), (1988.0, 20.0)]   # ~$90k bids
    asks = [(2010.0, 10.0), (2011.0, 15.0), (2012.0, 20.0)]   # ~$90k asks

    summary = engine.cluster_orderbook(symbol="ETH", bids=bids, asks=asks, mid_price=2000.0)

    assert summary.symbol == "ETH"
    assert len(summary.bid_clusters) >= 1
    assert len(summary.ask_clusters) >= 1
    assert summary.recommended_long_tp_price > 2000.0
    assert summary.recommended_short_tp_price < 2000.0


# =============================================================================
# 4. EMERGENCY FLASH EVACUATOR TESTS
# =============================================================================

@pytest.mark.asyncio
async def test_emergency_flash_evacuator():
    evacuator = EmergencyFlashEvacuator(master_wallet_address="0x5cE95F8F7594c082549B34A32c26f4bf2F1bcFe9")

    audit = await evacuator.execute_emergency_evacuation(
        active_positions_count=3,
        open_orders_count=8,
        total_collateral_usd=500.0,
        is_paper=True,
    )

    assert audit.orders_cancelled_count == 8
    assert audit.positions_flattened_count == 3
    assert audit.total_swept_usd == 500.0
    assert audit.status == "COMPLETED_SUCCESSFULLY"
    assert audit.execution_time_ms < 100.0  # Completed in sub-100ms
