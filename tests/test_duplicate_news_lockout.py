#!/usr/bin/env python3
"""
Unit & Integration Tests for Institutional Duplicate News Prevention & First-News-Only Position Lockout
========================================================================================================
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from news_pipeline import (
    NewsDeduplicator,
    NewsNormalizer,
    NewsPipeline,
    NormalizedNewsEvent,
    fuzzy_similarity,
    jaccard_similarity,
)
from news_sources import RawNewsRecord
from lighter_news_risk import LighterNewsRiskGate, MarketSnapshot
from news_lifecycle import TradeIntentQueue, PositionBook
from lighter_news_sniper import (
    ActivePosition,
    CatalystClassifier,
    CatalystSignal,
    LighterNewsSniperBot,
    NewsIngestionManager,
    NewsItem,
)


def make_record(
    source_id: str = "treenews",
    title: str = "Trump announces US Bitcoin Strategic Reserve",
    body: str = "Donald Trump announced plans for a strategic bitcoin reserve.",
    url: str = "https://example.com/story1",
    score: float = 0.95,
) -> RawNewsRecord:
    now = datetime.now(timezone.utc)
    return RawNewsRecord(
        source_id=source_id,
        publisher=source_id.title(),
        title=title,
        body=body,
        url=url,
        guid=url,
        published_at=now,
        ingested_at=now,
        trust_score=score,
        category="official",
    )


def test_fuzzy_and_jaccard_similarity_metrics():
    """Verify SequenceMatcher and token Jaccard similarity metrics."""
    s1 = "trump announces us bitcoin strategic reserve"
    s2 = "trump announces us strategic bitcoin reserve"
    jacc = jaccard_similarity(s1, s2)
    fuzzy = fuzzy_similarity(s1, s2)
    assert jacc >= 0.70
    assert fuzzy >= 0.70

    # Low similarity unrelated texts
    s3 = "ethereum foundation releases new roadmap"
    assert jaccard_similarity(s1, s3) < 0.20
    assert fuzzy_similarity(s1, s3) < 0.40


def test_cross_source_duplicate_rejection():
    """Verify deduplicator checks similarity across all sources when configured."""
    dedupe = NewsDeduplicator(similarity_threshold=0.85, cross_source_threshold=0.70)
    normalizer = NewsNormalizer()

    # 1. TreeNews scoop
    tree_event = normalizer.normalize(
        make_record(
            source_id="treenews",
            title="Trump announces US Bitcoin Strategic Reserve",
            url="https://tree.news/1",
        )
    )
    assert tree_event is not None
    assert dedupe.accept(tree_event) is True

    # 2. Bloomberg report of the same story -> Cross-source duplicate rejected
    bloomberg_event = normalizer.normalize(
        make_record(
            source_id="bloomberg",
            title="Trump Announces US Strategic Bitcoin Reserve Plan",
            url="https://bloomberg.com/2",
        )
    )
    assert bloomberg_event is not None
    assert dedupe.accept(bloomberg_event) is False

    # 3. CoinDesk report of the same story -> Cross-source duplicate rejected
    coindesk_event = normalizer.normalize(
        make_record(
            source_id="coindesk",
            title="Donald Trump Plans US Bitcoin Strategic Reserve",
            url="https://coindesk.com/3",
        )
    )
    assert coindesk_event is not None
    assert dedupe.accept(coindesk_event) is False

    # 4. Twitter / X report of the same story -> Cross-source duplicate rejected
    x_event = normalizer.normalize(
        make_record(
            source_id="x_wires",
            title="Trump announces US Bitcoin reserve on Truth Social",
            url="https://x.com/4",
        )
    )
    assert x_event is not None
    assert dedupe.accept(x_event) is False

    # 5. Distinct news event on different topic -> Accepted
    eth_event = normalizer.normalize(
        make_record(
            source_id="bloomberg",
            title="SEC approves Ethereum staking ETF applications",
            url="https://bloomberg.com/5",
        )
    )
    assert eth_event is not None
    assert dedupe.accept(eth_event) is True


def test_catalyst_classifier_story_fingerprint_lockout_15min_window():
    """Verify CatalystClassifier story fingerprint cache with 15-minute lockout."""
    classifier = CatalystClassifier(max_news_age_sec=60.0, fingerprint_window_sec=900.0)

    # 1. First event -> Emits signal
    item1 = NewsItem(
        source="TreeNews",
        headline="Donald Trump praises Hyperliquid as revolutionary crypto technology",
        body="Trump posted on Truth Social mentioning Hyperliquid and decentralized trading.",
        timestamp=time.time(),
    )
    sig1 = classifier.process_news(item1)
    assert sig1 is not None
    assert sig1.target_asset == "HYPE"
    assert sig1.sentiment == "BULLISH"

    # 2. Duplicate/follow-up event from another source within 2 minutes -> Dropped
    item2 = NewsItem(
        source="Bloomberg",
        headline="Trump Praises Hyperliquid Decentralized Exchange in Live Interview",
        body="Donald Trump speaks about revolutionary crypto technology Hyperliquid.",
        timestamp=time.time() + 30.0,
    )
    sig2 = classifier.process_news(item2)
    assert sig2 is None

    # 3. 3rd follow-up from Twitter -> Dropped
    item3 = NewsItem(
        source="Twitter",
        headline="Donald Trump says Hyperliquid is the future of crypto trading",
        body="Breaking: Trump mentions Hyperliquid.",
        timestamp=time.time() + 60.0,
    )
    sig3 = classifier.process_news(item3)
    assert sig3 is None

    # 4. Different catalyst on different asset (SEC ETF on ETH) -> Emits signal
    item4 = NewsItem(
        source="TreeNews",
        headline="SEC officially approves spot Ethereum ETF trading to begin tomorrow",
        body="Regulatory filing confirms ETF approval.",
        timestamp=time.time() + 70.0,
    )
    sig4 = classifier.process_news(item4)
    assert sig4 is not None
    assert sig4.target_asset == "ETH"
    assert sig4.sentiment == "BULLISH"


@pytest.mark.asyncio
async def test_first_news_burst_five_events_only_first_takes_position(tmp_path):
    """
    When 5 duplicate headlines arrive for the same asset within 2 minutes from 5 different sources:
    ONLY the 1st news takes a position and 2..5 are dropped.
    """
    db_file = str(tmp_path / "burst_test.db")
    bot = LighterNewsSniperBot(is_live=False)
    bot.db_path = db_file
    bot.intent_queue = TradeIntentQueue(db_file)
    bot.positions = PositionBook(db_file)
    bot.news_manager = NewsIngestionManager(bot._handle_news_event, db_path=db_file)
    bot.momentum_filter = None
    bot.news_risk_gate = LighterNewsRiskGate(live=False)
    bot.news_risk_gate.momentum_filter = None
    bot.news_risk_gate._session_trades = 0
    bot.news_manager.pipeline.confirmed = MagicMock(return_value=True)

    # Mock market snapshot and executor trade execution
    bot.executor.fetch_market_snapshot = AsyncMock(
        return_value=MarketSnapshot("ETH", 2500.0, spread_bps=1.0, timestamp=time.time(), market_index=0)
    )
    bot.executor.execute_trade = AsyncMock(
        return_value={
            "success": True,
            "mode": "PAPER",
            "asset": "ETH",
            "entry_price": 2500.0,
            "size_eth": 0.05,
            "notional_usd": 125.0,
            "order_id": "test_order_1",
        }
    )

    t_now = int(time.time() * 1000)
    headlines = [
        ("TreeNews", "Binance will list ETH perpetual contracts with 50x leverage", f"https://treenews.test/1_{t_now}"),
        ("Bloomberg", "Binance to List ETH Perpetual Contracts Today", f"https://bloomberg.test/2_{t_now}"),
        ("CoinDesk", "Binance Lists ETH Perpetual Contracts", f"https://coindesk.test/3_{t_now}"),
        ("Reuters", "Binance Announces ETH Perpetual Listing", f"https://reuters.test/4_{t_now}"),
        ("Twitter", "Binance enables ETH perpetual contracts trading", f"https://twitter.test/5_{t_now}"),
    ]

    normalizer = NewsNormalizer()

    for i, (src, hline, url) in enumerate(headlines):
        record = RawNewsRecord(
            source_id=src.lower(),
            publisher=src,
            title=hline,
            body="Official exchange listing announcement for ETH.",
            url=url,
            guid=url,
            published_at=datetime.now(timezone.utc),
            ingested_at=datetime.now(timezone.utc),
            trust_score=0.95,
            category="exchange",
        )
        norm_event = normalizer.normalize(record)
        assert norm_event is not None
        # Tag event direction and confidence for test
        from dataclasses import replace
        tagged = replace(
            norm_event,
            event_type="listing",
            direction="BULLISH",
            materiality=0.80,
            confidence=0.90,
            entities=("ETH",),
            cluster_id="cluster_eth_listing",
        )

        item = NewsItem(source=src, headline=hline, body=record.body, timestamp=time.time() + i * 10)
        await bot._handle_news_event(item, tagged)

        # After the 1st trade, simulate active position in executor
        if i == 0 and not bot.executor.active_positions:
            bot.executor.active_positions["pos_eth"] = ActivePosition(
                position_id="pos_eth",
                asset="ETH",
                market_index=0,
                side="BUY/LONG",
                entry_price=2500.0,
                size_eth=0.05,
                notional_usd=125.0,
                is_active=True,
            )

    # Verify that execute_trade was called EXACTLY ONCE (only for 1st news event)
    assert bot.executor.execute_trade.call_count == 1
    # Verify metrics recorded active position lockout or duplicate catalyst lockout for events 2..5
    lockout_count = bot.metrics.counters.get("active_position_lockout", 0) + bot.metrics.counters.get("duplicate_catalyst_lockout", 0)
    assert lockout_count >= 4


@pytest.mark.asyncio
async def test_active_position_blocks_duplicate_entry():
    """Verify that an active position in executor immediately drops duplicate incoming news."""
    bot = LighterNewsSniperBot(is_live=False)
    bot.news_manager.pipeline.confirmed = MagicMock(return_value=True)

    # Set up an active ETH position
    bot.executor.active_positions["existing_eth_pos"] = ActivePosition(
        position_id="existing_eth_pos",
        asset="ETH",
        market_index=0,
        side="BUY/LONG",
        entry_price=2600.0,
        size_eth=0.1,
        notional_usd=260.0,
        is_active=True,
    )
    bot.executor.execute_trade = AsyncMock()

    record = RawNewsRecord(
        source_id="treenews",
        publisher="TreeNews",
        title="SEC officially approves spot Ethereum ETF trading",
        body="SEC has approved Ethereum ETF.",
        url="https://treenews.test/sec_eth",
        guid="sec_eth",
        published_at=datetime.now(timezone.utc),
        ingested_at=datetime.now(timezone.utc),
        trust_score=0.98,
        category="official",
    )
    normalizer = NewsNormalizer()
    norm_event = normalizer.normalize(record)
    from dataclasses import replace
    tagged = replace(
        norm_event,
        event_type="approval",
        direction="BULLISH",
        materiality=0.85,
        confidence=0.95,
        entities=("ETH",),
        cluster_id="cluster_eth_approval",
    )

    item = NewsItem(source="TreeNews", headline=record.title, body=record.body, timestamp=time.time())
    await bot._handle_news_event(item, tagged)

    # Ensure execute_trade was never called due to active position lockout
    assert bot.executor.execute_trade.call_count == 0
    assert bot.metrics.counters.get("active_position_lockout", 0) == 1


@pytest.mark.asyncio
async def test_risk_gate_strict_cooldown_and_open_position_veto():
    """Verify LighterNewsRiskGate strict 15-min cooldown and active open position veto."""
    gate = LighterNewsRiskGate(live=False)
    gate._session_trades = 0
    assert gate.cooldown_seconds == 900.0

    now = datetime.now(timezone.utc)
    event = NormalizedNewsEvent(
        event_id="res_test_1",
        source_id="treenews",
        publisher="TreeNews",
        headline="Binance lists ETH",
        body="Listing announced",
        url="https://example.com/res1",
        guid="res1",
        published_at=now,
        ingested_at=now,
        source_score=0.95,
        category="official",
        content_hash="h1",
        entities=("ETH",),
        event_type="listing",
        direction="BULLISH",
        confidence=0.90,
        materiality=0.80,
    )
    snapshot = MarketSnapshot("ETH", 2500.0, timestamp=time.time())

    # 1. First approval -> Approved
    d1 = await gate.approve(event, snapshot, 25.0, confirmed=True, asset="ETH", side="BUY/LONG")
    assert d1.approved is True

    # 2. Record fill (triggers cooldown + open position)
    gate.record_fill("ETH")
    assert gate.has_open_position("ETH") is True

    # 3. Subsequent approval within cooldown -> Vetoed with exact expected message
    d2 = await gate.approve(event, snapshot, 25.0, confirmed=True, asset="ETH", side="BUY/LONG")
    assert d2.approved is False
    assert any("duplicate news signal: active position or cooldown in effect" in r for r in d2.reasons)

    # 4. Explicit active_positions parameter -> Vetoed
    active_pos_dict = {
        "pos_1": ActivePosition(
            position_id="pos_1", asset="ETH", market_index=0, side="BUY/LONG",
            entry_price=2500.0, size_eth=0.01, notional_usd=25.0, is_active=True
        )
    }
    # Reset trade timestamp to test active_positions check specifically
    gate._last_asset_trade["ETH"] = time.time() - 2000.0
    gate.clear_open_position("ETH")
    d3 = await gate.approve(
        event, snapshot, 25.0, confirmed=True, asset="ETH", side="BUY/LONG",
        active_positions=active_pos_dict,
    )
    assert d3.approved is False
    assert any("duplicate news signal: active position or cooldown in effect" in r for r in d3.reasons)
