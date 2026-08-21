#!/usr/bin/env python3
"""
Unit and Integration Tests for Frontier Upgrades Suite v5:
1. Dead-Man's Switch & 30s Uptime Watchdog (heartbeat_deadmans_switch.py)
2. L2 Rollup Batch Congestion & Gas Surge Arbitrageur (gas_congestion_arbitrageur.py)
3. Multi-Asset Basket Cointegration Engine (basket_cointegration_engine.py)
4. Multi-Source Profit Attribution & Performance Deck (performance_attribution_deck.py)
========================================================================================
"""

from __future__ import annotations

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from heartbeat_deadmans_switch import (
    DeadMansHeartbeatSwitch,
    HeartbeatStatus,
)
from gas_congestion_arbitrageur import (
    L2GasCongestionArbitrageur,
    CongestionMetrics,
)
from basket_cointegration_engine import (
    BasketCointegrationEngine,
    BasketTradeSignal,
)
from performance_attribution_deck import (
    PerformanceAttributionEngine,
    PerformanceAttributionDeck,
)


# =============================================================================
# 1. DEAD-MAN'S SWITCH TESTS
# =============================================================================

def test_deadmans_heartbeat_switch():
    emergency_triggered = []

    def mock_emergency():
        emergency_triggered.append(True)

    watchdog = DeadMansHeartbeatSwitch(
        ping_interval_sec=10.0,
        max_missed_threshold_sec=30.0,
        emergency_callback=mock_emergency,
    )

    # 1. Fresh ping -> Healthy
    watchdog.emit_heartbeat()
    status_good = watchdog.evaluate_health()
    assert status_good.is_healthy is True
    assert status_good.emergency_action_triggered is False

    # 2. Simulate 45s lapse -> Dead-man trigger
    watchdog._last_ping = time.time() - 45.0
    status_dead = watchdog.evaluate_health()
    assert status_dead.is_healthy is False
    assert status_dead.emergency_action_triggered is True
    assert len(emergency_triggered) == 1


# =============================================================================
# 2. GAS CONGESTION ARBITRAGEUR TESTS
# =============================================================================

def test_l2_gas_congestion_arbitrageur():
    arb = L2GasCongestionArbitrageur(baseline_gas_gwei=0.05, congestion_spike_threshold=2.0)

    # 1. Normal gas -> No congestion
    metrics_normal = arb.evaluate_congestion("Arbitrum", current_gas_gwei=0.05)
    assert metrics_normal.is_congested is False
    assert metrics_normal.recommended_spread_multiplier == 1.0

    # 2. Gas spike to 0.20 Gwei (4.0x) -> Congested
    metrics_spike = arb.evaluate_congestion("Arbitrum", current_gas_gwei=0.20)
    assert metrics_spike.is_congested is True
    assert metrics_spike.recommended_spread_multiplier > 1.5
    assert metrics_spike.expected_extra_edge_bps > 0.0


# =============================================================================
# 3. BASKET COINTEGRATION TESTS
# =============================================================================

def test_basket_cointegration_engine():
    engine = BasketCointegrationEngine(entry_z_score=2.0)

    prices = {"ETH": 2000.0, "SOL": 150.0, "AVAX": 25.0, "SUI": 2.5}
    # SOL outperforms violently (+8.0%), AVAX dumps (-4.0%)
    returns_24h = {"ETH": 1.0, "SOL": 8.0, "AVAX": -4.0, "SUI": 0.5}

    sig = engine.evaluate_basket("L1_BASKET", prices, returns_24h, available_margin_usd=200.0)
    assert sig is not None
    assert sig.basket_name == "L1_BASKET"
    assert sig.long_asset == "AVAX"    # Lagging asset (Buy)
    assert sig.short_asset == "SOL"    # Leading asset (Sell)
    assert sig.z_score_deviation >= 2.0
    assert sig.is_actionable is True


# =============================================================================
# 4. PROFIT ATTRIBUTION DECK TESTS
# =============================================================================

def test_performance_attribution_deck():
    engine = PerformanceAttributionEngine()

    engine.record_trade_pnl("Maker_Spread_Quoting", pnl_usd=45.0, volume_usd=2000.0)
    engine.record_trade_pnl("News_Catalyst_Sniper", pnl_usd=120.0, volume_usd=500.0)
    engine.record_trade_pnl("Latency_Lead_Arbitrage", pnl_usd=35.0, volume_usd=800.0)

    deck = engine.generate_attribution_deck()
    assert deck.total_realized_pnl_usd == 200.0
    assert deck.total_trades_count == 3
    assert deck.top_performing_strategy == "News_Catalyst_Sniper"

    tg_html = engine.format_telegram_deck(deck)
    assert "DAILY ALPHA ATTRIBUTION DECK" in tg_html
    assert "$+200.00" in tg_html
    assert "News Catalyst Sniper" in tg_html
