#!/usr/bin/env python3
"""
Unit and Integration Tests for Frontier Upgrades Suite v6:
1. Autonomous Delta-Neutral Basis Compounder Vault (delta_neutral_basis_vault.py)
2. Microsecond Order Flow Imbalance (OFI) Fill Predictor (order_flow_imbalance_engine.py)
3. CEX-DEX Triangular Arbitrage Loop (triangular_arbitrage_engine.py)
4. Tick-Level Orderbook Execution Replayer & Simulator (tick_execution_replay.py)
================================================================================
"""

from __future__ import annotations

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from delta_neutral_basis_vault import (
    DeltaNeutralBasisVault,
    BasisVaultState,
)
from order_flow_imbalance_engine import (
    MicrosecondOFIPredictor,
    OFIPrediction,
)
from triangular_arbitrage_engine import (
    TriangularArbitrageEngine,
    TriangularArbCycle,
)
from tick_execution_replay import (
    TickExecutionReplayer,
    ReplaySimulationResult,
)


# =============================================================================
# 1. DELTA-NEUTRAL BASIS VAULT TESTS
# =============================================================================

def test_delta_neutral_basis_vault():
    vault = DeltaNeutralBasisVault(symbol="SOL", initial_capital_usd=200.0)

    # 1. Allocate initial 50/50 position ($100 spot / $100 short @ $100 price)
    state = vault.allocate_initial_position(spot_price=100.0, perp_price=100.0)
    assert state.spot_balance == 1.0
    assert state.perp_short_size == 1.0
    assert state.net_delta_usd == 0.0

    # 2. Harvest 8h funding payout (+0.04% funding -> payout = $0.04 on $100 notional)
    payout = vault.harvest_funding_payment(funding_rate_8h=0.0004, spot_price=100.0, perp_price=100.0)
    assert payout == pytest.approx(0.04, abs=1e-4)
    assert vault.spot_balance > 1.0  # Compounded into spot!

    # 3. Delta rebalance when spot accumulates additional units or price diverges
    vault.spot_balance = 1.20  # Additional spot bought
    rebalanced, adj = vault.rebalance_delta(spot_price=100.0, perp_price=100.0)
    assert rebalanced is True
    assert vault.perp_short_size == 1.20


# =============================================================================
# 2. OFI FILL PREDICTOR TESTS
# =============================================================================

def test_order_flow_imbalance_engine():
    predictor = MicrosecondOFIPredictor(rolling_window_size=10, z_threshold=1.5)

    # 1. Initial snapshot
    p1 = predictor.update_orderbook_top("ETH", bid_price=2000.0, bid_size=10.0, ask_price=2001.0, ask_size=10.0)
    assert p1.symbol == "ETH"
    assert p1.current_ofi_score == 0.0

    # 2. Aggressive buyer surge (Bid price moves up to 2000.5 with size 25)
    p2 = predictor.update_orderbook_top("ETH", bid_price=2000.5, bid_size=25.0, ask_price=2001.0, ask_size=5.0)
    assert p2.current_ofi_score > 0.0
    assert p2.ask_fill_probability_pct > 50.0  # Asks are being eaten!


# =============================================================================
# 3. TRIANGULAR ARBITRAGE ENGINE TESTS
# =============================================================================

def test_triangular_arbitrage_engine():
    arb = TriangularArbitrageEngine(min_profit_threshold_bps=5.0)

    # SOL/USDC = 150, ETH/USDC = 2000 -> Synthetic SOL/ETH = 0.0750
    # But direct market SOL/ETH is mispriced at 0.0740 (dislocation ~13.5 bps)
    cycle = arb.evaluate_triangular_cycle(
        base1="SOL",
        base2="ETH",
        quote="USDC",
        px_base1_quote=150.0,
        px_base2_quote=2000.0,
        px_base1_base2=0.0740,
        available_margin_usd=500.0,
    )

    assert cycle is not None
    assert cycle.gross_dislocation_bps > 10.0
    assert cycle.net_profit_bps > 5.0
    assert cycle.is_actionable is True
    assert cycle.recommended_trade_usd == 250.0


# =============================================================================
# 4. TICK EXECUTION REPLAYER TESTS
# =============================================================================

def test_tick_execution_replayer():
    replayer = TickExecutionReplayer()

    # Generate synthetic price path with an upward surge
    base_px = 100.0
    for i in range(20):
        px = base_px + (i * 0.25)  # Price climbs from 100 to 105 (+5%)
        replayer.record_tick("SOL", price=px, size=10.0, is_buy=True)

    result = replayer.run_strategy_replay(
        symbol="SOL",
        entry_threshold_bps=5.0,
        tp_pct=2.0,
        sl_pct=1.0,
        position_size_usd=100.0,
    )

    assert result.symbol == "SOL"
    assert result.ticks_processed_count == 20
    assert result.simulated_trades_count >= 1
    assert result.gross_pnl_usd > 0.0
    assert result.win_rate_pct > 0.0
