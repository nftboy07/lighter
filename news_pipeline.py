from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from news_sources import RawNewsRecord, canonical_url, stable_hash
from news_universe import (
    ASSET_ALIASES,
    TICKER_HINT,
    _AMBIGUOUS,
    alias_is_english_collision,
    listed_symbols,
    known_symbols,
)


CLASSIFIER_VERSION = "news-classifier-v2"
OFFICIAL_DOMAINS = {
    "www.sec.gov": "SEC",
    "www.cftc.gov": "CFTC",
    "blog.ethereum.org": "ETH",
    "www.coinbase.com": "COINBASE",
    "blog.kraken.com": "KRAKEN",
    "www.binance.com": "BINANCE",
    "www.coindesk.com": "COINDESK",
    "www.theblock.co": "THEBLOCK",
}
CHAIN_ALIASES = {
    "ethereum": "ethereum",
    "eth": "ethereum",
    "base": "base",
    "solana": "solana",
    "bitcoin": "bitcoin",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
}
KNOWN_SYMBOLS = known_symbols()


@dataclass(frozen=True)
class NormalizedNewsEvent:
    event_id: str
    source_id: str
    publisher: str
    headline: str
    body: str
    url: str
    guid: str
    published_at: Optional[datetime]
    ingested_at: datetime
    source_score: float
    category: str
    content_hash: str
    entities: Tuple[str, ...] = ()
    event_type: str = "unknown"
    direction: str = "NEUTRAL"
    materiality: float = 0.0
    confidence: float = 0.0
    cluster_id: str = ""
    classifier_version: str = CLASSIFIER_VERSION
    contradiction: bool = False
    official_verified: bool = False
    chain: str = ""
    chain_ambiguous: bool = False
    invalidated: bool = False
    raw: dict = field(default_factory=dict)


class NewsNormalizer:
    def normalize(self, record: RawNewsRecord) -> Optional[NormalizedNewsEvent]:
        headline = clean_text(record.title, 500)
        body = clean_text(record.body, 10_000)
        if not headline:
            return None
        url = canonical_url(record.url)
        content_hash = stable_hash(headline.lower(), body.lower())
        event_id = stable_hash(record.source_id, record.guid or url or content_hash)
        text = f"{headline} {body}"
        entities = tuple(sorted(set(extract_entities(text))))
        chain, chain_ambiguous = extract_chain(text)
        official_verified = verify_official_domain(url, record.category, record.raw.get("official_domain", ""))
        ingested_at = record.ingested_at.astimezone(timezone.utc)
        source_latency = None
        if record.published_at is not None:
            if record.published_at.tzinfo is None:
                return None
            source_latency = max(0.0, (ingested_at - record.published_at.astimezone(timezone.utc)).total_seconds())
        raw = dict(record.raw)
        raw["source_latency_seconds"] = source_latency
        raw["transformation"] = {"normalizer": "v2", "classifier_version": CLASSIFIER_VERSION}
        return NormalizedNewsEvent(
            event_id=event_id,
            source_id=record.source_id,
            publisher=record.publisher,
            headline=headline,
            body=body,
            url=url,
            guid=record.guid,
            published_at=record.published_at,
            ingested_at=ingested_at,
            source_score=record.trust_score,
            category=record.category,
            content_hash=content_hash,
            entities=entities,
            chain=chain,
            chain_ambiguous=chain_ambiguous,
            official_verified=official_verified,
            raw=raw,
        )


def jaccard_similarity(s1: str, s2: str) -> float:
    tokens1 = set(s1.split())
    tokens2 = set(s2.split())
    if not tokens1 and not tokens2:
        return 1.0
    if not tokens1 or not tokens2:
        return 0.0
    return len(tokens1 & tokens2) / len(tokens1 | tokens2)


def fuzzy_similarity(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    sm_ratio = SequenceMatcher(None, s1, s2).ratio()
    jaccard_ratio = jaccard_similarity(s1, s2)
    return max(sm_ratio, jaccard_ratio)


class NewsDeduplicator:
    def __init__(self, similarity_threshold: float = 0.85, max_age_seconds: float = 7 * 86400, cross_source_threshold: Optional[float] = None) -> None:
        self.similarity_threshold = similarity_threshold
        self.cross_source_threshold = cross_source_threshold
        self.max_age_seconds = max_age_seconds
        self._events: Dict[str, Tuple[str, float]] = {}
        self._titles: List[Tuple[str, str, float]] = []

    def accept(self, event: NormalizedNewsEvent) -> bool:
        now = time.time()
        self._prune(now)
        keys = (
            event.event_id,
            f"{event.source_id}|{event.url}" if event.url else "",
            f"{event.source_id}|{event.content_hash}",
        )
        for key in keys:
            if key and key in self._events:
                return False
        normalized_title = normalize_title(event.headline)
        for prior_source_id, prior_title, _ in self._titles:
            if prior_source_id == event.source_id:
                if fuzzy_similarity(normalized_title, prior_title) >= self.similarity_threshold:
                    return False
            elif self.cross_source_threshold is not None:
                if fuzzy_similarity(normalized_title, prior_title) >= self.cross_source_threshold:
                    return False
        for key in keys:
            if key:
                self._events[key] = (event.event_id, now)
        self._titles.append((event.source_id, normalized_title, now))
        return True

    def _prune(self, now: float) -> None:
        cutoff = now - self.max_age_seconds
        self._events = {key: value for key, value in self._events.items() if value[1] >= cutoff}
        self._titles = [item for item in self._titles if item[2] >= cutoff]


class NewsConfirmationEngine:
    def __init__(self, min_sources: int = 2, max_age_seconds: float = 120.0) -> None:
        self.min_sources = max(1, min_sources)
        self.max_age_seconds = max_age_seconds
        self._clusters: Dict[str, List[NormalizedNewsEvent]] = {}

    def assess(self, event: NormalizedNewsEvent, event_type: str, direction: str, materiality: float) -> NormalizedNewsEvent:
        cluster_id = self._cluster_id(event, event_type, direction)
        members = self._clusters.setdefault(cluster_id, [])
        if all(item.source_id != event.source_id for item in members):
            members.append(event)
        contradiction = self._contradiction(event, event_type, direction)
        confidence = min(0.99, event.source_score * 0.6 + min(0.4, len(members) * 0.2))
        if contradiction:
            confidence = min(confidence, 0.35)
        if event.official_verified:
            confidence = min(0.99, confidence + 0.08)
        return replace(
            event,
            event_type=event_type,
            direction=direction,
            materiality=materiality,
            confidence=confidence,
            cluster_id=cluster_id,
            contradiction=contradiction,
            classifier_version=CLASSIFIER_VERSION,
        )

    def is_confirmed(self, event: NormalizedNewsEvent) -> bool:
        from news_quality import quality_veto, require_two_sources

        if event.contradiction or event.invalidated or event.chain_ambiguous:
            return False
        ok, _reason = quality_veto(event)
        if not ok:
            return False
        members = self._clusters.get(event.cluster_id, [])
        independent_sources = {member.source_id for member in members}
        if not require_two_sources(event, len(independent_sources), self.min_sources):
            return False
        return event.confidence >= 0.70 and event.materiality >= 0.50

    def invalidate_cluster(self, cluster_id: str) -> List[NormalizedNewsEvent]:
        members = self._clusters.get(cluster_id, [])
        updated = [replace(item, invalidated=True, direction="NEUTRAL", materiality=0.0, confidence=0.0) for item in members]
        self._clusters[cluster_id] = updated
        return updated

    def _contradiction(self, event: NormalizedNewsEvent, event_type: str, direction: str) -> bool:
        entity = event.entities[0] if event.entities else normalize_title(event.headline)[:80]
        for cluster_id, members in self._clusters.items():
            if not members:
                continue
            if entity not in {*(members[0].entities), cluster_id}:
                related = entity == (members[0].entities[0] if members[0].entities else "")
            else:
                related = True
            if not related and entity != (members[0].entities[0] if members[0].entities else normalize_title(members[0].headline)[:80]):
                continue
            existing_dirs = {item.direction for item in members if item.direction in {"BULLISH", "BEARISH"}}
            if direction in {"BULLISH", "BEARISH"} and existing_dirs and direction not in existing_dirs:
                return True
            if event_type == "correction":
                return True
        return False

    def _cluster_id(self, event: NormalizedNewsEvent, event_type: str, direction: str) -> str:
        from news_direction import theme_cluster_id

        if event_type == "correction":
            entity = event.entities[0] if event.entities else normalize_title(event.headline)[:80]
            return hashlib.sha256(f"{entity}|correction".encode()).hexdigest()[:24]
        tagged = replace(event, event_type=event_type, direction=direction)
        return theme_cluster_id(tagged)


class NewsPipeline:
    def __init__(
        self,
        db_path: Optional[str] = None,
        min_sources: int = 2,
        on_correction: Optional[Callable[[NormalizedNewsEvent], None]] = None,
    ) -> None:
        self.normalizer = NewsNormalizer()
        self.deduplicator = NewsDeduplicator()
        self.confirmation = NewsConfirmationEngine(min_sources=min_sources)
        self.db_path = db_path
        self.on_correction = on_correction
        self.recent_events: List[NormalizedNewsEvent] = []
        self.rejected_events: List[NormalizedNewsEvent] = []
        if db_path:
            self._init_db()

    def process(self, records: Sequence[RawNewsRecord]) -> List[NormalizedNewsEvent]:
        output: List[NormalizedNewsEvent] = []
        for record in records:
            event = self.normalizer.normalize(record)
            if not event:
                continue
            if not self.deduplicator.accept(event):
                self.rejected_events.append(replace(event, event_type="duplicate"))
                continue
            from news_direction import classify_with_body

            event_type, direction, materiality = classify_with_body(event.headline, event.body)
            assessed = self.confirmation.assess(event, event_type, direction, materiality)
            if assessed.event_type == "correction":
                related = self._related_cluster(assessed)
                if related:
                    self.confirmation.invalidate_cluster(related)
                    assessed = replace(assessed, cluster_id=related, invalidated=True)
                if self.on_correction:
                    self.on_correction(assessed)
            if self.db_path:
                self.persist(assessed)
            self.recent_events.append(assessed)
            self.recent_events = self.recent_events[-200:]
            output.append(assessed)
        return output

    def confirmed(self, event: NormalizedNewsEvent) -> bool:
        return self.confirmation.is_confirmed(event)

    def _related_cluster(self, event: NormalizedNewsEvent) -> str:
        entity = event.entities[0] if event.entities else normalize_title(event.headline)[:80]
        for cluster_id, members in self.confirmation._clusters.items():
            if not members:
                continue
            member_entity = members[0].entities[0] if members[0].entities else normalize_title(members[0].headline)[:80]
            if member_entity == entity and members[0].event_type != "correction":
                return cluster_id
        return event.cluster_id

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS news_events (
                    event_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, publisher TEXT NOT NULL,
                    headline TEXT NOT NULL, body TEXT NOT NULL, url TEXT, published_at TEXT,
                    ingested_at TEXT NOT NULL, source_score REAL NOT NULL, category TEXT NOT NULL,
                    content_hash TEXT NOT NULL, entities TEXT NOT NULL, event_type TEXT NOT NULL,
                    direction TEXT NOT NULL, materiality REAL NOT NULL, confidence REAL NOT NULL,
                    cluster_id TEXT NOT NULL, raw_json TEXT NOT NULL, classifier_version TEXT,
                    contradiction INTEGER, official_verified INTEGER, chain TEXT
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_news_events_cluster ON news_events(cluster_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_news_events_ingested ON news_events(ingested_at)")
            existing = {row[1] for row in conn.execute("PRAGMA table_info(news_events)").fetchall()}
            migrations = {
                "classifier_version": "TEXT",
                "contradiction": "INTEGER",
                "official_verified": "INTEGER",
                "chain": "TEXT",
            }
            for column, spec in migrations.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE news_events ADD COLUMN {column} {spec}")

    def persist(self, event: NormalizedNewsEvent) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO news_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.source_id,
                    event.publisher,
                    event.headline,
                    event.body,
                    event.url,
                    event.published_at.isoformat() if event.published_at else "",
                    event.ingested_at.isoformat(),
                    event.source_score,
                    event.category,
                    event.content_hash,
                    json.dumps(event.entities),
                    event.event_type,
                    event.direction,
                    event.materiality,
                    event.confidence,
                    event.cluster_id,
                    json.dumps(event.raw),
                    event.classifier_version,
                    int(event.contradiction),
                    int(event.official_verified),
                    event.chain,
                ),
            )


def clean_text(value: str, limit: int) -> str:
    value = html.unescape(unicodedata.normalize("NFKC", value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[\x00-\x1f]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", value.lower()).strip()


def extract_entities(text: str) -> Iterable[str]:
    found: List[str] = []
    lower = text.lower()
    for alias, symbol in ASSET_ALIASES.items():
        if len(alias) < 3:
            continue
        if alias_is_english_collision(alias, symbol):
            continue
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            found.append(symbol)
    found.extend(re.findall(r"\$([A-Z]{2,10})\b", text.upper()))
    listed = listed_symbols() | known_symbols()
    for word in re.findall(r"\b[A-Z]{3,12}\b", text):
        if word not in listed:
            continue
        if word in _AMBIGUOUS and not TICKER_HINT.search(text):
            continue
        found.append(word)
    addresses = re.findall(r"0x[a-fA-F0-9]{40}", text)
    return list(dict.fromkeys(found + [address.lower() for address in addresses]))


def extract_chain(text: str) -> Tuple[str, bool]:
    lower = text.lower()
    hits = []
    for alias, chain in CHAIN_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lower) and chain not in hits:
            hits.append(chain)
    if len(hits) > 1:
        return "", True
    return (hits[0] if hits else ""), False


def verify_official_domain(url: str, category: str, configured_domain: str = "") -> bool:
    host = urlsplit(url).netloc.lower()
    if not host:
        return False
    if configured_domain and host == configured_domain.lower():
        return True
    if host in OFFICIAL_DOMAINS and category in {"official", "regulator", "exchange"}:
        return True
    return False


def confirmation_threshold(event_type: str, default: int) -> int:
    if event_type in {"exploit", "listing", "delisting", "approval", "regulatory"}:
        return max(default, 2)
    if event_type in {"macro", "partnership"}:
        return max(default, 2)
    return default


RULE_REGISTRY: Tuple[Tuple[str, str, str, float], ...] = (
    (r"\b(retract(ed|ion)?|correction|false report|not approved|not hacked|no approval|did not hack)\b", "correction", "NEUTRAL", 0.0),
    (r"\b(satire|parody|joke|the onion)\b", "satire", "NEUTRAL", 0.0),
    (r"\b(opinion|op-ed|i think|editorial|alongside bitcoin|crypto stocks)\b", "opinion", "NEUTRAL", 0.10),
    (r"\b(rumou?r|unconfirmed|sources say|reportedly|speculation)\b", "rumor", "NEUTRAL", 0.25),
    (r"\b(hack|exploit|drain|breach|stolen|vulnerability)\b", "exploit", "BEARISH", 0.85),
    (r"\b(outage|downtime|halted|maintenance incident)\b", "outage", "BEARISH", 0.65),
    (r"\b(delist(ed|ing)?)\b", "delisting", "BEARISH", 0.80),
    (r"\b(reject(ed|ion)|denied)\b", "rejection", "BEARISH", 0.70),
    (r"\b(unlock|token unlock|cliff)\b", "unlock", "BEARISH", 0.60),
    (r"\b(liquidation|liquidated|long liquidation)\b", "liquidation", "BEARISH", 0.70),
    (r"\b(short squeeze|short liquidation)\b", "surge", "BULLISH", 0.80),
    (r"\b(opec|production cut|supply cut)\b", "opec", "BULLISH", 0.75),
    (r"\b(brent|wti|crude).{0,48}(plunge|slump|crash|collapse|tumble)\b", "opec", "BEARISH", 0.75),
    (r"\b(brent|wti|crude|oil price).{0,48}(jump|surge|soar|rally|tops|climbs|hits)\b", "opec", "BULLISH", 0.72),
    (r"\b(gold).{0,32}(plunge|slump|tumble)\b", "macro", "BEARISH", 0.68),
    (r"\b(gold).{0,32}(jump|surge|rally|soar|hits)\b", "macro", "BULLISH", 0.68),
    (r"\b(copper).{0,40}(collapse|slump|plunge)\b", "macro", "BEARISH", 0.68),
    (r"\b(yen|euro|sterling|pound).{0,32}(plunge|slump|tumble)\b", "macro", "BEARISH", 0.65),
    (r"\b(yen|euro|sterling|pound).{0,32}(surge|jump|rally|soar)\b", "macro", "BULLISH", 0.65),
    (r"\b(will list|lists|listed|listing|added to robinhood|added to binance|added to coinbase)\b", "listing", "BULLISH", 0.75),
    (r"\b(approv(ed|es|al)|etf approved|wins lawsuit|lawsuit dismissed|sec drops)\b", "approval", "BULLISH", 0.80),
    (r"\b(spot etf|etf filing|etf inflows?|etf launch)\b", "etf", "BULLISH", 0.80),
    (r"\b(partnership|integrat(es|ion)|mainnet launch|mainnet|upgrade|hard fork|v2|v3)\b", "upgrade", "BULLISH", 0.70),
    (r"\b(token burn|burned tokens?|buyback|accumulat(es|ing)|acquires?)\b", "partnership", "BULLISH", 0.70),
    (r"\b(surges?|soars?|rall(y|ies|ied)|jumps?|breaks? out|breakout|record high|all-time high|ath|pumps?)\b", "surge", "BULLISH", 0.75),
    (r"\b(plunges?|crashes?|collapses?|tumbles?|slumps?|dumps?|dives?|selloff)\b", "breakdown", "BEARISH", 0.75),
    (r"\b(governance|proposal passed|vote passed)\b", "governance", "BULLISH", 0.45),
    (r"\b(funding|raises|series [abc]|treasury)\b", "funding", "BULLISH", 0.50),
    (r"\b(beats|beat estimates|earnings beat|record (revenue|profit))\b", "earnings", "BULLISH", 0.75),
    (r"\b(misses|earnings miss|cuts guidance|profit warning)\b", "earnings", "BEARISH", 0.80),
    (r"\b(earnings|quarterly results|q[1-4] results)\b", "earnings", "NEUTRAL", 0.55),
    (r"\b(sanction|sanctions|tariff|embargo)\b", "sanction", "BEARISH", 0.70),
    (r"\b(bankrupt|insolvency|defaulted|chapter 11)\b", "distress", "BEARISH", 0.85),
    (r"\b(hawkish|rate hike|hikes rates)\b", "macro", "BEARISH", 0.70),
    (r"\b(dovish|rate cut|cuts rates|qe )\b", "macro", "BULLISH", 0.70),
    (r"\b(sec|cftc|doj|federal reserve|ecb|bank of england|boj|central bank)\b", "regulatory", "BEARISH", 0.55),
    (r"\b(cpi|fomc|nfp|nonfarm|interest rate|inflation|payrolls|pce)\b", "macro", "NEUTRAL", 0.55),
)


def classify_event(headline: str, body: str) -> Tuple[str, str, float]:
    text = f"{headline} {body}".lower()
    for pattern, event_type, direction, materiality in RULE_REGISTRY:
        if re.search(pattern, text):
            if event_type == "regulatory" and re.search(r"\b(approv(ed|es|al)|settlement)\b", text):
                return "approval", "BULLISH", 0.80
            return event_type, direction, materiality
    return "unknown", "NEUTRAL", 0.10
