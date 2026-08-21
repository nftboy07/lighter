#!/usr/bin/env python3
"""
Unit and Integration Tests for Frontier Upgrades Suite v3:
1. Cross-Exchange Latency Lead Arbitrage (latency_arbitrage_engine.py)
2. Liquidation Cascade Predictor & Wick Snatcher (liquidation_cascade_predictor.py)
3. Autonomous Compounding & Reinvestment Optimizer (compound_reinvestment_engine.py)
====================================================================================
"""

from __future__ import annotations

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from latency_arbitrage_engine import (
    LatencyLeadArbitrageEngine,
    LatencyArbSignal,
)
from liquidation_cascade_predictor import (
    LiquidationCascadePredictor,
    CascadeReboundSetup,
)
from compound_reinvestment_engine import (
    DynamicCompoundingOptimizer,
    ReinvestmentSplit,
)


# =============================================================================
# 1. LATENCY ARBITRAGE TESTS
# =============================================================================

def test_latency_lead_arbitrage_engine():
    engine = LatencyLeadArbitrageEngine(min_dislocation_bps=10.0)

    now_ms = time.time() * 1000.0
    # CEX is $2004 (leads higher), DEX is $2000 (dislocation ~20 bps)
    engine.update_cex_tick("ETH", price=2004.0, timestamp_ms=now_ms)
    engine.update_dex_tick("ETH", price=2000.0, timestamp_ms=now_ms - 50.0)

    sig = engine.evaluate_arbitrage("ETH")
    assert sig is not None
    assert sig.symbol == "ETH"
    assert sig.action_side == "BUY/LONG"
    assert sig.spread_bps == 20.0
    assert sig.is_actionable is True


# =============================================================================
# 2. LIQUIDATION CASCADE PREDICTOR TESTS
# =============================================================================

def test_liquidation_cascade_predictor():
    predictor = LiquidationCascadePredictor(min_liquidation_notional_usd=20000.0)

    clusters = [
        (1950.0, 50000.0, "LONG_LIQ"),
        (1920.0, 80000.0, "LONG_LIQ"),   # Deepest long liquidation cluster
        (1900.0, 120000.0, "LONG_LIQ"),  # Extreme exhaustion
    ]

    setup = predictor.predict_cascade_rebound(
        symbol="ETH",
        current_price=2000.0,
        liquidation_clusters=clusters,
    )

    assert setup is not None
    assert setup.symbol == "ETH"
    assert setup.side == "BUY/LONG"
    assert setup.predicted_exhaustion_price == 1900.0
    assert setup.rebound_entry_price > 1900.0  # Placed slightly above exhaustion
    assert setup.target_tp_price > setup.rebound_entry_price


# =============================================================================
# 3. COMPOUNDING & REINVESTMENT OPTIMIZER TESTS
# =============================================================================

def test_dynamic_compounding_optimizer():
    optimizer = DynamicCompoundingOptimizer()

    # 1. High winning trades history -> Aggressive compounding (75% active reinvest)
    pnls_bullish = [25.0, 30.0, 18.0, -5.0, 40.0, 22.0, 15.0, -4.0, 35.0, 28.0]
    split_high = optimizer.calculate_reinvestment_split(pnls_bullish)
    assert split_high.current_regime == "AGGRESSIVE_COMPOUND"
    assert split_high.active_reinvest_pct == 75.0
    assert split_high.cold_treasury_lock_pct == 25.0

    # 2. Chop/losing history -> Defensive lock (25% active, 75% treasury lock)
    pnls_defensive = [-15.0, -20.0, 5.0, -12.0, -8.0, 4.0, -10.0]
    split_low = optimizer.calculate_reinvestment_split(pnls_defensive)
    assert split_low.current_regime == "DEFENSIVE_LOCK"
    assert split_low.active_reinvest_pct == 25.0
    assert split_low.cold_treasury_lock_pct == 75.0
