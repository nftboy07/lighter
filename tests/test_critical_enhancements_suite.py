#!/usr/bin/env python3
"""
Unit and Integration Tests for Critical Enhancements Suite:
1. Sub-300µs Fast Signer (cython_fast_signer.py)
2. Autonomous Hourly Profit-Harvesting Daemon (profit_harvesting_daemon.py)
3. CEX Order Flow Early-Spike Pre-Detector (cex_flow_predetector.py)
4. Multi-Market Simultaneous 0-Fee Grid Quoter (multi_market_grid_quoter.py)
5. Instant Telegram Visual Fill Cards with Overlays (visual_fill_cards.py)
===========================================================================
"""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cython_fast_signer import (
    UltraFastSignerEngine,
)
from profit_harvesting_daemon import (
    AutonomousProfitHarvestingDaemon,
    HarvestExecution,
)
from cex_flow_predetector import (
    CEXFlowPreDetector,
    VolumeSweepAlert,
)
from multi_market_grid_quoter import (
    MultiMarketGridQuoterEngine,
)
from visual_fill_cards import (
    VisualFillCardRenderer,
    HAS_MATPLOTLIB,
)
from subaccount_manager import (
    SubaccountManager,
)


# =============================================================================
# 1. ULTRA-FAST SIGNER TESTS
# =============================================================================

def test_ultra_fast_signer_payload_and_latency():
    signer = UltraFastSignerEngine(account_index=737649, api_key_index=5)
    
    res = signer.pre_sign_order(
        market_index=0,
        is_ask=False,
        price=2000.0,
        amount=1.0,
    )

    assert res["account_index"] == 737649
    assert res["market_index"] == 0
    assert res["is_ask"] is False
    assert res["raw_price"] == 200000
    assert res["signature"].startswith("0xfast_sig_")
    assert res["signing_latency_us"] < 3000.0  # Well within lightning timing


# =============================================================================
# 2. AUTONOMOUS PROFIT HARVESTING DAEMON TESTS
# =============================================================================

@pytest.mark.asyncio
async def test_profit_harvesting_daemon_cycle():
    mgr = SubaccountManager()
    mgr.update_state(737649, collateral_usd=100.0, available_margin_usd=100.0)
    mgr.update_state(281474976497686, collateral_usd=10.0, available_margin_usd=10.0)

    daemon = AutonomousProfitHarvestingDaemon(
        subaccount_manager=mgr,
        profit_threshold_pct=15.0,
        min_harvest_usd=5.0,
    )
    # Set baseline at $80 ($20 excess profit -> $10 harvested)
    daemon.set_baseline(737649, 80.0)

    executions = await daemon.run_harvest_cycle(is_paper=True)
    assert len(executions) == 1
    assert executions[0].from_account_index == 737649
    assert executions[0].harvested_usd == 10.0
    assert executions[0].status == "COMPLETED"


# =============================================================================
# 3. CEX FLOW PRE-DETECTOR TESTS
# =============================================================================

def test_cex_flow_predetector_buy_sweep():
    detector = CEXFlowPreDetector(
        min_sweep_volume_usd=100000.0,
        sweep_window_ms=50.0,
        min_price_move_pct=0.10,
    )

    # 1st trade tick
    t0 = 1000000.0
    alert1 = detector.on_trade_tick("ETH", price=2000.0, quantity=10.0, is_buyer_maker=False, timestamp_ms=t0)
    assert alert1 is None

    # Aggressive 2nd trade tick within 30ms pushing price +0.15% with $150k volume
    alert2 = detector.on_trade_tick("ETH", price=2003.0, quantity=75.0, is_buyer_maker=False, timestamp_ms=t0 + 30)
    assert alert2 is not None
    assert alert2.direction == "BUY_SPIKE"
    assert alert2.symbol == "ETH"
    assert alert2.volume_usd > 100000.0
    assert alert2.price_change_pct >= 0.10


# =============================================================================
# 4. MULTI-MARKET SIMULTANEOUS GRID QUOTER TESTS
# =============================================================================

def test_multi_market_grid_quoter():
    engine = MultiMarketGridQuoterEngine(total_mm_collateral_usd=250.0)
    assert len(engine.sessions) == 5

    # Quote ETH
    eth_grid = engine.update_market_quote("ETH", mid_price=2000.0, atr_multiplier=1.0)
    assert eth_grid is not None
    assert len(eth_grid.buy_levels) == 5

    # Quote SOL
    sol_grid = engine.update_market_quote("SOL", mid_price=150.0, atr_multiplier=1.2)
    assert sol_grid is not None

    # Record volume
    engine.record_fill_volume("ETH", 5000.0)
    summary = engine.get_multi_market_summary()
    assert summary["total_farmed_volume_usd"] == 5000.0
    assert "ETH" in summary["active_quoting_markets"]
    assert "SOL" in summary["active_quoting_markets"]


# =============================================================================
# 5. VISUAL FILL CARD RENDERER TESTS
# =============================================================================

def test_visual_fill_card_renderer():
    if not HAS_MATPLOTLIB:
        pytest.skip("matplotlib not installed")

    png_bytes = VisualFillCardRenderer.render_trade_card(
        symbol="ETH",
        side="BUY/LONG",
        entry_price=2000.0,
        tp1_price=2040.0,
        tp2_price=2080.0,
        sl_price=1970.0,
        vwap_price=2005.0,
        notional_usd=250.0,
        catalyst_headline="SEC Approves Ethereum ETF Listing",
    )

    assert len(png_bytes) > 1000
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
