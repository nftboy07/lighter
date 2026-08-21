#!/usr/bin/env python3
"""
Unit and Integration Tests for Ultimate Institutional Master Suite (10-Module Pack):
1. AS Dynamic Inventory Skew (as_inventory_skew.py)
2. Micro-Burst Protection (micro_burst_protector.py)
3. Funding Rate Forecaster (funding_rate_forecaster.py)
4. Hidden Wall Shadowing (hidden_wall_shadow.py)
5. Autonomous Mesh Rebalancer (mesh_rebalancer.py)
6. Trend Confluence Engine (trend_confluence_engine.py)
7. Adaptive Kelly Drawdown Sizer (kelly_drawdown_sizer.py)
8. Execution Impact Minimizer (execution_impact_minimizer.py)
9. Institutional Circuit Breaker (institutional_circuit_breaker.py)
10. Telemetry Health Exporter (telemetry_health_exporter.py)
====================================================================================
"""

from __future__ import annotations

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from as_inventory_skew import ASInventorySkewEngine, ASQuotingParameters
from micro_burst_protector import MicroBurstProtector, MicroBurstAlert
from funding_rate_forecaster import FundingRateForecaster, FundingForecast
from hidden_wall_shadow import HiddenWallShadowEngine, IcebergOrderDetection
from mesh_rebalancer import AutonomousMeshRebalancer, MeshBalanceStatus
from trend_confluence_engine import TrendConfluenceEngine, TrendConfluenceResult
from kelly_drawdown_sizer import AdaptiveKellyDrawdownSizer, KellySizingRecommendation
from execution_impact_minimizer import AlmgrenChrissImpactMinimizer, OptimalExecutionTrajectory
from institutional_circuit_breaker import InstitutionalCircuitBreaker, CircuitBreakerStatus
from telemetry_health_exporter import TelemetryHealthExporter, HealthTelemetrySnapshot


# =============================================================================
# 1. AS INVENTORY SKEW TESTS
# =============================================================================

def test_as_inventory_skew():
    engine = ASInventorySkewEngine(risk_aversion_gamma=0.1, order_arrival_intensity_k=1.5)

    # 1. Flat inventory (q=0) -> Symmetric around mid
    q_flat = engine.calculate_quotes("SOL", mid_price=100.0, inventory_q=0.0)
    assert q_flat.reservation_price == 100.0
    assert q_flat.optimal_bid_price < 100.0
    assert q_flat.optimal_ask_price > 100.0

    # 2. Long inventory (q=+10) -> Reservation price skews DOWN (encourages selling)
    q_long = engine.calculate_quotes("SOL", mid_price=100.0, inventory_q=10.0)
    assert q_long.reservation_price < 100.0
    assert q_long.inventory_skew_bps < 0.0


# =============================================================================
# 2. MICRO-BURST PROTECTOR TESTS
# =============================================================================

def test_micro_burst_protector():
    protector = MicroBurstProtector(burst_threshold_count=4, burst_window_max_ms=50.0, min_burst_volume_usd=2000.0)

    now = time.time() * 1000.0
    # Simulate 4 rapid market sells in 20ms ($4000 total)
    alert = None
    for i in range(4):
        alert = protector.record_trade_tick("ETH", price=2000.0, size=0.5, is_buy=False, timestamp_ms=now + (i * 5.0))

    assert alert is not None
    assert alert.symbol == "ETH"
    assert alert.burst_trades_count == 4
    assert alert.is_toxic_sweep is True
    assert alert.recommended_action == "PULL_MAKER_QUOTES"


# =============================================================================
# 3. FUNDING RATE FORECASTER TESTS
# =============================================================================

def test_funding_rate_forecaster():
    forecaster = FundingRateForecaster(min_arb_apr_threshold=20.0)

    # Record premium index expansion (Perp $105 vs Spot $100 -> +5% premium)
    for _ in range(5):
        forecaster.record_premium_tick("SOL", perp_price=105.0, spot_price=100.0)

    forecast = forecaster.forecast_next_funding("SOL", current_funding_rate_8h=0.0003)
    assert forecast.symbol == "SOL"
    assert forecast.predicted_next_funding_rate_8h > 0.0
    assert forecast.predicted_annual_yield_pct > 0.0
    assert forecast.confidence_score >= 0.50


# =============================================================================
# 4. HIDDEN WALL SHADOW TESTS
# =============================================================================

def test_hidden_wall_shadow():
    shadow = HiddenWallShadowEngine(min_iceberg_ratio=2.0, min_hidden_notional_usd=5000.0)

    # 1. Record displayed visible size of 5 SOL ($500) @ $100
    shadow.record_displayed_depth("SOL", price=100.0, visible_size=5.0, is_buy=False)

    # 2. Cumulative trade fills execute 100 SOL ($10,000) at $100 -> Iceberg!
    detection = shadow.record_trade_fill("SOL", price=100.0, trade_size=100.0, is_buy=True)
    assert detection is not None
    assert detection.symbol == "SOL"
    assert detection.is_confirmed_iceberg is True
    assert detection.iceberg_ratio >= 2.0
    assert detection.estimated_total_hidden_usd >= 5000.0


# =============================================================================
# 5. MESH REBALANCER TESTS
# =============================================================================

def test_autonomous_mesh_rebalancer():
    rebalancer = AutonomousMeshRebalancer(imbalance_threshold_pct=10.0)

    # Shard Sniper has $800, MM has $100, Treasury has $100 (Total $1000)
    # Target: Sniper $400, MM $400, Treasury $200
    balances = {
        "Subaccount_737649_Sniper": 800.0,
        "Subaccount_MM": 100.0,
        "Subaccount_Treasury": 100.0,
    }

    status = rebalancer.evaluate_mesh(balances)
    assert status.is_balanced is False
    assert len(status.recommended_actions) >= 1
    assert status.recommended_actions[0].source_shard == "Subaccount_737649_Sniper"


# =============================================================================
# 6. TREND CONFLUENCE ENGINE TESTS
# =============================================================================

def test_trend_confluence_engine():
    engine = TrendConfluenceEngine()

    # Price $105 > EMA_1m $104 > EMA_5m $102 > EMA_15m $100 -> Strong Bullish
    res = engine.evaluate_trend_alignment(
        symbol="SOL",
        proposed_side="BUY/LONG",
        ema_1m=104.0,
        ema_5m=102.0,
        ema_15m=100.0,
        current_price=105.0,
        adx_14=32.0,
    )

    assert res.dominant_trend == "STRONG_BULLISH"
    assert res.confluence_score >= 80.0
    assert res.is_aligned_with_signal is True
    assert res.recommended_position_multiplier >= 1.0


# =============================================================================
# 7. KELLY SIZING TESTS
# =============================================================================

def test_kelly_drawdown_sizer():
    sizer = AdaptiveKellyDrawdownSizer()

    rec = sizer.calculate_trade_size(
        symbol="SOL",
        total_portfolio_usd=1000.0,
        win_rate_p=0.70,
        win_loss_payoff_b=2.0,
        current_drawdown_pct=0.0,
    )

    assert rec.symbol == "SOL"
    assert rec.optimal_kelly_fraction > 0.0
    assert rec.recommended_position_usd > 0.0
    assert rec.current_drawdown_dampener == 1.0


# =============================================================================
# 8. EXECUTION IMPACT MINIMIZER TESTS
# =============================================================================

def test_execution_impact_minimizer():
    minimizer = AlmgrenChrissImpactMinimizer(max_slice_size_usd=25.0)

    # Large $100 order sliced into 4 chunks
    traj = minimizer.plan_execution_trajectory(
        symbol="SOL",
        total_order_size_usd=100.0,
        available_top_depth_usd=2000.0,
    )

    assert traj.symbol == "SOL"
    assert traj.num_slices == 4
    assert len(traj.slices) == 4
    assert traj.estimated_total_slippage_bps < 2.5


# =============================================================================
# 9. INSTITUTIONAL CIRCUIT BREAKER TESTS
# =============================================================================

def test_institutional_circuit_breaker():
    cb = InstitutionalCircuitBreaker()

    # 1. Normal state -> Allowed
    ok, _ = cb.is_asset_tradeable("SOL")
    assert ok is True

    # 2. Trigger Tier-1 Cooldown
    cb.trigger_tier1_asset_cooldown("SOL", reason="Anomalous slippage")
    ok_sol, reason = cb.is_asset_tradeable("SOL")
    assert ok_sol is False
    assert "Tier-1" in reason

    # 3. Other asset is still allowed
    ok_eth, _ = cb.is_asset_tradeable("ETH")
    assert ok_eth is True

    # 4. Trigger Tier-3 Global Evacuation
    cb.trigger_tier3_global_evacuation(reason="Macro volatility shock")
    ok_all, reason_all = cb.is_asset_tradeable("ETH")
    assert ok_all is False
    assert "Tier-3" in reason_all


# =============================================================================
# 10. TELEMETRY HEALTH EXPORTER TESTS
# =============================================================================

def test_telemetry_health_exporter():
    exporter = TelemetryHealthExporter()
    exporter.record_trade()
    exporter.record_ws_latency(15.4)

    snap = exporter.generate_health_snapshot(total_portfolio_usd=500.0, total_realized_pnl_usd=50.0)
    assert snap.total_trades_processed == 1
    assert snap.is_fully_healthy is True

    prom = exporter.export_prometheus_metrics(snap)
    assert "lighter_portfolio_usd" in prom
    assert "lighter_pnl_usd" in prom
