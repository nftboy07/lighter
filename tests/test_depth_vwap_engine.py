#!/usr/bin/env python3
"""
Unit & Integration Tests for WebSocket L2/L3 Live Depth & Microstructure VWAP Sizing Engine
==========================================================================================
Tests:
- MicrostructureDepthBook in-memory state, microsecond timestamping, sorted invariants
- Delta updates (insert, update, delete) on bids and asks
- calculate_vwap on BUY (walking asks) and SELL (walking bids)
- liquidity_adjusted_size enforcing strict slippage caps
- Top-of-book and fallback behaviors on low/empty depth
- Microstructure alpha signals (Micro-price, OBI, depth density)
- Integration with lighter_execution.py and lighter_news_sniper.py
"""

import math
import os
import sys
import time
import pytest

# Ensure repository root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from depth_vwap_engine import (
    DepthLevel,
    DepthVWAPEngine,
    MicrostructureDepthBook,
    OrderBookSide,
    calculate_vwap,
    global_depth_vwap_engine,
    liquidity_adjusted_size,
    normalize_side,
)
from lighter_strategy import L2OrderBook, OrderBookLevel, OrderSide
from lighter_execution import LighterExecutionEngine, LighterWebSocketStreamer
from lighter_news_sniper import MaxSizeExecutionEngine, CatalystSignal


# =============================================================================
# 1. DEPTH BOOK STATE & MICROSECOND DELTA UPDATE TESTS
# =============================================================================

def test_depth_book_initialization_and_snapshot():
    book = MicrostructureDepthBook(market_index=0, symbol="ETH")
    assert book.best_bid == 0.0
    assert book.best_ask == float("inf")
    assert book.mid_price == 0.0
    assert book.spread == 0.0

    bids_data = [
        (3000.0, 2.0),
        (2990.0, 5.0),
        (2980.0, 10.0),
    ]
    asks_data = [
        (3005.0, 1.5),
        (3010.0, 4.0),
        (3020.0, 8.0),
    ]

    book.load_snapshot(bids=bids_data, asks=asks_data, nonce=1001)

    assert book.best_bid == 3000.0
    assert book.best_bid_size == 2.0
    assert book.best_ask == 3005.0
    assert book.best_ask_size == 1.5
    assert book.mid_price == 3002.5
    assert book.spread == 5.0
    assert pytest.approx(book.spread_bps, 0.01) == (5.0 / 3002.5) * 10000.0
    assert book.nonce == 1001

    # Verify sorting invariants
    bids = book.get_bids()
    assert len(bids) == 3
    for i in range(len(bids) - 1):
        assert bids[i].price > bids[i + 1].price

    asks = book.get_asks()
    assert len(asks) == 3
    for i in range(len(asks) - 1):
        assert asks[i].price < asks[i + 1].price


def test_depth_book_microsecond_delta_updates():
    book = MicrostructureDepthBook(market_index=0, symbol="ETH")
    book.load_snapshot(
        bids=[(3000.0, 2.0), (2990.0, 5.0)],
        asks=[(3010.0, 2.0), (3020.0, 5.0)],
    )

    t1 = int(time.time() * 1_000_000)
    # 1. Insert new top of book bid @ 3005.0
    book.apply_delta(side=OrderBookSide.BUY, price=3005.0, size=3.0, timestamp_us=t1)
    assert book.best_bid == 3005.0
    assert book.best_bid_size == 3.0
    assert len(book.sorted_bid_prices) == 3
    assert book.sorted_bid_prices == [3005.0, 3000.0, 2990.0]

    # 2. Update existing bid size @ 3000.0
    t2 = t1 + 50
    book.apply_delta(side="BUY", price=3000.0, size=10.0, timestamp_us=t2)
    assert book.bids_map[3000.0].size == 10.0
    assert book.sorted_bid_prices == [3005.0, 3000.0, 2990.0]

    # 3. Remove top bid @ 3005.0 (size = 0)
    t3 = t2 + 50
    book.apply_delta(side="BUY", price=3005.0, size=0.0, timestamp_us=t3)
    assert book.best_bid == 3000.0
    assert 3005.0 not in book.bids_map
    assert book.sorted_bid_prices == [3000.0, 2990.0]

    # 4. Insert new top ask @ 3008.0
    book.apply_delta(side=OrderBookSide.SELL, price=3008.0, size=1.0)
    assert book.best_ask == 3008.0
    assert book.sorted_ask_prices == [3008.0, 3010.0, 3020.0]

    # 5. Remove ask @ 3010.0
    book.apply_delta(side="SELL", price=3010.0, size=0.0)
    assert 3010.0 not in book.asks_map
    assert book.sorted_ask_prices == [3008.0, 3020.0]


# =============================================================================
# 2. VWAP CALCULATION TESTS
# =============================================================================

def test_calculate_vwap_buy_side():
    book = MicrostructureDepthBook(market_index=0, symbol="ETH")
    book.load_snapshot(
        bids=[(2990.0, 5.0)],
        asks=[
            (3000.0, 1.0),  # $3000 USD
            (3010.0, 2.0),  # $6020 USD
            (3030.0, 5.0),  # $15150 USD
        ],
    )

    # 1. Fill within first level ($1500 USD)
    vwap, filled_usd, slippage_bps, exhausted = calculate_vwap(
        orderbook=book,
        side="BUY",
        target_notional_usd=1500.0,
    )
    assert pytest.approx(vwap, 0.001) == 3000.0
    assert pytest.approx(filled_usd, 0.001) == 1500.0
    assert slippage_bps == 0.0
    assert exhausted is False

    # 2. Fill across first 2 levels ($3000 + $3010 = $6010 USD -> 1.0 + 1.0 = 2.0 units)
    vwap2, filled_usd2, slippage_bps2, exhausted2 = calculate_vwap(
        orderbook=book,
        side=OrderBookSide.BUY,
        target_notional_usd=6010.0,
    )
    # Total filled qty: 1.0 @ 3000 + 1.0 @ 3010 = 2.0 ETH
    # Total USD: $6010. VWAP = 6010 / 2.0 = 3005.0
    assert pytest.approx(vwap2, 0.001) == 3005.0
    assert pytest.approx(filled_usd2, 0.001) == 6010.0
    # Slippage: (3005 - 3000) / 3000 * 10000 = 16.6667 bps
    assert pytest.approx(slippage_bps2, 0.01) == (5.0 / 3000.0) * 10000.0
    assert exhausted2 is False

    # 3. Fill exceeding total book depth ($50,000 USD > $24,170 available)
    total_avail = (1.0 * 3000) + (2.0 * 3010) + (5.0 * 3030)
    vwap3, filled_usd3, slippage_bps3, exhausted3 = calculate_vwap(
        orderbook=book,
        side="BUY",
        target_notional_usd=50000.0,
    )
    assert pytest.approx(filled_usd3, 0.01) == total_avail
    assert exhausted3 is True


def test_calculate_vwap_sell_side():
    book = MicrostructureDepthBook(market_index=0, symbol="ETH")
    book.load_snapshot(
        bids=[
            (3000.0, 1.0),  # $3000 USD
            (2990.0, 2.0),  # $5980 USD
            (2970.0, 5.0),  # $14850 USD
        ],
        asks=[(3005.0, 5.0)],
    )

    # Walk bids for $5990 USD: 1.0 @ 3000 ($3000) + 1.0 @ 2990 ($2990) = 2.0 ETH
    vwap, filled_usd, slippage_bps, exhausted = calculate_vwap(
        orderbook=book,
        side=OrderSide.SELL,
        target_notional_usd=5990.0,
    )
    # Total filled qty = 2.0 ETH for $5990 USD -> VWAP = 2995.0
    assert pytest.approx(vwap, 0.001) == 2995.0
    assert pytest.approx(filled_usd, 0.001) == 5990.0
    # Slippage: (3000 - 2995) / 3000 * 10000 = 16.6667 bps
    assert pytest.approx(slippage_bps, 0.01) == (5.0 / 3000.0) * 10000.0
    assert exhausted is False


# =============================================================================
# 3. LIQUIDITY-ADJUSTED SIZING TESTS
# =============================================================================

def test_liquidity_adjusted_size_strict_slippage_cap():
    book = MicrostructureDepthBook(market_index=0, symbol="ETH")
    book.load_snapshot(
        bids=[(100.0, 10.0)],
        asks=[
            (100.0, 10.0),  # $1000 USD (0 bps)
            (102.0, 10.0),  # $1020 USD (+200 bps)
            (110.0, 10.0),  # $1100 USD (+1000 bps)
        ],
    )

    # 1. Cap is 50 bps (max allowable VWAP = 100.0 * 1.0050 = 100.50)
    # Requested $5000 USD
    max_slippage_bps = 50.0
    adj_usd = liquidity_adjusted_size(
        orderbook=book,
        side="BUY",
        requested_usd=5000.0,
        max_slippage_bps=max_slippage_bps,
    )

    # Check VWAP of adjusted size
    vwap, filled_usd, slippage_bps, exhausted = calculate_vwap(
        orderbook=book,
        side="BUY",
        target_notional_usd=adj_usd,
    )

    assert adj_usd < 5000.0
    assert pytest.approx(filled_usd, 0.01) == adj_usd
    assert slippage_bps <= max_slippage_bps + 1e-4
    assert vwap <= 100.50 + 1e-4

    # 2. Relax cap to 500 bps -> can absorb more depth
    adj_usd_relaxed = liquidity_adjusted_size(
        orderbook=book,
        side="BUY",
        requested_usd=5000.0,
        max_slippage_bps=500.0,
    )
    assert adj_usd_relaxed > adj_usd


def test_liquidity_adjusted_size_sell_side():
    book = MicrostructureDepthBook(market_index=0, symbol="ETH")
    book.load_snapshot(
        bids=[
            (100.0, 10.0),  # $1000 USD (0 bps)
            (98.0, 10.0),   # $980 USD (-200 bps)
            (90.0, 10.0),   # $900 USD (-1000 bps)
        ],
        asks=[(105.0, 10.0)],
    )

    # Max slippage cap: 50 bps (min allowable VWAP = 100.0 * 0.9950 = 99.50)
    adj_usd = liquidity_adjusted_size(
        orderbook=book,
        side="SELL",
        requested_usd=5000.0,
        max_slippage_bps=50.0,
    )

    vwap, filled_usd, slippage_bps, exhausted = calculate_vwap(
        orderbook=book,
        side="SELL",
        target_notional_usd=adj_usd,
    )

    assert adj_usd < 5000.0
    assert slippage_bps <= 50.0 + 1e-4
    assert vwap >= 99.50 - 1e-4


def test_depth_vwap_fallback_on_empty_depth():
    empty_book = MicrostructureDepthBook(market_index=0)

    # Empty book with fallback price
    vwap, filled_usd, slippage_bps, exhausted = calculate_vwap(
        orderbook=empty_book,
        side="BUY",
        target_notional_usd=1000.0,
        fallback_price=2500.0,
    )
    assert vwap == 2500.0
    assert filled_usd == 1000.0
    assert slippage_bps == 0.0
    assert exhausted is True

    # Liquidity adjusted size on empty book returns requested
    adj = liquidity_adjusted_size(
        orderbook=empty_book,
        side="BUY",
        requested_usd=1000.0,
        fallback_price=2500.0,
    )
    assert adj == 1000.0


# =============================================================================
# 4. MICROSTRUCTURE ALPHA INDICATOR TESTS
# =============================================================================

def test_microstructure_alpha_indicators():
    book = MicrostructureDepthBook(market_index=0, symbol="ETH")
    book.load_snapshot(
        bids=[OrderBookLevel(price=3000.0, size=4.0)],
        asks=[OrderBookLevel(price=3002.0, size=1.0)],
    )

    # S_micro = (4.0 * 3002.0 + 1.0 * 3000.0) / 5.0 = 15008 / 5 = 3001.6
    micro = book.calculate_micro_price()
    assert pytest.approx(micro, 0.001) == 3001.6

    # OBI = (4.0 - 1.0) / (4.0 + 1.0) = 3.0 / 5.0 = +0.60
    obi = book.calculate_order_book_imbalance()
    assert pytest.approx(obi, 0.001) == 0.60

    # Total USD depth
    assert book.get_total_depth_usd(OrderBookSide.BUY) == 12000.0
    assert book.get_total_depth_usd(OrderBookSide.SELL) == 3002.0


# =============================================================================
# 5. INTEGRATION TESTS WITH LIGHTER EXECUTION & NEWS SNIPER
# =============================================================================

@pytest.mark.asyncio
async def test_execution_engine_taker_snipe_vwap_integration():
    engine = LighterExecutionEngine(is_paper_mode=True, market_index=0)

    # Populate engine depth book
    book = engine.depth_engine.get_or_create_book(market_index=0)
    book.load_snapshot(
        bids=[(2500.0, 5.0)],
        asks=[
            (2500.0, 1.0),  # $2500
            (2505.0, 2.0),  # $5010
        ],
    )

    res = await engine.execute_taker_snipe(
        side=OrderSide.BUY,
        price=2500.0,
        size=2.0,  # $5000 notional
        orderbook=book,
        max_slippage_bps=50.0,
    )

    assert res["success"] is True
    assert "vwap_price" in res
    assert res["vwap_price"] >= 2500.0
    assert "expected_slippage_bps" in res
    assert res["mode"] == "PAPER"


@pytest.mark.asyncio
async def test_news_sniper_max_size_vwap_sizing_guard():
    sniper = MaxSizeExecutionEngine(is_live=False, default_tp_pct=2.5, default_sl_pct=1.5)

    # Setup book with shallow liquidity
    book = sniper.depth_engine.get_or_create_book(market_index=0, symbol="ETH")
    book.load_snapshot(
        bids=[(3000.0, 10.0)],
        asks=[
            (3000.0, 0.05),  # $150 USD
            (3050.0, 10.0),  # $30,500 USD (+166 bps jump)
        ],
    )

    # Execute trade for $1000 USD
    res = await sniper.execute_trade(
        asset="ETH",
        market_index=0,
        is_ask=False,
        current_market_price=3000.0,
        notional_usd=1000.0,
    )

    assert res["success"] is True
    assert "vwap_price" in res
    assert "expected_slippage_bps" in res
    assert "depth_exhausted" in res


def test_depth_vwap_engine_multi_market_registry():
    engine = DepthVWAPEngine()
    book_eth = engine.get_or_create_book(0, "ETH")
    book_btc = engine.get_or_create_book(1, "BTC")

    book_eth.load_snapshot(bids=[(3000.0, 5.0)], asks=[(3001.0, 5.0)])
    book_btc.load_snapshot(bids=[(90000.0, 1.0)], asks=[(90050.0, 1.0)])

    res_eth = engine.calculate_sizing_and_vwap(0, "BUY", requested_usd=5000.0)
    assert res_eth["market_index"] == 0
    assert res_eth["executable_usd"] == 5000.0
    assert res_eth["vwap_price"] == 3001.0

    res_btc = engine.calculate_sizing_and_vwap(1, "SELL", requested_usd=45000.0)
    assert res_btc["market_index"] == 1
    assert res_btc["executable_usd"] == 45000.0
    assert res_btc["vwap_price"] == 90000.0
