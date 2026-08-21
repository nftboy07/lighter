#!/usr/bin/env python3
"""
Unit and Integration Tests for:
1. WebSocket Auto-Healing & Zombie Watchdog (ws_auto_healing.py)
2. Autonomous Multi-Exchange Delta-Neutral Hedger (delta_hedger.py)
3. GARCH(1,1) Volatility Squeeze Forecaster (volatility_forecaster.py)
4. Capital Growth Dynamic Deleverager (capital_allocator.py)
===================================================================
"""

from __future__ import annotations

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ws_auto_healing import (
    WebSocketAutoHealingSupervisor,
    SocketHealthState,
)
from delta_hedger import (
    AutonomousDeltaHedger,
    HedgeOrderRecord,
)
from volatility_forecaster import (
    GARCHVolatilityForecaster,
    VolatilityForecast,
)
from capital_allocator import (
    CapitalGrowthAllocator,
    CapitalTier,
)


# =============================================================================
# 1. WEBSOCKET AUTO-HEALING TESTS
# =============================================================================

def test_ws_auto_healing_zombie_detection_and_failover():
    supervisor = WebSocketAutoHealingSupervisor(
        zombie_timeout_seconds=1.0,
        heartbeat_interval_seconds=0.2,
    )

    feed = supervisor.register_feed(
        feed_name="TreeNews",
        primary_url="wss://news.treeofalpha.com/ws",
        standby_url="wss://backup.treeofalpha.com/ws",
    )

    # Initial healthy state
    assert feed.health_state == SocketHealthState.HEALTHY
    assert feed.is_using_standby is False

    # Simulate 1.5s stall without packets -> Detects zombie and fails over
    state = supervisor.check_feed_health("TreeNews", current_time=time.time() + 1.5)
    assert state == SocketHealthState.FAILOVER
    assert feed.is_using_standby is True
    assert feed.failover_count == 1

    summary = supervisor.get_supervisor_summary()
    assert summary["total_failovers"] == 1


# =============================================================================
# 2. AUTONOMOUS DELTA HEDGER TESTS
# =============================================================================

def test_autonomous_delta_hedger():
    hedger = AutonomousDeltaHedger(
        max_unhedged_delta_usd=100.0,
        preferred_hedge_exchange="Hyperliquid",
    )

    # 1. Below threshold ($50 delta) -> No hedge needed
    hedger.update_inventory("ETH", net_base_qty=0.025)  # 0.025 ETH * $2000 = $50
    hedge1 = hedger.evaluate_hedge("ETH", current_mark_price=2000.0, is_paper=True)
    assert hedge1 is None

    # 2. Above threshold ($200 delta) -> Triggers SELL hedge on Hyperliquid
    hedger.update_inventory("ETH", net_base_qty=0.10)  # 0.10 ETH * $2000 = $200
    hedge2 = hedger.evaluate_hedge("ETH", current_mark_price=2000.0, is_paper=True)
    assert hedge2 is not None
    assert hedge2.hedge_side == "SELL/SHORT"
    assert hedge2.hedged_usd == 200.0
    assert hedger.inventory["ETH"] == 0.0  # Reset to delta neutral


# =============================================================================
# 3. GARCH VOLATILITY FORECASTER TESTS
# =============================================================================

def test_garch_volatility_forecaster():
    forecaster = GARCHVolatilityForecaster(squeeze_threshold_pct=15.0, expansion_threshold_pct=60.0)

    # Ingest 10 tight candles
    p = 2000.0
    for _ in range(10):
        forecaster.update_candle("ETH", high=p + 1.0, low=p - 1.0, close=p)

    forecast = forecaster.forecast_volatility("ETH")
    assert forecast.symbol == "ETH"
    assert forecast.current_volatility_pct > 0.0
    assert forecast.recommended_grid_spacing_mult > 0.0
    assert forecast.recommended_tp_target_mult > 0.0


# =============================================================================
# 4. CAPITAL GROWTH ALLOCATOR TESTS
# =============================================================================

def test_capital_growth_allocator_tiers():
    allocator = CapitalGrowthAllocator()

    # Tier 1: $50 Micro Bootstrap
    t1 = allocator.compute_shard_allocations(50.0)
    assert t1["tier_name"] == "MICRO_BOOTSTRAP"
    assert t1["shards"]["sniper_shard_737649"]["max_leverage"] == 5.0

    # Tier 2: $500 Growth Compounder
    t2 = allocator.compute_shard_allocations(500.0)
    assert t2["tier_name"] == "GROWTH_COMPOUNDER"
    assert t2["shards"]["sniper_shard_737649"]["max_leverage"] == 3.5

    # Tier 3: $5,000 Institutional Vault
    t3 = allocator.compute_shard_allocations(5000.0)
    assert t3["tier_name"] == "INSTITUTIONAL_VAULT"
    assert t3["shards"]["sniper_shard_737649"]["max_leverage"] == 2.0
