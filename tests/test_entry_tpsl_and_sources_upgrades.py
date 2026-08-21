#!/usr/bin/env python3
"""
Unit and Integration Tests for:
1. Microstructure Entry Filter & Chaser (microstructure_entry_filter.py)
2. Advanced TP/SL & Chandelier Engine (advanced_tpsl_engine.py)
3. Macro & On-Chain Sources Engine (macro_onchain_sources.py)
====================================================================
"""

from __future__ import annotations

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from microstructure_entry_filter import (
    MicrostructureEntryFilter,
    MicrostructureDecision,
)
from advanced_tpsl_engine import (
    AdvancedTPSLEngine,
    ExitAction,
)
from macro_onchain_sources import (
    MacroOnChainSourcesEngine,
    MacroSignal,
)


# =============================================================================
# 1. MICROSTRUCTURE ENTRY FILTER TESTS
# =============================================================================

def test_microstructure_ofi_and_decision():
    filter_engine = MicrostructureEntryFilter()

    # Heavy buy wall: $100k bids vs $10k asks -> Positive OFI
    bids = [(2000.0, 50.0)]
    asks = [(2001.0, 5.0)]

    dec = filter_engine.evaluate_entry("ETH", "BUY/LONG", bids, asks, conviction=0.90)
    assert dec.is_approved is True
    assert dec.ofi_score > 0.50
    assert dec.recommended_entry_type in ["POST_ONLY_LIMIT", "IOC_TAKER"]

    # Heavy sell wall against buy order with low conviction -> Veto
    bids_low = [(2000.0, 2.0)]
    asks_heavy = [(2001.0, 50.0)]
    dec_veto = filter_engine.evaluate_entry("ETH", "BUY/LONG", bids_low, asks_heavy, conviction=0.80)
    assert dec_veto.is_approved is False
    assert "OFI sell pressure" in str(dec_veto.rejection_reason)


# =============================================================================
# 2. ADVANCED TP/SL ENGINE TESTS
# =============================================================================

def test_wall_aware_tp_and_chandelier_exit():
    engine = AdvancedTPSLEngine(
        time_decay_seconds=2.0,       # Fast test decay
        min_momentum_gain_pct=1.0,
        max_mae_loss_usd=10.0,
        wall_threshold_usd=20000.0,
    )

    # 1. Wall-Aware TP
    asks = [(2038.0, 1.0), (2040.0, 25.0)]  # $51k wall at $2040
    adj_tp = engine.calculate_wall_aware_tp(
        entry_price=2000.0,
        is_long=True,
        target_tp_price=2040.0,
        orderbook_levels=asks,
    )
    assert adj_tp < 2040.0
    assert adj_tp == 2038.98

    # 2. Position Lifecycle & Chandelier Trailing
    engine.register_position(
        position_id="test_eth_1",
        symbol="ETH",
        side="BUY/LONG",
        entry_price=2000.0,
        size=1.0,
        initial_tp_price=2100.0,
        initial_sl_price=1970.0,
    )

    # Price moves to +3% ($2060) -> Locks Breakeven SL
    action1 = engine.evaluate_tick("test_eth_1", current_price=2060.0)
    assert action1.action_type == "ADJUST_SL"
    assert "Breakeven" in action1.reason

    # Price hits Stop-Loss ($1965) -> Closes Market
    action2 = engine.evaluate_tick("test_eth_1", current_price=1965.0)
    assert action2.action_type == "CLOSE_MARKET"

    # Time decay test
    time.sleep(2.1)
    engine.register_position("test_eth_stale", "ETH", "BUY/LONG", 2000.0, 1.0, 2040.0, 1970.0)
    # Stagnant price ($2002) after 2s decay -> auto-exit
    pos = engine.positions["test_eth_stale"]
    pos.entry_time = time.time() - 5.0
    action3 = engine.evaluate_tick("test_eth_stale", current_price=2002.0)
    assert action3.action_type == "CLOSE_MARKET"
    assert "Time-Decay" in action3.reason


# =============================================================================
# 3. MACRO & ON-CHAIN ALPHA SOURCES TESTS
# =============================================================================

def test_macro_fomc_diff_and_mint_parser():
    engine = MacroOnChainSourcesEngine()

    prev_statement = "The Committee remains attentive to elevated inflation and is committed to restrictive policy."
    new_statement = "The Committee notes disinflation progress and is preparing for rate cut easing."

    fomc_sig = engine.parse_fomc_statement_diff(new_statement, prev_statement)
    assert fomc_sig is not None
    assert fomc_sig.direction == "BULLISH"
    assert "DOVISH" in fomc_sig.headline

    # Stablecoin mint test
    mint_sig = engine.parse_stablecoin_mint_burn("USDT", 250000000.0, "MINT", chain="Ethereum")
    assert mint_sig is not None
    assert mint_sig.direction == "BULLISH"
    assert "250,000,000" in mint_sig.headline

    # DAO Governance test
    gov_sig = engine.parse_governance_proposal("UNI", "Activate Uniswap Protocol Fee Switch", "PASSED")
    assert gov_sig is not None
    assert gov_sig.direction == "BULLISH"
    assert gov_sig.asset == "UNI"
