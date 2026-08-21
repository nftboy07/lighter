#!/usr/bin/env python3
"""
Unit and Integration Tests for Advanced Institutional Suite:
1. zkLighter Internal Spot vs Perp Basis Arbitrage (internal_basis_arbitrage.py)
2. Whale Liquidity Wall Shadowing & Structural Protection (whale_orderbook_shadow.py)
3. Self-Learning NLP Catalyst Engine with PnL Feedback Loop (self_learning_catalyst.py)
4. Dynamic Compounding & Profit Sweeper Vault (profit_sweeper_vault.py)
5. Multi-Node Failover & Redundant Watchdog (redundant_failover_node.py)
========================================================================================
"""

from __future__ import annotations

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from internal_basis_arbitrage import (
    BasisDirection,
    BasisOpportunity,
    InternalBasisArbitrageEngine,
)
from whale_orderbook_shadow import (
    WhaleOrderBookShadowEngine,
    WallSide,
)
from self_learning_catalyst import (
    SelfLearningCatalystEngine,
    TradeOutcome,
)
from profit_sweeper_vault import (
    ProfitSweeperVaultManager,
)
from redundant_failover_node import (
    NodeHeartbeat,
    NodeRole,
    RedundantFailoverManager,
)


# =============================================================================
# 1. INTERNAL BASIS ARBITRAGE TESTS
# =============================================================================

def test_basis_arbitrage_perp_premium_detection():
    engine = InternalBasisArbitrageEngine(min_basis_spread_bps=15.0, estimated_fee_bps=2.0)
    # Spot: 2000.0 bid / 2000.20 ask
    engine.update_spot_book("ETH", bid=2000.0, ask=2000.20)
    # Perp: 2005.0 bid / 2005.20 ask (Perp premium of ~24 bps)
    engine.update_perp_book("ETH", bid=2005.0, ask=2005.20)

    opp = engine.evaluate_opportunity("ETH")
    assert opp is not None
    assert opp.direction == BasisDirection.BUY_SPOT_SELL_PERP
    assert opp.basis_spread_bps > 20.0
    assert opp.net_edge_bps >= 15.0
    assert opp.is_actionable is True


def test_basis_arbitrage_perp_discount_detection():
    engine = InternalBasisArbitrageEngine(min_basis_spread_bps=15.0, estimated_fee_bps=2.0)
    # Spot: 2005.0 bid / 2005.20 ask
    engine.update_spot_book("ETH", bid=2005.0, ask=2005.20)
    # Perp: 2000.0 bid / 2000.20 ask (Perp discount of ~24 bps)
    engine.update_perp_book("ETH", bid=2000.0, ask=2000.20)

    opp = engine.evaluate_opportunity("ETH")
    assert opp is not None
    assert opp.direction == BasisDirection.BUY_PERP_SELL_SPOT
    assert opp.basis_spread_bps > 20.0
    assert opp.net_edge_bps >= 15.0


def test_basis_arbitrage_unwind_on_convergence():
    engine = InternalBasisArbitrageEngine(unwind_spread_bps=3.0)
    engine.update_spot_book("ETH", bid=2000.0, ask=2000.20)
    engine.update_perp_book("ETH", bid=2005.0, ask=2005.20)

    opp = engine.evaluate_opportunity("ETH")
    assert opp is not None
    pos = engine.open_position(opp)
    assert pos is not None

    # Prices converge: Spot 2002.50, Perp 2002.60 (spread < 3 bps)
    engine.update_spot_book("ETH", bid=2002.40, ask=2002.60)
    engine.update_perp_book("ETH", bid=2002.50, ask=2002.70)

    should_unwind, reason, pnl = engine.should_unwind_position(pos)
    assert should_unwind is True
    assert reason == "SPREAD_CONVERGED"
    assert pnl > 0.0

    closed = engine.close_position(pos.position_id, realized_pnl=pnl)
    assert closed is not None
    assert closed.status == "CLOSED"


# =============================================================================
# 2. WHALE LIQUIDITY WALL SHADOW TESTS
# =============================================================================

def test_whale_wall_detection_and_front_run_setup():
    engine = WhaleOrderBookShadowEngine(
        min_wall_usd=20000.0,
        min_wall_duration_sec=1.0,
        front_run_ticks=1,
        stop_cushion_pct=0.20,
    )

    now = 1000.0
    # Place a $100,000 bid wall at $2000.0 (50 ETH)
    bids = [(2000.0, 50.0), (1995.0, 5.0)]
    asks = [(2005.0, 2.0)]

    # 1st scan: first seen
    setups1 = engine.scan_orderbook("ETH", bids, asks, tick_size=0.01, now=now)
    assert len(setups1) == 0  # Unconfirmed wall on first frame

    # 2nd scan after 1.5s: wall confirmed
    setups2 = engine.scan_orderbook("ETH", bids, asks, tick_size=0.01, now=now + 1.5)
    assert len(setups2) == 1

    setup = setups2[0]
    assert setup.action == "BUY/LONG"
    assert setup.entry_price == 2000.01  # 1 tick ahead
    assert setup.stop_loss_price < 2000.0  # cushioned behind wall
    assert setup.risk_reward_ratio > 3.0


# =============================================================================
# 3. SELF-LEARNING CATALYST ENGINE TESTS
# =============================================================================

def test_self_learning_catalyst_weight_reinforcement(tmp_path):
    temp_file = str(tmp_path / "test_learning.json")
    engine = SelfLearningCatalystEngine(persistence_file=temp_file)

    base_conv = 0.60
    adj_initial = engine.get_adjusted_conviction(base_conv, "bloomberg", ["etf", "approval"])
    assert adj_initial > base_conv  # Bloomberg has high initial weight

    # Record winning trade
    outcome = TradeOutcome(
        trade_id="t1",
        source="bloomberg",
        headline="SEC Approves Spot Ethereum ETF",
        keywords=["etf", "approval"],
        sentiment="BULLISH",
        initial_conviction=0.60,
        realized_pnl_usd=50.0,
        pnl_pct=3.5,
        mfe_pct=4.2,
        mae_pct=0.2,
    )
    engine.record_trade_outcome(outcome)

    adj_boosted = engine.get_adjusted_conviction(base_conv, "bloomberg", ["etf", "approval"])
    assert adj_boosted > adj_initial


# =============================================================================
# 4. PROFIT SWEEPER VAULT TESTS
# =============================================================================

def test_profit_sweeper_and_compound_multipliers():
    vault = ProfitSweeperVaultManager(
        base_target_capital_usd=500.0,
        profit_sweep_threshold_pct=20.0,  # $600 threshold
        sweep_retention_pct=80.0,
        min_sweep_usd=10.0,
    )

    # 1. Compounding sizing
    assert vault.calculate_compound_multiplier(500.0) == 1.0
    assert vault.calculate_compound_multiplier(1000.0) == 1.50
    assert vault.calculate_compound_multiplier(350.0) == 0.75

    # 2. Sweep evaluation under threshold
    assert vault.evaluate_profit_sweep(550.0) is None

    # 3. Sweep evaluation above threshold ($700 equity -> $200 profit -> $40 swept)
    sweep = vault.evaluate_profit_sweep(700.0)
    assert sweep is not None
    assert sweep.amount_usd == 40.0
    assert sweep.from_account_index == 737649


# =============================================================================
# 5. REDUNDANT FAILOVER NODE TESTS
# =============================================================================

def test_redundant_failover_election():
    mgr = RedundantFailoverManager(
        node_id="standby_node",
        initial_role=NodeRole.STANDBY,
        heartbeat_timeout_sec=3.0,
    )

    now = 1000.0
    hb = NodeHeartbeat(
        node_id="primary_node",
        role=NodeRole.PRIMARY,
        last_ping_time=now,
        active_positions_count=2,
        open_orders_count=4,
        rpc_latency_ms=45.0,
        is_healthy=True,
    )
    mgr.receive_heartbeat(hb)

    # Within timeout window -> healthy
    should_failover, reason = mgr.check_failover_condition(now=now + 1.5)
    assert should_failover is False
    assert reason == "HEALTHY"

    # Beyond timeout window -> trigger failover
    should_failover, reason = mgr.check_failover_condition(now=now + 4.0)
    assert should_failover is True
    assert "timeout" in reason

    mgr.promote_to_active(reason)
    assert mgr.current_role == NodeRole.FAILOVER_ACTIVE
