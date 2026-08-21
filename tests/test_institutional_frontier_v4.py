#!/usr/bin/env python3
"""
Unit and Integration Tests for Frontier Upgrades Suite v4:
1. HFT Fake Wall & Spoofing Detector (spoofing_detector.py)
2. Monte Carlo 10,000-Path VaR Simulator (monte_carlo_var_simulator.py)
3. Multi-DEX Cross-Chain Yield & Fast Bridge Router (cross_chain_liquidity_bridger.py)
4. VIP Telegram & Twitter/X Signal Broadcaster (vip_tg_twitter_broadcaster.py)
================================================================================
"""

from __future__ import annotations

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from spoofing_detector import (
    HFTSpoofingDetector,
    SpoofingMetrics,
)
from monte_carlo_var_simulator import (
    MonteCarloRiskSimulator,
    PortfolioVaRReport,
)
from cross_chain_liquidity_bridger import (
    CrossChainLiquidityBridger,
    CrossChainArbRoute,
)
from vip_tg_twitter_broadcaster import (
    VIPSignalBroadcaster,
    BroadcastSignal,
)


# =============================================================================
# 1. SPOOFING DETECTOR TESTS
# =============================================================================

def test_hft_spoofing_detector():
    detector = HFTSpoofingDetector(min_wall_usd=10000.0, spoof_lifespan_threshold_ms=100.0)

    now_ms = time.time() * 1000.0
    # 1. Place fake $50k wall
    detector.record_order_placement("SOL", order_id="fake_1", side="BUY", price=145.0, size=500.0, timestamp_ms=now_ms)
    # Cancel in 50ms (Spoof!)
    tracker = detector.record_order_cancellation("SOL", order_id="fake_1", timestamp_ms=now_ms + 50.0)
    assert tracker is not None
    assert tracker.is_spoof is True
    assert pytest.approx(tracker.lifespan_ms, abs=1e-2) == 50.0

    raw_walls = [(145.0, 50000.0, "BUY"), (140.0, 30000.0, "BUY")]
    metrics = detector.evaluate_genuine_liquidity("SOL", raw_walls)
    assert metrics.spoof_ratio_pct > 0.0
    # The $145 wall is filtered out, leaving only genuine $140 wall
    assert len(metrics.verified_genuine_walls) == 1
    assert metrics.verified_genuine_walls[0][0] == 140.0


# =============================================================================
# 2. MONTE CARLO VAR SIMULATOR TESTS
# =============================================================================

def test_monte_carlo_var_simulator():
    simulator = MonteCarloRiskSimulator(num_paths=1000)

    positions = [
        {"symbol": "ETH", "notional_usd": 200.0, "is_long": True, "volatility_annual_pct": 60.0},
        {"symbol": "SOL", "notional_usd": 150.0, "is_long": True, "volatility_annual_pct": 75.0},
    ]

    report = simulator.simulate_portfolio_var(total_portfolio_usd=500.0, active_positions=positions)
    assert report.simulated_paths_count == 1000
    assert report.var_95_pct_usd > 0.0
    assert report.var_99_pct_usd >= report.var_95_pct_usd
    assert report.recommended_margin_scaling_mult > 0.0


# =============================================================================
# 3. CROSS-CHAIN LIQUIDITY BRIDGER TESTS
# =============================================================================

def test_cross_chain_liquidity_bridger():
    bridger = CrossChainLiquidityBridger(min_net_spread_apr_pct=15.0)

    # zkLighter has 10% funding, Hyperliquid on Arbitrum has 45% funding -> 35% spread
    route = bridger.evaluate_bridge_opportunity(
        symbol="HYPE",
        source_funding_apr_pct=10.0,
        target_funding_apr_pct=45.0,
        source_chain="Base",
        target_chain="Arbitrum",
        target_protocol="Hyperliquid",
        available_collateral_usd=300.0,
    )

    assert route.symbol == "HYPE"
    assert route.funding_spread_apr_pct == 35.0
    assert route.is_actionable is True
    assert route.recommended_bridge_usd == 150.0
    assert route.net_annual_profit_usd > 0.0


# =============================================================================
# 4. VIP SIGNAL BROADCASTER TESTS
# =============================================================================

def test_vip_signal_broadcaster():
    broadcaster = VIPSignalBroadcaster()

    sig = broadcaster.broadcast_signal(
        symbol="SOL",
        action_side="BUY/LONG",
        entry_price=145.0,
        tp1_price=147.9,
        tp2_price=150.8,
        sl_price=142.8,
        catalyst_headline="US SEC Formally Approves Solana Spot ETF Filings",
        conviction_score=0.95,
        is_paper=True,
    )

    assert sig.symbol == "SOL"
    assert sig.sent_to_telegram is True
    assert sig.sent_to_twitter is True

    tg_html = broadcaster.format_telegram_signal(sig)
    assert "VIP ALPHA SIGNAL: SOL" in tg_html
    assert "$147.90" in tg_html

    tweet = broadcaster.format_twitter_tweet(sig)
    assert "NEW TRADE ALERT: $SOL" in tweet
    assert "#zkLighter" in tweet
