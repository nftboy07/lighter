from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from news_pipeline import NewsPipeline, NormalizedNewsEvent
from news_sources import RawNewsRecord, NewsSourceConfig, WebhookNewsAdapter, parse_iso_time
from datetime import datetime, timezone


class NewsMetrics:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()

    def inc(self, name: str, amount: int = 1) -> None:
        self.counters[name] += amount

    def snapshot(self) -> Dict[str, int]:
        return dict(self.counters)


class AuditLog:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path) if path else None
        self.records: List[Dict[str, Any]] = []

    def emit(self, kind: str, correlation_id: str, **fields: Any) -> Dict[str, Any]:
        record = {
            "ts": time.time(),
            "kind": kind,
            "correlation_id": correlation_id,
            **fields,
        }
        self.records.append(record)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        return record


class NewsReplay:
    """Replay sanitized fixtures through the pipeline without submitting orders."""

    def __init__(self, pipeline: Optional[NewsPipeline] = None) -> None:
        self.pipeline = pipeline or NewsPipeline()

    def replay_records(self, records: Iterable[RawNewsRecord]) -> List[NormalizedNewsEvent]:
        return self.pipeline.process(list(records))

    def replay_file(self, path: str) -> List[NormalizedNewsEvent]:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        records = [record_from_fixture(item) for item in payload]
        return self.replay_records(records)


def record_from_fixture(item: Dict[str, Any]) -> RawNewsRecord:
    published = item.get("published_at")
    ingested = item.get("ingested_at")
    return RawNewsRecord(
        source_id=item.get("source_id", "fixture"),
        publisher=item.get("publisher", "Fixture"),
        title=item.get("title") or item.get("headline", ""),
        body=item.get("body", ""),
        url=item.get("url", ""),
        guid=item.get("guid") or item.get("url", ""),
        published_at=parse_iso_time(published),
        ingested_at=parse_iso_time(ingested) or datetime.now(timezone.utc),
        trust_score=float(item.get("trust_score", 0.9)),
        category=item.get("category", "media"),
        raw=item.get("raw") or {"fixture": True},
    )


def parse_fixture_feed(source: NewsSourceConfig, body: str) -> List[RawNewsRecord]:
    if source.adapter == "json":
        payload = json.loads(body)
        now = datetime.now(timezone.utc)
        items = payload.get("articles") or payload.get("items") or payload
        records = []
        for item in items:
            records.append(
                RawNewsRecord(
                    source_id=source.source_id,
                    publisher=source.publisher,
                    title=str(item.get("title", "")),
                    body=str(item.get("description", "")),
                    url=str(item.get("url", "")),
                    guid=str(item.get("id", "")),
                    published_at=parse_iso_time(item.get("publishedAt")),
                    ingested_at=now,
                    trust_score=source.trust_score,
                    category=source.category,
                    raw={"adapter": "json", "fixture": True},
                )
            )
        return records
    if source.adapter == "webhook":
        return WebhookNewsAdapter(source).ingest(json.loads(body))
    parsed = __import__("feedparser").parse(body)
    now = datetime.now(timezone.utc)
    from news_sources import parse_entry_time, canonical_url

    records = []
    for entry in parsed.entries:
        records.append(
            RawNewsRecord(
                source_id=source.source_id,
                publisher=source.publisher,
                title=str(entry.get("title", "")),
                body=str(entry.get("summary", "")),
                url=canonical_url(str(entry.get("link", ""))),
                guid=str(entry.get("id", "") or entry.get("link", "")),
                published_at=parse_entry_time(entry),
                ingested_at=now,
                trust_score=source.trust_score,
                category=source.category,
                raw={"adapter": source.adapter, "fixture": True},
            )
        )
    return records
