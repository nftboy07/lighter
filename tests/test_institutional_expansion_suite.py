#!/usr/bin/env python3
"""
Unit and Integration Tests for Institutional Expansion Suite:
1. zkLighter Liquidation Cascade Hunter (liquidation_hunter.py)
2. Dynamic Volatility Grid MM (dynamic_grid_mm.py)
3. Statistical Arbitrage Cointegration Pairs (stat_arb_pairs.py)
4. Institutional Execution Algos: TWAP & Iceberg (institutional_execution_algo.py)
5. Live Telegram Mini-App Web Dashboard (telegram_mini_app.py)
====================================================================================
"""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from liquidation_hunter import (
    LiquidationHunterEngine,
    LiquidationSide,
)
from dynamic_grid_mm import (
    DynamicGridMMEngine,
)
from stat_arb_pairs import (
    StatisticalArbitragePairEngine,
    PairAction,
)
from institutional_execution_algo import (
    InstitutionalExecutionEngine,
    ExecutionAlgoType,
)
from telegram_mini_app import (
    TelegramMiniAppGenerator,
)


# =============================================================================
# 1. LIQUIDATION HUNTER TESTS
# =============================================================================

def test_liquidation_hunter_long_liquidation_snipe():
    engine = LiquidationHunterEngine(min_notional_usd=100.0, min_discount_bps=25.0)
    # Mark price = $2000.0, Bankruptcy price = $1990.0 (0.50% discount = 50 bps)
    order = engine.evaluate_liquidation(
        event_id="liq_1",
        symbol="ETH",
        side=LiquidationSide.LONG_LIQUIDATED,
        bankruptcy_price=1990.0,
        mark_price=2000.0,
        size_base=1.0,
    )
    assert order is not None
    assert order.action == "BUY"
    assert order.snipe_price == 1990.0
    assert order.discount_bps == 50.0
    assert order.expected_profit_usd > 0.0


def test_liquidation_hunter_short_liquidation_snipe():
    engine = LiquidationHunterEngine(min_notional_usd=100.0, min_discount_bps=25.0)
    # Mark price = $2000.0, Bankruptcy price = $2010.0 (0.50% premium = 50 bps)
    order = engine.evaluate_liquidation(
        event_id="liq_2",
        symbol="ETH",
        side=LiquidationSide.SHORT_LIQUIDATED,
        bankruptcy_price=2010.0,
        mark_price=2000.0,
        size_base=1.0,
    )
    assert order is not None
    assert order.action == "SELL"
    assert order.snipe_price == 2010.0
    assert order.discount_bps == 50.0


# =============================================================================
# 2. DYNAMIC GRID MM TESTS
# =============================================================================

def test_dynamic_grid_generation_and_skew():
    engine = DynamicGridMMEngine(
        base_layer_size_usd=50.0,
        num_layers=5,
        base_grid_spacing_pct=0.20,
    )

    grid = engine.generate_grid(
        symbol="SOL",
        mid_price=150.0,
        atr_multiplier=1.0,
        current_inventory_usd=0.0,
    )

    assert len(grid.buy_levels) == 5
    assert len(grid.sell_levels) == 5
    assert grid.grid_spacing_pct == 0.20
    assert grid.buy_levels[0].price < 150.0
    assert grid.sell_levels[0].price > 150.0

    # With high volatility, grid spacing widens
    vol_grid = engine.generate_grid(
        symbol="SOL",
        mid_price=150.0,
        atr_multiplier=2.0,
    )
    assert vol_grid.grid_spacing_pct == 0.40


# =============================================================================
# 3. STATISTICAL ARBITRAGE COINTEGRATION PAIR TESTS
# =============================================================================

def test_stat_arb_pair_divergence_and_exit():
    engine = StatisticalArbitragePairEngine(
        lookback_periods=30,
        entry_z_threshold=2.0,
        exit_z_threshold=0.5,
    )

    # Establish baseline ratio history around 1.0
    for _ in range(25):
        engine.update_prices("SOL", 100.0, "ETH", 100.0)

    # Sudden divergence: SOL spikes to 110.0 while ETH stays 100.0 (Ratio 1.10)
    opp = engine.update_prices("SOL", 110.0, "ETH", 100.0)
    assert opp is not None
    assert opp.action == PairAction.SHORT_A_LONG_B
    assert opp.z_score > 2.0


# =============================================================================
# 4. INSTITUTIONAL EXECUTION ALGOS: TWAP & ICEBERG TESTS
# =============================================================================

def test_institutional_twap_and_iceberg_plans():
    engine = InstitutionalExecutionEngine()

    # 1. TWAP Plan
    twap = engine.build_twap_plan(
        symbol="ETH",
        side="BUY",
        total_notional_usd=600.0,
        current_price=2000.0,
        duration_seconds=60.0,
        num_tranches=6,
    )
    assert twap.algo_type == ExecutionAlgoType.TWAP
    assert len(twap.tranches) == 6
    assert sum(t.notional_usd for t in twap.tranches) == pytest.approx(600.0, abs=0.1)

    # 2. Iceberg Plan
    iceberg = engine.build_iceberg_plan(
        symbol="ETH",
        side="SELL",
        total_notional_usd=1000.0,
        current_price=2000.0,
        visible_display_pct=25.0,
    )
    assert iceberg.algo_type == ExecutionAlgoType.ICEBERG
    assert len(iceberg.tranches) == 4

    # 3. Fill recording
    filled = engine.record_tranche_fill(twap.plan_id, tranche_index=1, filled_price=2000.0)
    assert filled is not None
    assert twap.completed_tranches == 1
    assert twap.progress_pct == pytest.approx(100.0 / 6.0, rel=0.05)


# =============================================================================
# 5. TELEGRAM MINI-APP GENERATOR TESTS
# =============================================================================

def test_telegram_mini_app_html_rendering():
    subs = [
        {"name": "Sniper Shard", "account_index": 737649, "collateral_usd": 500.0, "margin_utilization_pct": 20.0},
        {"name": "MM Shard", "account_index": 281474976497685, "collateral_usd": 250.0, "margin_utilization_pct": 0.0},
    ]
    positions = [
        {"asset": "ETH", "side": "BUY", "pnl_usd": 12.50},
    ]

    html = TelegramMiniAppGenerator.generate_html_dashboard(
        total_portfolio_usd=750.0,
        subaccounts_data=subs,
        active_positions=positions,
        daily_volume_usd=50000.0,
        daily_pnl_usd=85.20,
    )

    assert "<!DOCTYPE html>" in html
    assert "zkLighter Live Hub" in html
    assert "$750.00" in html
    assert "Sniper Shard" in html
    assert "Panic Flatten All" in html
