#!/usr/bin/env python3
"""
Unit and Integration Tests for Binance/Bybit Cross-Exchange Momentum Lead Filter
================================================================================
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cross_exchange_momentum import (
    CrossExchangeMomentumFilter,
    MomentumConfirmation,
    TradeTick,
)
from lighter_news_risk import LighterNewsRiskGate, MarketSnapshot
from news_pipeline import NormalizedNewsEvent


def create_mock_event(confidence=0.92, direction="BULLISH", headline="Binance lists ETH"):
    now = datetime.now(timezone.utc)
    return NormalizedNewsEvent(
        event_id=f"test_evt_{int(time.time()*1000)}",
        source_id="tree_news",
        publisher="TreeNews",
        headline=headline,
        body="Breaking listing announcement",
        url="https://treeofalpha.com/news/123",
        guid="guid_123",
        published_at=now,
        ingested_at=now,
        source_score=0.85,
        category="media",
        content_hash="mock_hash",
        entities=("ETH",),
        event_type="listing",
        direction=direction,
        confidence=confidence,
        materiality=0.85,
    )


def test_momentum_filter_evaluation_bullish_spike_confirmed():
    """Test volume spike confirmation with buy flow alignment in 100ms window."""
    filter_engine = CrossExchangeMomentumFilter(min_spike_ratio=1.5, window_ms=100)
    filter_engine.set_baseline_volume("ETH", volume_usd_per_sec=1000.0)

    now = time.time()
    # Baseline for 100ms (0.1s) is $100.
    # Inject $300 volume in Binance + Bybit within last 50ms (3.0x spike) with buy taker orders
    filter_engine.record_tick("ETH", "binance", price=2500.0, size=0.08, is_buyer_maker=False, timestamp=now - 0.05)  # $200 buy
    filter_engine.record_tick("ETH", "bybit", price=2500.0, size=0.04, is_buyer_maker=False, timestamp=now - 0.03)    # $100 buy

    res = filter_engine.evaluate_buffer("ETH", "BULLISH", window_ms=100, now=now)
    assert res.confirmed is True
    assert res.spike_ratio >= 2.5
    assert res.direction_aligned is True
    assert res.buy_ratio == 1.0
    assert res.total_vol_usd == 300.0
    assert res.latency_ms < 10.0  # Ultra-fast evaluation


def test_momentum_filter_evaluation_flow_contradiction():
    """Test that selling order flow fails a bullish catalyst confirmation."""
    filter_engine = CrossExchangeMomentumFilter(min_spike_ratio=1.5, window_ms=100)
    filter_engine.set_baseline_volume("ETH", volume_usd_per_sec=1000.0)

    now = time.time()
    # Inject sell taker orders (is_buyer_maker=True)
    filter_engine.record_tick("ETH", "binance", price=2500.0, size=0.20, is_buyer_maker=True, timestamp=now - 0.05)  # $500 sell

    res = filter_engine.evaluate_buffer("ETH", "BULLISH", window_ms=100, now=now)
    assert res.confirmed is False
    assert res.direction_aligned is False
    assert res.buy_ratio == 0.0
    assert any("contradiction" in r for r in res.reasons)


def test_momentum_filter_evaluation_low_volume_unconfirmed():
    """Test that lack of volume spike fails confirmation."""
    filter_engine = CrossExchangeMomentumFilter(min_spike_ratio=2.0, window_ms=100)
    filter_engine.set_baseline_volume("ETH", volume_usd_per_sec=5000.0)  # Expecting $500/100ms

    now = time.time()
    # Only $50 volume
    filter_engine.record_tick("ETH", "binance", price=2500.0, size=0.02, is_buyer_maker=False, timestamp=now - 0.05)

    res = filter_engine.evaluate_buffer("ETH", "BULLISH", window_ms=100, now=now)
    assert res.confirmed is False
    assert res.spike_ratio < 2.0


def test_momentum_size_multiplier():
    """Test sizing scaling based on cross-exchange momentum."""
    filter_engine = CrossExchangeMomentumFilter(min_spike_ratio=1.5, require_confirmation=True)

    # 1. Confirmed -> 1.0x (Max Size)
    confirmed_res = MomentumConfirmation(
        confirmed=True, spike_ratio=2.5, binance_vol_usd=1000, bybit_vol_usd=500,
        total_vol_usd=1500, buy_ratio=0.8, direction_aligned=True, latency_ms=1.0,
        asset="ETH", sentiment="BULLISH"
    )
    assert filter_engine.size_multiplier(confirmed_res, conviction_score=0.95) == 1.0

    # 2. Contradiction -> 0.0x (No trade)
    contradicted_res = MomentumConfirmation(
        confirmed=False, spike_ratio=2.5, binance_vol_usd=1000, bybit_vol_usd=500,
        total_vol_usd=1500, buy_ratio=0.1, direction_aligned=False, latency_ms=1.0,
        asset="ETH", sentiment="BULLISH"
    )
    assert filter_engine.size_multiplier(contradicted_res, conviction_score=0.95) == 0.0


@pytest.mark.asyncio
async def test_risk_gate_integration_with_momentum_filter():
    """Test LighterNewsRiskGate approves confirmed and blocks unconfirmed high-conviction events."""
    risk_gate = LighterNewsRiskGate(live=False)
    momentum_filter = CrossExchangeMomentumFilter(min_spike_ratio=1.5, require_confirmation=True)
    risk_gate.momentum_filter = momentum_filter

    snapshot = MarketSnapshot(asset="ETH", price=2500.0)
    event = create_mock_event(confidence=0.95, direction="BULLISH")

    # 1. Explicit momentum confirmed -> Approved
    decision_ok = await risk_gate.approve(
        event=event,
        snapshot=snapshot,
        requested_usd=100.0,
        confirmed=True,
        authorized=True,
        asset="ETH",
        side="BUY/LONG",
        momentum_confirmed=True,
    )
    assert decision_ok.approved is True
    assert decision_ok.sized_usd == 100.0

    # 2. Explicit momentum failed -> Vetoed
    decision_veto = await risk_gate.approve(
        event=event,
        snapshot=snapshot,
        requested_usd=100.0,
        confirmed=True,
        authorized=True,
        asset="ETH",
        side="BUY/LONG",
        momentum_confirmed=False,
    )
    assert decision_veto.approved is False
    assert any("momentum" in r for r in decision_veto.reasons)
