#!/usr/bin/env python3
"""
Unit and Integration Tests for Sub-15ms TreeNews WebSocket Ingestion Client
==========================================================================
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from treenews_ws import TreeNewsWebSocketClient, TreeNewsClientStats
from news_sources import RawNewsRecord


def test_treenews_parse_payload_standard_json():
    """Test parsing standard TreeNews JSON event."""
    client = TreeNewsWebSocketClient()
    payload = {
        "_id": "65b1c8f123456789",
        "title": "Federal Reserve announces emergency rate cut of 50bps",
        "body": "The Federal Open Market Committee decided to lower the target range for federal funds rate.",
        "source": "Bloomberg",
        "link": "https://bloomberg.com/news/fed-rate-cut",
        "time": 1706000000000,
        "symbols": ["BTC", "ETH"],
    }
    
    records = client.parse_payload(payload)
    assert len(records) == 1
    rec = records[0]
    assert rec.source_id == "tree_news"
    assert rec.publisher == "TreeNews"
    assert rec.title == "Federal Reserve announces emergency rate cut of 50bps"
    assert "Federal Open Market Committee" in rec.body
    assert rec.url == "https://bloomberg.com/news/fed-rate-cut"
    assert rec.guid == "65b1c8f123456789"
    assert rec.trust_score == 0.85
    assert rec.category == "media"
    assert rec.raw["adapter"] == "treenews_ws"
    assert rec.raw["tree_source"] == "Bloomberg"
    assert "BTC" in rec.raw["symbols"]
    assert rec.raw["ws_latency_ms"] < 15.0  # Must be sub-15ms ingestion time


def test_treenews_parse_payload_string_json():
    """Test parsing JSON string payload."""
    client = TreeNewsWebSocketClient()
    payload_str = json.dumps({
        "title": "SEC Approves First Spot Solana ETF Application",
        "source": "Reuters",
        "url": "https://reuters.com/markets/sol-etf-approved",
        "time": 1707000000,
    })

    records = client.parse_payload(payload_str)
    assert len(records) == 1
    assert records[0].title == "SEC Approves First Spot Solana ETF Application"
    assert records[0].url == "https://reuters.com/markets/sol-etf-approved"


def test_treenews_parse_payload_batch_array():
    """Test parsing batch array of news items."""
    client = TreeNewsWebSocketClient()
    payload = [
        {"title": "Binance to list SUI perpetual contracts", "time": 1708000000},
        {"title": "BlackRock files updated Ethereum S-1 amendment", "time": 1708000005},
    ]

    records = client.parse_payload(payload)
    assert len(records) == 2
    assert records[0].title == "Binance to list SUI perpetual contracts"
    assert records[1].title == "BlackRock files updated Ethereum S-1 amendment"


def test_treenews_ignores_heartbeats_and_empty():
    """Test that ping, pong, and control frames are ignored."""
    client = TreeNewsWebSocketClient()
    assert client.parse_payload("") == []
    assert client.parse_payload("pong") == []
    assert client.parse_payload("ping") == []
    assert client.parse_payload({"type": "ping"}) == []
    assert client.parse_payload({"type": "heartbeat"}) == []
    assert client.parse_payload({"event": "subscribed"}) == []


@pytest.mark.asyncio
async def test_treenews_zero_latency_dispatch():
    """Test sub-15ms dispatch from receive to pipeline handler."""
    dispatched_events = []

    def mock_on_news(records):
        dispatched_events.extend(records)

    client = TreeNewsWebSocketClient(on_records=mock_on_news)
    
    payload = json.dumps({
        "title": "Trump announces US Strategic Bitcoin Reserve executive order",
        "body": "Official signing ceremony at 2 PM.",
        "time": 1709000000,
    })

    t0 = time.perf_counter()
    await client._handle_message(payload)
    latency_ms = (time.perf_counter() - t0) * 1000.0

    assert len(dispatched_events) == 1
    assert "Strategic Bitcoin Reserve" in dispatched_events[0].title
    assert client.stats.messages_received == 1
    assert client.stats.records_dispatched == 1
    assert latency_ms < 15.0  # Confirms sub-15ms execution latency


@pytest.mark.asyncio
async def test_treenews_client_lifecycle_and_reconnect():
    """Test client start, stop, and clean state handling."""
    client = TreeNewsWebSocketClient(
        ws_url="ws://127.0.0.1:59999/invalid_ws_for_test",
        reconnect_initial_delay=0.05,
        connect_timeout=0.05,
    )
    
    task = client.start()
    assert client.is_running
    
    # Wait briefly for connection attempt and backoff handling
    await asyncio.sleep(0.15)
    assert client.stats.connection_attempts >= 1
    assert client.stats.errors >= 1

    await client.stop()
    assert not client.is_running
    assert not client.is_connected
    assert task.done()
