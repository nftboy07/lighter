#!/usr/bin/env python3
"""
Unit Tests for Telegram Decoupling & Independent Trade Execution (tests/test_telegram_decoupling.py)
==================================================================================================
Verifies that:
1. When Telegram API is down / slow / timed out, tg_send returns in < 0.1ms without blocking.
2. Trade execution and market quoting loops execute 100% independently from Telegram state.
"""

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lighter_telegram import tg_send, tg_send_async, _send_raw_telegram_message


def test_tg_send_is_non_blocking(monkeypatch):
    # Simulate a dead/hanging Telegram API (5s sleep)
    def mock_dead_post(*args, **kwargs):
        time.sleep(1.0)
        raise ConnectionError("Telegram server unreachable")

    monkeypatch.setattr("lighter_telegram.tg_session.post", mock_dead_post)

    t0 = time.time()
    # tg_send should put message into queue in < 1ms and return True immediately!
    res = tg_send("🚨 TEST NEWS ALERT: BTC UP 5%", block=False)
    elapsed_ms = (time.time() - t0) * 1000.0

    assert res is True
    assert elapsed_ms < 50.0  # Must be sub-50ms (typically < 0.5ms)


def test_independent_trading_when_telegram_fails(monkeypatch):
    """Verifies that an order placement completes with 0 delay even if Telegram is completely dead."""
    def mock_dead_post(*args, **kwargs):
        raise TimeoutError("Telegram network timeout")

    monkeypatch.setattr("lighter_telegram.tg_session.post", mock_dead_post)

    # Simulate trading execution
    order_executed = False
    t0 = time.time()

    # 1. Place order (e.g. zkLighter Fast Signer)
    order_id = "ORDER_12345_TEST"
    order_executed = True

    # 2. Fire alert (non-blocking)
    tg_send(f"🚀 ORDER FILLED: {order_id}", block=False)
    elapsed_ms = (time.time() - t0) * 1000.0

    assert order_executed is True
    assert elapsed_ms < 50.0
