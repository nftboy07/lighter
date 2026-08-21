#!/usr/bin/env python3
"""
Unit & Integration Tests for Lighter News Catalyst Sniper Bot
============================================================
"""

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lighter_news_sniper import (
    NewsItem,
    ActivePosition,
    CatalystClassifier,
    MaxSizeExecutionEngine,
    CatalystSignal,
    unpack_signer_result,
)


def test_catalyst_classification_trump_hyperliquid():
    """Test instant identification of Trump + Hyperliquid catalyst."""
    classifier = CatalystClassifier(min_conviction=0.75)

    item = NewsItem(
        source="TruthSocial",
        headline="Donald Trump praises Hyperliquid as revolutionary crypto technology",
        body="Trump posted on Truth Social mentioning Hyperliquid and decentralized trading.",
        timestamp=time.time(),
    )

    signal = classifier.process_news(item)
    assert signal is not None
    assert signal.target_asset == "HYPE"
    assert signal.sentiment == "BULLISH"
    assert signal.conviction_score >= 0.95


def test_catalyst_classification_sec_etf():
    """Test SEC approval catalyst detection."""
    classifier = CatalystClassifier(min_conviction=0.75)

    item = NewsItem(
        source="TreeNews",
        headline="SEC officially approves spot Ethereum ETF trading to begin tomorrow",
        body="Regulatory filing confirms ETF launch.",
        timestamp=time.time(),
    )

    signal = classifier.process_news(item)
    assert signal is not None
    assert signal.target_asset == "ETH"
    assert signal.sentiment == "BULLISH"


def test_catalyst_classification_bearish_hack():
    """Test emergency bearish exploit detection."""
    classifier = CatalystClassifier(min_conviction=0.75)

    item = NewsItem(
        source="TreeNews",
        headline="Major cross-chain bridge drained for $150M in ongoing exploit",
        body="Security researchers alert community of massive exploit.",
        timestamp=time.time(),
    )

    signal = classifier.process_news(item)
    assert signal is not None
    assert signal.sentiment == "BEARISH"


def test_stale_news_and_deduplication_rejection():
    """Test that news older than 60s and duplicate headlines are rejected."""
    classifier = CatalystClassifier(max_news_age_sec=60.0)

    # 1. Stale news
    stale_item = NewsItem(
        source="OldFeed",
        headline="Donald Trump mentions Hyperliquid",
        body="Old text",
        timestamp=time.time() - 120.0,  # 2 minutes ago
    )
    assert classifier.process_news(stale_item) is None

    # 2. Fresh news -> Accepted
    fresh_item = NewsItem(
        source="FreshFeed",
        headline="Donald Trump mentions Hyperliquid in live interview",
        body="Fresh text",
        timestamp=time.time(),
    )
    assert classifier.process_news(fresh_item) is not None

    # 3. Duplicate news -> Rejected
    assert classifier.process_news(fresh_item) is None


def test_max_size_calculation():
    """Test aggressive max-size position calculation."""
    executor = MaxSizeExecutionEngine(
        max_margin_utilization_pct=80.0,
        slippage_tolerance_pct=1.5,
        is_live=False,
    )

    collateral_usd = 100.0
    current_price_usd = 2500.0  # ETH price

    # 80% of $100 = $80 -> $80 / 2500 = 0.032 ETH
    order_size = executor.calculate_max_order_size(collateral_usd, current_price_usd)
    assert order_size == 0.032


def test_paper_execution_pipeline():
    """Test full execution pipeline in paper simulation mode."""
    import asyncio
    executor = MaxSizeExecutionEngine(is_live=False)

    signal = CatalystSignal(
        news_id="test_123",
        headline="Trump mentions crypto surge",
        target_asset="ETH",
        market_index=0,
        sentiment="BULLISH",
        conviction_score=0.95,
        matched_keywords=["trump", "crypto"],
    )

    result = asyncio.run(executor.execute_catalyst_snipe(signal, current_market_price=2600.0))
    assert result["success"] is True
    assert result["mode"] == "PAPER_SIMULATION"
    assert result["side"] == "BUY/LONG"
    assert result["size_eth"] > 0


def test_exit_policy_and_trailing():
    from trade_exits import policy_for, trail_stop, tp_sl_prices
    fx = policy_for("EURUSD")
    assert fx.tp_pct < policy_for("BTC").tp_pct
    tp, sl = tp_sl_prices("BUY/LONG", 100.0, policy_for("BTC"))
    assert tp > 100 and sl < 100
    trailed = trail_stop("BUY/LONG", 100.0, 104.0, 100.0, 98.5, policy_for("BTC"))
    assert trailed >= 100.0


def test_tpsl_uses_matching_asset_price_only():
    import asyncio
    executor = MaxSizeExecutionEngine(is_live=False)
    executor.active_positions["pos1"] = ActivePosition(
        position_id="pos1",
        asset="BTC",
        market_index=1,
        side="BUY/LONG",
        entry_price=70000.0,
        size_eth=0.0001,
        notional_usd=7.0,
        tp_pct=2.5,
        sl_pct=1.5,
        highest_price=70000.0,
        lowest_price=70000.0,
    )
    # ETH mark must not stop out a BTC position
    events = asyncio.run(executor.check_take_profit_and_stop_loss({"ETH": 2300.0}))
    assert events == []
    assert executor.active_positions["pos1"].is_active
    events = asyncio.run(executor.check_take_profit_and_stop_loss({"BTC": 71000.0}))
    assert events == []
    events = asyncio.run(executor.check_take_profit_and_stop_loss({"BTC": 68000.0}))
    assert len(events) == 1
    assert events[0]["type"] == "STOP_LOSS"
    assert events[0]["asset"] == "BTC"


def test_signer_result_unpacks_three_tuple():
    resp, err = unpack_signer_result(("tx", "resp-hash", None))
    assert err is None
    assert resp == "resp-hash"
    resp, err = unpack_signer_result((None, None, "rejected"))
    assert resp is None
    assert err == "rejected"
    resp, err = unpack_signer_result(("hash-only", None))
    assert resp == "hash-only"


def test_live_execute_fails_closed_without_wallet(monkeypatch):
    import asyncio
    monkeypatch.delenv("WALLET_ADDRESS", raising=False)
    executor = MaxSizeExecutionEngine(is_live=True)
    result = asyncio.run(executor.execute_trade(current_market_price=2500.0))
    assert result["success"] is False
    assert "collateral" in result["error"]


def test_one_position_per_market():
    import asyncio
    executor = MaxSizeExecutionEngine(is_live=False)
    first = asyncio.run(executor.execute_trade(asset="ETH", current_market_price=2600.0))
    assert first["success"] is True
    assert first["asset"] == "ETH"
    second = asyncio.run(executor.execute_trade(asset="ETH", current_market_price=2610.0))
    assert second["success"] is False
    assert "already" in second["error"]


def test_time_stop_fires():
    import asyncio
    executor = MaxSizeExecutionEngine(is_live=False)
    executor.active_positions["pos_t"] = ActivePosition(
        position_id="pos_t",
        asset="AAPL",
        market_index=12,
        side="BUY/LONG",
        entry_price=200.0,
        size_eth=0.2,
        notional_usd=40.0,
        tp_pct=1.5,
        sl_pct=1.0,
        tp_price=203.0,
        sl_price=198.0,
        highest_price=200.0,
        lowest_price=200.0,
        entry_time=time.time() - 91 * 60,
        max_hold_seconds=90 * 60,
    )
    events = asyncio.run(executor.check_take_profit_and_stop_loss({"AAPL": 200.2}))
    assert len(events) == 1
    assert events[0]["type"] == "TIME_STOP"


def test_chase_headline_is_not_macro_trade():
    from news_direction import classify_with_body
    kind, direction, _ = classify_with_body("Crypto Stocks Soar Alongside Bitcoin As Traders Cheer", "")
    assert kind in {"unknown", "opinion"}
    assert direction in {"BULLISH", "NEUTRAL"}


def test_classify_orphan_and_stale_orders():
    from trade_exits import classify_working_order
    now = 1_700_000_000.0
    live_m, live_s = {51}, {"XCU"}
    assert classify_working_order(
        {"market_id": 1, "symbol": "BTC", "timestamp": now - 10, "reduce_only": True},
        live_m, live_s, now, 2700,
    ) == "orphan"
    assert classify_working_order(
        {"market_id": 51, "symbol": "XCU", "timestamp": now - 10, "reduce_only": True},
        live_m, live_s, now, 2700,
    ) == "keep"
    assert classify_working_order(
        {"market_id": 51, "symbol": "XCU", "timestamp": now - 4000, "reduce_only": True},
        live_m, live_s, now, 2700,
    ) == "stale_protect"
    assert classify_working_order(
        {"market_id": 51, "symbol": "XCU", "timestamp": now - 120, "reduce_only": False},
        live_m, live_s, now, 2700,
    ) == "stale_entry"


def test_paper_cancel_orders_is_noop():
    import asyncio
    executor = MaxSizeExecutionEngine(is_live=False)
    assert asyncio.run(executor.cancel_open_orders(1, [2, 3])) == 0
    assert asyncio.run(executor.harvest_exchange_exits({"ETH": 2600.0})) == []
    care = asyncio.run(executor.care_open_orders({"ETH": 2600.0}))
    assert care["cancelled"] == 0
    assert care["flattened"] == 0


def test_fx_tighter_than_crypto_and_limit_slip():
    from trade_exits import policy_for, protect_limit_price, parse_manual_trade, trail_stop
    assert policy_for("EURUSD").tp_pct < policy_for("NVDA").tp_pct < policy_for("BTC").tp_pct
    long_sl = protect_limit_price("BUY/LONG", "sl", 100.0)
    short_sl = protect_limit_price("SELL/SHORT", "sl", 100.0)
    assert long_sl < 100.0 < short_sl
    assert parse_manual_trade("buy aapl") == ("AAPL", False)
    assert parse_manual_trade("short gold") == ("XAU", True)
    assert parse_manual_trade("nvda") == ("NVDA", False)
    assert parse_manual_trade("close") is None
    trailed = trail_stop("SELL/SHORT", 100.0, 100.0, 96.0, 101.5, policy_for("BTC"))
    assert trailed <= 100.0


def test_parse_account_position_uses_sign_and_market_zero():
    executor = MaxSizeExecutionEngine(is_live=False)
    xrp = executor.parse_account_position({
        "symbol": "XRP",
        "market_id": 7,
        "position": "898",
        "sign": -1,
        "avg_entry_price": "1.336258",
    })
    assert xrp["side"] == "SELL/SHORT"
    assert xrp["size"] == 898
    assert xrp["market_index"] == 7
    eth = executor.parse_account_position({
        "symbol": "ETH",
        "market_id": 0,
        "position": "0.0214",
        "sign": 1,
        "avg_entry_price": "2333.11",
    })
    assert eth["side"] == "BUY/LONG"
    assert eth["market_index"] == 0
    baba = executor.parse_account_position({
        "symbol": "BABA",
        "market_id": 177,
        "position": "0.3887",
        "sign": 1,
        "avg_entry_price": "128.78",
        "open_order_count": 16,
        "position_tied_order_count": 16,
    })
    assert baba["open_order_count"] == 16


def test_zero_size_decimals_not_replaced_with_four():
    executor = MaxSizeExecutionEngine(is_live=False)
    assert executor._int_or(0, 4) == 0
    assert executor._int_or(None, 4) == 4
    executor.market_meta["XRP"] = {"size_decimals": 0, "price_decimals": 6, "market_index": 7}
    meta = executor._meta("XRP")
    assert meta["size_decimals"] == 0
    assert meta["price_decimals"] == 6
    size_int = int(198 * (10 ** meta["size_decimals"]))
    assert size_int == 198


def test_ensure_exit_prices_always_sets_tp_sl():
    executor = MaxSizeExecutionEngine(is_live=False)
    pos = ActivePosition(
        position_id="x",
        asset="XRP",
        market_index=7,
        side="SELL/SHORT",
        entry_price=1.336,
        size_eth=198,
        notional_usd=264,
        tp_pct=0.0,
        sl_pct=0.0,
        tp_price=0.0,
        sl_price=0.0,
    )
    executor.ensure_exit_prices(pos)
    assert pos.tp_price > 0 and pos.tp_price < 1.336
    assert pos.sl_price > 1.336
    assert pos.tp_pct > 0 and pos.sl_pct > 0


def test_already_through_exit_short_and_long():
    from trade_exits import already_through_exit
    assert already_through_exit("SELL/SHORT", 1.266, 1.302, 1.356) == "TAKE_PROFIT"
    assert already_through_exit("BUY/LONG", 103.0, 102.5, 98.5) == "TAKE_PROFIT"
    assert already_through_exit("BUY/LONG", 98.0, 102.5, 98.5) == "STOP_LOSS"
    assert already_through_exit("BUY/LONG", 100.0, 102.5, 98.5) is None


def test_position_clock_survives_restart(tmp_path):
    from news_lifecycle import PositionClock
    db = str(tmp_path / "clock.db")
    clock = PositionClock(db)
    first = time.time() - 3600
    assert clock.remember("XRP", first) == first
    assert clock.remember("XRP", time.time()) == first
    restored = PositionClock(db)
    assert restored.recall("XRP") == first
    restored.forget("XRP")
    assert PositionClock(db).recall("XRP") is None


def test_short_take_profit_uses_down_move():
    import asyncio
    executor = MaxSizeExecutionEngine(is_live=False)
    executor.active_positions["xrp"] = ActivePosition(
        position_id="xrp",
        asset="XRP",
        market_index=7,
        side="SELL/SHORT",
        entry_price=1.336258,
        size_eth=898,
        notional_usd=1200,
        tp_pct=2.5,
        sl_pct=1.5,
        tp_price=1.336258 * 0.975,
        sl_price=1.336258 * 1.015,
        highest_price=1.336258,
        lowest_price=1.266,
    )
    events = asyncio.run(executor.check_take_profit_and_stop_loss({"XRP": 1.266}))
    assert events and events[0]["type"].startswith("PARTIAL_TP")
    assert events[0]["asset"] == "XRP"
    assert events[0]["pnl_usd"] > 0


def test_match_exchange_position_uses_symbol():
    executor = MaxSizeExecutionEngine(is_live=False)
    books = [
        {"symbol": "ETH", "market_index": 0, "size": 0.01},
        {"symbol": "WTI", "market_index": 44, "size": 0.5},
        {"symbol": "XCU", "market_index": 51, "size": 7.66},
    ]
    assert executor.match_exchange_position(books, "XCU", 0)["symbol"] == "XCU"
    assert executor.match_exchange_position(books, "ETH", 0)["symbol"] == "ETH"
    assert executor.match_exchange_position(books, "AAPL", 0) is None


def test_paper_flatten_confirms():
    import asyncio
    executor = MaxSizeExecutionEngine(is_live=False)
    asyncio.run(executor.execute_trade(asset="ETH", current_market_price=2600.0))
    asyncio.run(executor.execute_trade(asset="BTC", current_market_price=70000.0))
    closed = asyncio.run(executor.close_all_positions({"ETH": 2610.0, "BTC": 70100.0}))
    assert closed == 2
    assert all(not p.is_active for p in executor.active_positions.values())


def test_xaut_commodity_policy_and_manual_aliases():
    from trade_exits import policy_for, parse_manual_trade
    assert policy_for("XAUT").tp_pct == policy_for("XAU").tp_pct
    assert policy_for("SPY").tp_pct == policy_for("QQQ").tp_pct
    assert parse_manual_trade("xaut") == ("XAUT", False)
    assert parse_manual_trade("short spy") == ("SPY", True)
    assert parse_manual_trade("buy qqq") == ("QQQ", False)


def test_scale_out_ladder_and_partial_qty():
    from trade_exits import infer_tp_hits, partial_qty, policy_for, scale_tp_price, scaled_out_qty, tp_ladder_prices, breakeven_sl
    policy = policy_for("BTC")
    ladder = tp_ladder_prices("BUY/LONG", 100.0, policy)
    assert ladder[0] < ladder[1] < ladder[2]
    assert abs(ladder[0] - scale_tp_price("BUY/LONG", 100.0, policy, 1)) < 1e-9
    assert partial_qty(100, 100, 1) == 50
    assert partial_qty(100, 50, 2) == 25
    assert partial_qty(100, 25, 3) == 25
    assert scaled_out_qty(100, 100, 1) == 50
    assert scaled_out_qty(100, 100, 2) == 75
    assert infer_tp_hits("BUY/LONG", 100.0, 102.0, policy) == 1
    assert infer_tp_hits("BUY/LONG", 100.0, 104.0, policy) == 2
    short_l = tp_ladder_prices("SELL/SHORT", 100.0, policy)
    assert short_l[0] > short_l[1] > short_l[2]
    assert abs(breakeven_sl("BUY/LONG", 100.0) - 100.1) < 1e-9
    assert abs(breakeven_sl("SELL/SHORT", 100.0) - 99.9) < 1e-9


def test_partial_tp_watchdog_scales_then_keeps_runner():
    import asyncio
    executor = MaxSizeExecutionEngine(is_live=False)
    pos = ActivePosition(
        position_id="spy",
        asset="SPY",
        market_index=90,
        side="BUY/LONG",
        entry_price=100.0,
        size_eth=4.0,
        original_size=4.0,
        notional_usd=400.0,
        tp_pct=2.00,
        sl_pct=0.80,
        highest_price=100.0,
        lowest_price=100.0,
    )
    executor.active_positions["spy"] = pos
    executor.ensure_exit_prices(pos)
    # Through TP1 (2.0%) and TP2 (4.0%)
    events = asyncio.run(executor.check_take_profit_and_stop_loss({"SPY": 104.5}))
    assert [ev["type"] for ev in events] == ["PARTIAL_TP_1", "PARTIAL_TP_2"]
    assert events[0]["close_qty"] == 2.0
    assert events[1]["close_qty"] == 1.0
    assert not events[0]["full"] and not events[1]["full"]
    asyncio.run(executor.close_position(pos, 104.5, qty=events[0]["close_qty"]))
    asyncio.run(executor.close_position(pos, 104.5, qty=events[1]["close_qty"]))
    pos.tp_hits = 2
    executor.ensure_exit_prices(pos)
    assert pos.size_eth == 1.0
    assert pos.tp_price == 0.0
    assert pos.trail_gap_pct == 1.0
    assert abs(pos.sl_price - 100.1) < 1e-9


def test_dynamic_kelly_sizing_conviction_scale():
    from trade_exits import dynamic_kelly_margin
    assert abs(dynamic_kelly_margin(0.98) - 90.0) < 1e-6
    assert abs(dynamic_kelly_margin(0.85) - 65.0) < 1e-6
    assert abs(dynamic_kelly_margin(0.75) - 40.0) < 1e-6

    executor = MaxSizeExecutionEngine(is_live=False)
    collateral = 1000.0
    price = 2500.0
    # 98% conviction -> 90% size = $900 / 2500 = 0.36
    assert executor.calculate_max_order_size(collateral, price, conviction=0.98) == 0.36
    # 85% conviction -> 65% size = $650 / 2500 = 0.26
    assert executor.calculate_max_order_size(collateral, price, conviction=0.85) == 0.26
    # 75% conviction -> 40% size = $400 / 2500 = 0.16
    assert executor.calculate_max_order_size(collateral, price, conviction=0.75) == 0.16


def test_scale_out_ladder_level1_shifts_sl_to_breakeven_and_runner_trails():
    import asyncio
    executor = MaxSizeExecutionEngine(is_live=False)
    pos = ActivePosition(
        position_id="eth_pos",
        asset="ETH",
        market_index=0,
        side="BUY/LONG",
        entry_price=2000.0,
        size_eth=10.0,
        original_size=10.0,
        notional_usd=20000.0,
        tp_pct=2.0,
        sl_pct=1.5,
        highest_price=2000.0,
        lowest_price=2000.0,
    )
    executor.active_positions["eth_pos"] = pos
    executor.ensure_exit_prices(pos)
    # Price reaches +2.0% profit ($2040)
    events = asyncio.run(executor.check_take_profit_and_stop_loss({"ETH": 2040.0}))
    assert len(events) == 1
    assert events[0]["type"] == "PARTIAL_TP_1"
    assert events[0]["close_qty"] == 5.0  # 50%
    # SL shifted to Breakeven (+0.1% = 2002.0)
    assert abs(pos.sl_price - 2002.0) < 1e-6


def test_ticker_news_source_and_catalog_diff(tmp_path):
    from news_sources import NewsSourceRegistry, register_ticker_sources, ticker_news_source
    from news_universe import sync_catalog
    src = ticker_news_source("XAUT")
    assert src.source_id == "tkr_xaut"
    assert "XAUT" in src.url
    path = tmp_path / "universe.json"
    new, first = sync_catalog(["ETH", "XAUT", "SPY"], path=path)
    assert first
    new2, first2 = sync_catalog(["ETH", "XAUT", "SPY", "QQQ"], path=path)
    assert not first2
    assert new2 == ["QQQ"]
    registry = NewsSourceRegistry()
    added = register_ticker_sources(registry, ["XAUT", "FOOBAR"])
    assert "FOOBAR" in added
    assert registry.get("tkr_foobar").url.startswith("https://news.google.com/rss/search")
    assert "FOOBAR" in registry.get("tkr_foobar").url
    assert registry.get("tkr_xaut").source_id == "tkr_xaut"
    assert registry.get("tkr_spy").url.startswith("https://news.google.com/rss/search")
    assert registry.get("tkr_qqq").url.startswith("https://news.google.com/rss/search")
