"""Tests for Phase 11 upgrades: gas cap, limit orders, chunking, bot detection, early sells, A/B."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import phase11_upgrades as p11
import backtest_engine


def setup_module(module):
    # Use a throwaway DB for tests
    p11.DB_FILE = "test_phase11.db"
    backtest_engine.DB_FILE = "test_phase11.db"
    if os.path.exists("test_phase11.db"):
        os.remove("test_phase11.db")


def teardown_module(module):
    if os.path.exists("test_phase11.db"):
        os.remove("test_phase11.db")


def test_gas_cap_blocks_expensive_tx():
    os.environ["MAX_GAS_ETH_PER_TRADE"] = "0.001"
    # 500k gas at 10 gwei = 0.005 ETH worst case → blocked
    ok, cost, cap = p11.check_gas_cap(500_000, 10 * 10**9)
    assert not ok
    assert abs(cost - 0.005) < 1e-9
    # 100k gas at 5 gwei = 0.0005 ETH → allowed
    ok, cost, cap = p11.check_gas_cap(100_000, 5 * 10**9)
    assert ok


def test_gas_cap_disabled_with_zero():
    os.environ["MAX_GAS_ETH_PER_TRADE"] = "0"
    ok, _, _ = p11.check_gas_cap(10_000_000, 1000 * 10**9)
    assert ok
    os.environ["MAX_GAS_ETH_PER_TRADE"] = "0.0015"


def test_limit_order_lifecycle():
    oid = p11.add_limit_order("0xB20aaaa", 0.000001, 0.005, note="test")
    assert oid > 0
    orders = p11.list_limit_orders("open")
    assert any(o["id"] == oid for o in orders)
    assert p11.cancel_limit_order(oid)
    assert not p11.cancel_limit_order(oid)  # already cancelled
    assert not any(o["id"] == oid for o in p11.list_limit_orders("open"))


def test_limit_order_triggers_buy():
    oid = p11.add_limit_order("0xB20bbbb", 0.001, 0.002)
    fired = {}

    def fake_price(w3, token):
        return 0.0005  # below target → should trigger

    def fake_buy(w3, token, fee, amount, cfg, max_retries=1, force=False):
        fired["token"] = token
        fired["amount"] = amount
        return "0xtx"

    sent = []
    p11._last_limit_check = 0  # bypass rate limit
    p11.check_limit_orders(None, {}, fake_price, fake_buy, lambda m, **k: sent.append(m))
    time.sleep(0.5)  # buy fires in a thread
    assert fired.get("token") == "0xB20bbbb"
    assert fired.get("amount") == 0.002
    # Order must be marked triggered (no double-fire)
    assert not any(o["id"] == oid for o in p11.list_limit_orders("open"))


def test_should_chunk_thresholds():
    os.environ["CHUNKED_BUY_ENABLED"] = "true"
    os.environ["CHUNKED_BUY_MIN_TOTAL"] = "0.01"
    assert p11.should_chunk(0.02)
    assert not p11.should_chunk(0.001)
    os.environ["CHUNKED_BUY_ENABLED"] = "false"
    assert not p11.should_chunk(0.02)
    os.environ["CHUNKED_BUY_ENABLED"] = "true"


def test_chunked_buy_splits_amount():
    os.environ["CHUNK_COUNT"] = "3"
    os.environ["CHUNK_DELAY_SECS"] = "2"
    calls = []

    def fake_buy(w3, token, fee, amount, cfg, max_retries=1, force=False):
        calls.append(amount)
        return "0xtx"

    p11.chunked_buy(None, "0xB20cccc", 3000, 0.03, {}, buy_fn=fake_buy)
    time.sleep(5)  # wait for the 2 background chunks
    assert len(calls) == 3
    assert all(abs(a - 0.01) < 1e-9 for a in calls)


def test_known_bots_and_learning():
    os.environ["KNOWN_BOT_WALLETS"] = "0xDEADBEEF00000000000000000000000000000001"
    p11._known_bots_cache = None
    bots = p11.get_known_bots()
    assert "0xdeadbeef00000000000000000000000000000001" in bots
    # Learning: one hit isn't enough, two hits is
    p11.record_fast_buyer("0xBOT0000000000000000000000000000000000002")
    p11._known_bots_cache = None
    assert "0xbot0000000000000000000000000000000000002" not in p11.get_known_bots()
    p11.record_fast_buyer("0xBOT0000000000000000000000000000000000002")
    p11._known_bots_cache = None
    assert "0xbot0000000000000000000000000000000000002" in p11.get_known_bots()


def test_competition_skip_logic():
    os.environ["MAX_KNOWN_BOT_COMPETITORS"] = "1"

    class FakeMonitor:
        pending_swaps = {"0xB20dddd": [
            {"from": "0xdeadbeef00000000000000000000000000000001", "gas_price": 1},
        ]}

    p11._known_bots_cache = None
    skip, why = p11.should_skip_for_competition(FakeMonitor(), "0xB20dddd")
    assert skip
    assert "known sniper" in why
    os.environ["MAX_KNOWN_BOT_COMPETITORS"] = "2"


def test_early_sell_watchdog_alerts_on_drop():
    os.environ["EARLY_SELL_DROP_PCT"] = "30"
    p11.record_entry("0xB20eeee", 0.001)
    alerts = []
    p11._last_early_check = 0
    p11.check_early_sells(None, {}, lambda w3, t: 0.0005, lambda m, **k: alerts.append(m))
    assert alerts and "EARLY SELL" in alerts[0]
    # No duplicate alert
    p11._last_early_check = 0
    p11.check_early_sells(None, {}, lambda w3, t: 0.0004, lambda m, **k: alerts.append(m))
    assert len(alerts) == 1


def test_ab_variant_deterministic():
    assert p11.assign_variant("0xB20a2") == p11.assign_variant("0xB20a2")
    p11.record_ab_trade("0xB20a2", "A", 0.01)
    s = p11.ab_summary()
    assert s["A"]["trades"] >= 1


def test_backtest_engine_runs_empty():
    strategies = backtest_engine.evaluate_strategies([])
    assert strategies["actual"]["trades"] == 0


def test_backtest_tp_ladder_math():
    trips = [{"token": "0xB20f1", "eth_in": 0.01, "eth_out": 0.06, "tokens": 100, "exits": []}]  # 6x
    s = backtest_engine.evaluate_strategies(trips)
    assert s["actual"]["total_pnl_eth"] == 0.05
    # ladder: 25% @2x (0.005) + 25% @5x (0.0125) + 50% @6x (0.03) = 0.0475 out → +0.0375
    assert abs(s["tp_ladder_2x_5x"]["total_pnl_eth"] - 0.0375) < 1e-6
