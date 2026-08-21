#!/usr/bin/env python3
"""
Unit and Integration Tests for:
1. Cross-DEX Hyperliquid <-> zkLighter Price-Lag Arbitrage Engine (cross_dex_arbitrage.py)
2. Sub-3ms Fast Signer & Pre-Cached Nonce Manager (fast_signer.py)
========================================================================================
"""

import asyncio
import os
import sys
import time
from typing import List
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cross_dex_arbitrage import (
    ArbDirection,
    CrossDexArbOpportunity,
    CrossDexArbitrageEngine,
    HyperliquidPriceState,
    ZkLighterBookState,
)
from fast_signer import (
    FastMarketConfig,
    FastSignedTransaction,
    FastZkLighterSigner,
    OrderType,
    PreAllocatedPayloadPool,
    PreCachedNonceManager,
    TimeInForce,
)


# =============================================================================
# PART 1: CROSS-DEX ARBITRAGE ENGINE TESTS
# =============================================================================

def test_cross_dex_arb_initialization():
    """Test engine initialization with default assets and market configs."""
    engine = CrossDexArbitrageEngine()
    assert "BTC" in engine.assets
    assert "ETH" in engine.assets
    assert "SOL" in engine.assets
    assert "HYPE" in engine.assets
    assert engine.min_spread_bps == 25.0
    assert engine.max_staleness_sec == 2.5
    assert len(engine.hl_states) == 4
    assert len(engine.zkl_states) == 4


def test_cross_dex_arb_hyperliquid_lead_up_buy_zklighter():
    """
    Test scenario: Hyperliquid leads UP by >= 25 bps (0.25%).
    Hyperliquid BTC mark price surges to $96,300 while zkLighter ask is lagging at $96,000.
    Spread: ~31.2 bps (> 25 bps).
    Expected Signal: BUY_ZKLIGHTER_SELL_HL
    """
    engine = CrossDexArbitrageEngine(min_spread_bps=25.0)
    now = time.time()

    # zkLighter book: bid 95,980, ask 96,000 (mid 95,990)
    engine.update_zklighter_book(
        asset="BTC",
        best_bid=95980.0,
        best_ask=96000.0,
        best_bid_size=1.5,
        best_ask_size=2.0,
        timestamp=now,
    )

    # Hyperliquid jumps to 96,300 (leading up)
    opp = engine.update_hyperliquid_price(
        asset="BTC",
        price=96300.0,
        timestamp=now,
    )

    assert opp is not None
    assert opp.asset == "BTC"
    assert opp.direction == ArbDirection.BUY_ZKLIGHTER_SELL_HL
    assert opp.hl_price == 96300.0
    assert opp.zklighter_best_ask == 96000.0
    assert opp.spread_bps >= 25.0
    assert opp.net_edge_bps > 0.0
    assert opp.target_size > 0.0
    assert opp.estimated_profit_usd > 0.0
    assert opp.is_actionable is True
    assert opp.urgency in ("MEDIUM", "HIGH", "CRITICAL")
    assert "BUY_ZKLIGHTER_SELL_HL" in opp.summary()


def test_cross_dex_arb_hyperliquid_lead_down_sell_zklighter():
    """
    Test scenario: Hyperliquid leads DOWN by >= 25 bps.
    Hyperliquid ETH dumps to $2,690 while zkLighter bid is lagging at $2,700.
    Spread: ~37 bps (> 25 bps).
    Expected Signal: SELL_ZKLIGHTER_BUY_HL
    """
    engine = CrossDexArbitrageEngine(min_spread_bps=25.0)
    now = time.time()

    # zkLighter book: bid 2,700, ask 2,702 (mid 2,701)
    engine.update_zklighter_book(
        asset="ETH",
        best_bid=2700.0,
        best_ask=2702.0,
        best_bid_size=10.0,
        best_ask_size=8.0,
        timestamp=now,
    )

    # Hyperliquid dumps to 2,690 (leading down)
    opp = engine.update_hyperliquid_price(
        asset="ETH",
        price=2690.0,
        timestamp=now,
    )

    assert opp is not None
    assert opp.asset == "ETH"
    assert opp.direction == ArbDirection.SELL_ZKLIGHTER_BUY_HL
    assert opp.hl_price == 2690.0
    assert opp.zklighter_best_bid == 2700.0
    assert opp.spread_bps >= 25.0
    assert opp.net_edge_bps > 0.0
    assert opp.target_size > 0.0
    assert opp.estimated_profit_usd > 0.0
    assert opp.is_actionable is True


def test_cross_dex_arb_sol_and_hype():
    """Test SOL and HYPE arbitrage signal generation."""
    engine = CrossDexArbitrageEngine(min_spread_bps=25.0)
    now = time.time()

    # SOL test
    engine.update_zklighter_book("SOL", best_bid=180.0, best_ask=180.1, best_bid_size=50.0, best_ask_size=50.0, timestamp=now)
    opp_sol = engine.update_hyperliquid_price("SOL", price=181.0, timestamp=now) # ~50 bps lead
    assert opp_sol is not None
    assert opp_sol.asset == "SOL"
    assert opp_sol.direction == ArbDirection.BUY_ZKLIGHTER_SELL_HL

    # HYPE test
    engine.update_zklighter_book("HYPE", best_bid=25.0, best_ask=25.05, best_bid_size=200.0, best_ask_size=200.0, timestamp=now)
    opp_hype = engine.update_hyperliquid_price("HYPE", price=24.80, timestamp=now) # ~80 bps drop
    assert opp_hype is not None
    assert opp_hype.asset == "HYPE"
    assert opp_hype.direction == ArbDirection.SELL_ZKLIGHTER_BUY_HL


def test_cross_dex_arb_below_threshold_no_signal():
    """Test that spread difference below 25 bps (e.g. 10 bps) does not trigger signal."""
    engine = CrossDexArbitrageEngine(min_spread_bps=25.0)
    now = time.time()

    # zkLighter mid = 96,000
    engine.update_zklighter_book("BTC", best_bid=95995.0, best_ask=96005.0, timestamp=now)
    # HL is 96,080 -> 8.3 bps difference (< 25 bps)
    opp = engine.update_hyperliquid_price("BTC", price=96080.0, timestamp=now)

    assert opp is None


def test_cross_dex_arb_stale_feed_protection():
    """Test that stale prices (> max_staleness_sec) are rejected."""
    engine = CrossDexArbitrageEngine(min_spread_bps=25.0, max_staleness_sec=2.0)
    now = time.time()

    # zkLighter update 5 seconds ago (stale)
    engine.update_zklighter_book("BTC", best_bid=96000.0, best_ask=96010.0, timestamp=now - 5.0)
    # HL update fresh
    opp = engine.update_hyperliquid_price("BTC", price=96500.0, timestamp=now)

    assert opp is None


def test_cross_dex_arb_invalid_orderbook_handling():
    """Test that inverted, zero, or infinite orderbooks are safely ignored."""
    engine = CrossDexArbitrageEngine(min_spread_bps=25.0)
    now = time.time()

    # Crossed book (bid > ask)
    engine.update_zklighter_book("BTC", best_bid=96100.0, best_ask=96000.0, timestamp=now)
    assert engine.evaluate_arbitrage("BTC", now=now) is None

    # Zero bid
    engine.update_zklighter_book("BTC", best_bid=0.0, best_ask=96000.0, timestamp=now)
    assert engine.evaluate_arbitrage("BTC", now=now) is None


def test_cross_dex_arb_opportunity_callback():
    """Test async / sync callback invocation upon opportunity detection."""
    captured: List[CrossDexArbOpportunity] = []

    def on_opp(opportunity: CrossDexArbOpportunity):
        captured.append(opportunity)

    engine = CrossDexArbitrageEngine(min_spread_bps=25.0, on_opportunity=on_opp)
    now = time.time()

    engine.update_zklighter_book("ETH", best_bid=2700.0, best_ask=2701.0, best_ask_size=5.0, timestamp=now)
    engine.update_hyperliquid_price("ETH", price=2720.0, timestamp=now)

    assert len(captured) == 1
    assert captured[0].asset == "ETH"
    assert captured[0].direction == ArbDirection.BUY_ZKLIGHTER_SELL_HL


def test_cross_dex_arb_parsers():
    """Test parsing helpers for Hyperliquid and zkLighter feeds."""
    engine = CrossDexArbitrageEngine()

    # Parse allMids
    all_mids_payload = {"data": {"mids": {"BTC": "96250.5", "ETH": "2715.2", "SOL": "182.4", "HYPE": "25.5"}}}
    parsed_mids = dict(engine.parse_hyperliquid_all_mids(all_mids_payload))
    assert parsed_mids["BTC"] == 96250.5
    assert parsed_mids["ETH"] == 2715.2
    assert parsed_mids["SOL"] == 182.4
    assert parsed_mids["HYPE"] == 25.5

    # Parse metaAndAssetCtxs
    meta_ctx_payload = [
        {"universe": [{"name": "ETH"}, {"name": "BTC"}]},
        [{"markPx": "2710.0", "funding": "0.0001"}, {"markPx": "96100.0", "funding": "0.0002"}],
    ]
    parsed_meta = engine.parse_hyperliquid_meta_and_contexts(meta_ctx_payload)
    assert len(parsed_meta) == 2
    assert parsed_meta[0] == ("ETH", 2710.0, 0.0001)
    assert parsed_meta[1] == ("BTC", 96100.0, 0.0002)

    # Parse zkLighter orderbook WS message
    ws_ob_msg = {
        "market_index": 0,  # ETH
        "bids": [[2700.0, 5.0]],
        "asks": [[2702.0, 4.0]],
    }
    parsed_ob = engine.parse_zklighter_orderbook_msg(ws_ob_msg)
    assert parsed_ob is not None
    asset, bid, ask, bid_sz, ask_sz = parsed_ob
    assert asset == "ETH"
    assert bid == 2700.0
    assert ask == 2702.0
    assert bid_sz == 5.0
    assert ask_sz == 4.0


def test_cross_dex_arb_metrics():
    """Test metrics calculation and aggregation."""
    engine = CrossDexArbitrageEngine(min_spread_bps=25.0)
    now = time.time()

    engine.update_zklighter_book("BTC", best_bid=95000.0, best_ask=95010.0, timestamp=now)
    engine.update_hyperliquid_price("BTC", price=95500.0, timestamp=now)

    metrics = engine.get_metrics()
    assert metrics["total_evaluations"] >= 2
    assert metrics["total_opportunities_found"] >= 1
    assert metrics["opportunities_by_asset"]["BTC"] >= 1
    assert metrics["latest_opportunity"] is not None


# =============================================================================
# PART 2: SUB-3MS FAST SIGNER & PRE-CACHED NONCE MANAGER TESTS
# =============================================================================

def test_pre_cached_nonce_manager_sequential_and_batch():
    """Test lockless sequential nonce generation and burst reservations."""
    manager = PreCachedNonceManager(initial_nonce=10)
    assert manager.current_nonce == 10

    # Sequential single nonces
    n1 = manager.get_next_nonce()
    n2 = manager.get_next_nonce()
    assert n1 == 10
    assert n2 == 11
    assert manager.current_nonce == 12
    assert manager.pending_count == 2

    # Batch reservation
    batch = manager.reserve_nonce_batch(5)
    assert batch == [12, 13, 14, 15, 16]
    assert manager.current_nonce == 17
    assert manager.pending_count == 7

    # Mark confirmed and failed
    manager.mark_nonce_confirmed(10)
    manager.mark_nonce_failed(11)
    assert manager.pending_count == 5

    # On-chain sync
    manager.sync_nonce(25)
    assert manager.current_nonce == 26


def test_pre_allocated_payload_pool():
    """Test reusable payload pooling with cached indices."""
    pool = PreAllocatedPayloadPool(account_index=12345, api_key_index=2)

    order_payload = pool.create_order_payload(
        market_index=0,
        client_order_index=999,
        scaled_base_amount=500,
        scaled_price=270000,
        is_ask=False,
        order_type=0,
        time_in_force=2,
        nonce=42,
    )
    assert order_payload["account_index"] == 12345
    assert order_payload["api_key_index"] == 2
    assert order_payload["client_order_index"] == 999
    assert order_payload["scaled_base_amount"] if "scaled_base_amount" in order_payload else order_payload["base_amount"] == 500
    assert order_payload["nonce"] == 42

    cancel_payload = pool.create_cancel_payload(market_index=1, order_index=888, nonce=43)
    assert cancel_payload["market_index"] == 1
    assert cancel_payload["order_index"] == 888
    assert cancel_payload["nonce"] == 43


def test_fast_signer_create_order():
    """Test fast signing for single limit order creation."""
    signer = FastZkLighterSigner(
        account_index=12345,
        api_key_index=1,
        api_private_key="test_private_key_institutional_sub3ms",
        initial_nonce=1,
    )

    tx = signer.sign_create_order(
        market_index=0,  # ETH (price_decimals=2, size_decimals=3)
        client_order_index=101,
        base_amount=0.5,
        price=2750.25,
        is_ask=False,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.POST_ONLY,
    )

    assert isinstance(tx, FastSignedTransaction)
    assert tx.tx_type == "create_order"
    assert tx.market_index == 0
    assert tx.nonce == 1
    assert tx.payload["client_order_index"] == 101
    assert tx.payload["price"] == 275025  # 2750.25 * 100
    assert tx.payload["base_amount"] == 500  # 0.5 * 1000
    assert tx.payload["is_ask"] is False
    assert tx.signature.startswith("0x")
    assert tx.tx_hash.startswith("0x")
    assert tx.latency_us > 0.0
    assert tx.latency_ms < 3.0  # Ultra low latency


def test_fast_signer_cancel_and_batch_orders():
    """Test cancel and atomic batch order signing."""
    signer = FastZkLighterSigner(
        account_index=12345,
        api_key_index=1,
        api_private_key="test_private_key_institutional_sub3ms",
        initial_nonce=10,
    )

    # Cancel order
    cancel_tx = signer.sign_cancel_order(market_index=1, order_index=555)
    assert cancel_tx.tx_type == "cancel_order"
    assert cancel_tx.payload["order_index"] == 555
    assert cancel_tx.nonce == 10
    assert cancel_tx.latency_ms < 3.0

    # Batch orders
    orders = [
        {"client_order_index": 1, "price": 270000, "base_amount": 100, "is_ask": False},
        {"client_order_index": 2, "price": 271000, "base_amount": 100, "is_ask": True},
    ]
    batch_tx = signer.sign_batch_orders(orders=orders, market_index=0)
    assert batch_tx.tx_type == "batch_orders"
    assert len(batch_tx.payload["orders"]) == 2
    assert batch_tx.nonce == 11
    assert batch_tx.latency_ms < 3.0


def test_fast_signer_sub_3ms_benchmark_guarantee():
    """
    Rigorous institutional benchmark test:
    Executes 1,000 order signings and verifies p99 latency < 3.0 ms.
    """
    signer = FastZkLighterSigner(
        account_index=99999,
        api_key_index=0,
        api_private_key="benchmark_sub_3ms_private_key",
        initial_nonce=1,
    )

    results = signer.benchmark_signing_speed(iterations=1000)
    assert results["iterations"] == 1000.0
    assert results["sub_3ms_sla_passed"] == 1.0
    assert results["p99_ms"] < 3.0
    # In Python, pre-cached signing typically achieves < 0.1ms (100 µs)
    assert results["avg_us"] < 500.0
