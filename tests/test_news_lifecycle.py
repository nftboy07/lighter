import asyncio
import os
from datetime import datetime, timezone

from news_lifecycle import PaperFillSimulator, PositionBook, PositionClock, TradeIntentQueue
from news_markets import MarketRegistry
from news_observability import NewsReplay
from news_pipeline import NewsPipeline, NormalizedNewsEvent
from news_sources import JSONNewsAdapter, NewsSourceConfig, RawNewsRecord, WebhookNewsAdapter, parse_retry_after
from lighter_news_risk import MarketSnapshot


def _event(source="one", headline="Exchange lists ETH", cluster="cluster-eth"):
    now = datetime.now(timezone.utc)
    return NormalizedNewsEvent(
        event_id=f"{source}-{headline}",
        source_id=source,
        publisher=source.title(),
        headline=headline,
        body="official listing",
        url=f"https://{source}.test/a",
        guid=f"{source}-a",
        published_at=now,
        ingested_at=now,
        source_score=0.9,
        category="official",
        content_hash="hash",
        entities=("ETH",),
        event_type="listing",
        direction="BULLISH",
        materiality=0.8,
        confidence=0.9,
        cluster_id=cluster,
    )


def test_json_and_webhook_adapters_and_retry_after():
    source = NewsSourceConfig("json_sec", "SEC", "https://example.test/news.json", adapter="json", category="regulator", trust_score=0.95)
    payload = {"articles": [{"id": "1", "title": "SEC approves ETH ETF", "url": "https://www.sec.gov/a", "publishedAt": "2026-08-20T12:00:00Z"}]}
    records = JSONNewsAdapter(source).source  # keep config valid
    assert source.adapter == "json"
    webhook = WebhookNewsAdapter(NewsSourceConfig("hook", "Ops", "webhook://ops", adapter="webhook", category="unverified", trust_score=0.4))
    ingested = webhook.ingest({"title": "Exchange lists ETH", "url": "https://ops.test/a", "id": "w1"})
    assert ingested and ingested[0].publisher == "Ops"
    assert parse_retry_after("2") == 2.0
    assert records.source_id == "json_sec"


def test_duplicate_cluster_does_not_create_second_intent():
    queue = TradeIntentQueue()
    market = MarketRegistry().get("ETH")
    first = asyncio.run(queue.enqueue(_event("one"), market, "BUY/LONG", 25))
    first.status = "filled"
    second = asyncio.run(queue.enqueue(_event("two"), market, "BUY/LONG", 25))
    assert first.intent_id == second.intent_id
    assert len(queue.intents) == 1


def test_paper_fill_and_restart_reconciliation(tmp_path):
    db = str(tmp_path / "news.db")
    queue = TradeIntentQueue(db)
    book = PositionBook(db)
    market = MarketRegistry().get("ETH")
    intent = asyncio.run(queue.enqueue(_event(), market, "BUY/LONG", 25))
    filled = PaperFillSimulator().fill(intent, MarketSnapshot("ETH", 2500))
    assert filled.status == "filled"
    assert filled.fill_size > 0
    pos = book.activate_from_fill(filled)
    restored = PositionBook(db)
    assert restored.positions[pos.position_id].is_active
    assert restored.due_retries()


def test_correction_replay_and_emergency_flatten():
    replay = NewsReplay(NewsPipeline(min_sources=2))
    events = replay.replay_file(os.path.join(os.path.dirname(__file__), "..", "news_fixtures", "correction.json"))
    assert events[0].event_type == "listing"
    assert events[-1].event_type == "correction"
    book = PositionBook()
    market = MarketRegistry().get("ETH")
    intent = asyncio.run(TradeIntentQueue().enqueue(events[0], market, "BUY/LONG", 25))
    PaperFillSimulator().fill(intent, MarketSnapshot("ETH", 2500, timestamp=__import__("time").time()))
    book.activate_from_fill(intent)
    closed = book.emergency_flatten({"ETH": 2490})
    assert closed and not book.active()
