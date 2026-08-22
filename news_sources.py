from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Protocol, Sequence
from urllib.parse import quote, urlsplit, urlunsplit
import ssl

import aiohttp
import certifi
import feedparser

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SEC_UA = "LighterNewsTerminal/2.0 (research; contact=operator)"


def browser_headers(url: str = "") -> Dict[str, str]:
    host = urlsplit(url).netloc.lower() if url else ""
    ua = SEC_UA if "sec.gov" in host else BROWSER_UA
    return {
        "User-Agent": ua,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }


VALID_CATEGORIES = {
    "official",
    "regulator",
    "exchange",
    "media",
    "research",
    "social",
    "unverified",
}
LIVE_SAFE_CATEGORIES = {"official", "regulator", "exchange", "media", "research"}


@dataclass(frozen=True)
class NewsSourceConfig:
    source_id: str
    publisher: str
    url: str
    adapter: str = "rss"
    interval_seconds: float = 10.0
    timeout_seconds: float = 8.0
    max_entries: int = 20
    trust_score: float = 0.5
    category: str = "media"
    enabled: bool = True
    language: str = "en"
    region: str = "global"
    quota_per_minute: int = 12
    official_domain: str = ""
    allow_live: bool = False
    items_path: str = ""
    fallback_urls: tuple = ()

    def __post_init__(self) -> None:
        if not self.source_id or not self.publisher or not self.url:
            raise ValueError("source_id, publisher, and url are required")
        if not 0.0 <= self.trust_score <= 1.0:
            raise ValueError("trust_score must be between 0 and 1")
        if self.interval_seconds <= 0 or self.timeout_seconds <= 0:
            raise ValueError("source intervals and timeouts must be positive")
        if self.max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"unknown source category: {self.category}")
        if self.adapter not in {"rss", "atom", "json", "webhook", "official", "x"}:
            raise ValueError(f"unknown adapter: {self.adapter}")
        object.__setattr__(self, "official_domain", self.official_domain or urlsplit(self.url).netloc.lower())
        live_ok = self.allow_live or (self.category in LIVE_SAFE_CATEGORIES and self.adapter != "webhook")
        object.__setattr__(self, "allow_live", bool(live_ok and self.category != "social" and self.category != "unverified"))


@dataclass
class SourceHealth:
    source_id: str
    status: str = "unknown"
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    last_article_at: float = 0.0
    last_latency_ms: float = 0.0
    last_error: str = ""
    circuit_open_until: float = 0.0
    etag: str = ""
    last_modified: str = ""
    retry_after_until: float = 0.0

    @property
    def circuit_open(self) -> bool:
        now = time.time()
        return self.circuit_open_until > now or self.retry_after_until > now


@dataclass
class RawNewsRecord:
    source_id: str
    publisher: str
    title: str
    body: str
    url: str
    guid: str
    published_at: Optional[datetime]
    ingested_at: datetime
    trust_score: float
    category: str
    raw: Dict[str, Any] = field(default_factory=dict)


class NewsAdapter(Protocol):
    source: NewsSourceConfig

    async def fetch(self, session: aiohttp.ClientSession, health: Optional[SourceHealth] = None) -> List[RawNewsRecord]:
        ...


class DomainRateLimiter:
    def __init__(self, min_interval_seconds: float = 0.1) -> None:
        self.min_interval_seconds = min_interval_seconds
        self._last: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, url: str) -> None:
        domain = urlsplit(url).netloc.lower()
        async with self._lock:
            wait = self.min_interval_seconds - (time.time() - self._last.get(domain, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[domain] = time.time()


class SourceHealthStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path
        if db_path:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS source_health (
                        source_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )"""
                )

    def load(self, source_id: str) -> Optional[SourceHealth]:
        if not self.db_path:
            return None
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT payload FROM source_health WHERE source_id = ?", (source_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        return SourceHealth(**data)

    def save(self, health: SourceHealth) -> None:
        if not self.db_path:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO source_health VALUES (?, ?, ?)",
                (health.source_id, json.dumps(asdict(health)), time.time()),
            )


class NewsSourceRegistry:
    def __init__(self, sources: Optional[Iterable[NewsSourceConfig]] = None) -> None:
        self._sources: Dict[str, NewsSourceConfig] = {}
        for source in sources or default_sources():
            self.register(source)
        self._apply_runtime_filters()

    def register(self, source: NewsSourceConfig) -> None:
        self._sources[source.source_id] = source

    def get(self, source_id: str) -> NewsSourceConfig:
        return self._sources[source_id]

    def enabled(self) -> List[NewsSourceConfig]:
        return [source for source in self._sources.values() if source.enabled]

    def health(self) -> Dict[str, SourceHealth]:
        return {source.source_id: SourceHealth(source.source_id) for source in self._sources.values()}

    def as_dict(self) -> Dict[str, Dict[str, Any]]:
        return {source_id: vars(source).copy() for source_id, source in self._sources.items()}

    def reject_unknown_live_source(self, source_id: str) -> bool:
        source = self._sources.get(source_id)
        return source is None or not source.allow_live

    def _apply_runtime_filters(self) -> None:
        allow = _csv_set(os.getenv("NEWS_SOURCE_ALLOWLIST", ""))
        deny = _csv_set(os.getenv("NEWS_DISABLED_SOURCES", "") or os.getenv("NEWS_SOURCE_DENYLIST", ""))
        min_trust = float(os.getenv("NEWS_MIN_TRUST_SCORE", "0.0"))
        for source_id, source in list(self._sources.items()):
            enabled = source.enabled
            if allow and source_id not in allow:
                enabled = False
            if source_id in deny:
                enabled = False
            if source.trust_score < min_trust and source.category in {"social", "unverified"}:
                enabled = False
            if enabled != source.enabled:
                object.__setattr__(source, "enabled", enabled)
        for extra in load_sources_from_env():
            self.register(extra)


class RSSNewsAdapter:
    def __init__(self, source: NewsSourceConfig) -> None:
        self.source = source

    async def fetch(self, session: aiohttp.ClientSession, health: Optional[SourceHealth] = None) -> List[RawNewsRecord]:
        started = time.perf_counter()
        urls = (self.source.url,) + tuple(self.source.fallback_urls or ())
        last_err: Optional[Exception] = None
        body = ""
        for url in urls:
            headers = browser_headers(url)
            if health and health.etag and url == self.source.url and health.consecutive_failures == 0:
                headers["If-None-Match"] = health.etag
            if health and health.last_modified and url == self.source.url and health.consecutive_failures == 0:
                headers["If-Modified-Since"] = health.last_modified
            timeout = aiohttp.ClientTimeout(total=max(12.0, self.source.timeout_seconds))
            try:
                async with session.get(url, timeout=timeout, headers=headers, allow_redirects=True) as response:
                    if response.status == 304:
                        return []
                    if response.status == 429:
                        retry_after = parse_retry_after(response.headers.get("Retry-After"))
                        raise RateLimitedError(retry_after)
                    if response.status not in {200, 201}:
                        last_err = RuntimeError(f"HTTP {response.status}")
                        continue
                    body = await response.text()
                    if health is not None:
                        health.etag = response.headers.get("ETag", health.etag)
                        health.last_modified = response.headers.get("Last-Modified", health.last_modified)
                    break
            except RateLimitedError:
                raise
            except Exception as exc:
                last_err = exc
                continue
        else:
            raise last_err or RuntimeError("all feed URLs failed")
        if len(body.encode("utf-8", "ignore")) > 2_000_000:
            raise ValueError("feed response exceeds 2MB limit")
        parsed = await asyncio.to_thread(feedparser.parse, body)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise ValueError("malformed feed")
        now = datetime.now(timezone.utc)
        records: List[RawNewsRecord] = []
        for entry in parsed.entries[: self.source.max_entries]:
            title = str(entry.get("title", "")).strip()
            if not title:
                continue
            published_at = parse_entry_time(entry)
            url = canonical_url(str(entry.get("link", "")))
            guid = str(entry.get("id", "") or entry.get("guid", "") or url)
            records.append(
                RawNewsRecord(
                    source_id=self.source.source_id,
                    publisher=self.source.publisher,
                    title=title,
                    body=str(entry.get("summary", "") or entry.get("description", "")),
                    url=url,
                    guid=guid,
                    published_at=published_at,
                    ingested_at=now,
                    trust_score=self.source.trust_score,
                    category=self.source.category,
                    raw={
                        "author": str(entry.get("author", "")),
                        "latency_ms": (time.perf_counter() - started) * 1000.0,
                        "adapter": self.source.adapter,
                        "official_domain": self.source.official_domain,
                    },
                )
            )
        return records


class JSONNewsAdapter:
    def __init__(self, source: NewsSourceConfig) -> None:
        self.source = source

    async def fetch(self, session: aiohttp.ClientSession, health: Optional[SourceHealth] = None) -> List[RawNewsRecord]:
        started = time.perf_counter()
        timeout = aiohttp.ClientTimeout(total=self.source.timeout_seconds)
        headers = browser_headers(self.source.url)
        headers["Accept"] = "application/json,text/plain,*/*"
        headers["Referer"] = "https://news.treeofalpha.com/"
        if health and health.etag:
            headers["If-None-Match"] = health.etag
        async with session.get(self.source.url, timeout=timeout, headers=headers) as response:
            if response.status == 304:
                return []
            if response.status == 429:
                raise RateLimitedError(parse_retry_after(response.headers.get("Retry-After")))
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            payload = await response.json(content_type=None)
            if health is not None:
                health.etag = response.headers.get("ETag", health.etag)
        items = extract_json_items(payload, self.source.items_path)
        now = datetime.now(timezone.utc)
        records: List[RawNewsRecord] = []
        for item in items[: self.source.max_entries]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("headline") or item.get("en") or item.get("text") or "").strip()
            if not title:
                continue
            url = canonical_url(str(item.get("url") or item.get("link") or ""))
            guid = str(item.get("id") or item.get("guid") or url)
            t_val = item.get("time") or item.get("timestamp") or item.get("publishedAt") or item.get("published") or item.get("updated")
            if isinstance(t_val, (int, float)):
                if t_val > 1e11:
                    t_val = t_val / 1000.0
                published_at = datetime.fromtimestamp(t_val, timezone.utc)
            elif isinstance(t_val, str) and t_val.isdigit():
                iv = int(t_val)
                if iv > 1e11:
                    iv = iv / 1000.0
                published_at = datetime.fromtimestamp(iv, timezone.utc)
            else:
                published_at = parse_iso_time(t_val)
            records.append(
                RawNewsRecord(
                    source_id=self.source.source_id,
                    publisher=self.source.publisher,
                    title=title,
                    body=str(item.get("description") or item.get("summary") or item.get("body") or ""),
                    url=url,
                    guid=guid,
                    published_at=published_at,
                    ingested_at=now,
                    trust_score=self.source.trust_score,
                    category=self.source.category,
                    raw={"adapter": "json", "latency_ms": (time.perf_counter() - started) * 1000.0, "official_domain": self.source.official_domain},
                )
            )
        return records


class WebhookNewsAdapter:
    """Credential-free ingest interface. Call ingest() from an authenticated operator endpoint."""

    def __init__(self, source: NewsSourceConfig) -> None:
        self.source = source

    async def fetch(self, session: aiohttp.ClientSession, health: Optional[SourceHealth] = None) -> List[RawNewsRecord]:
        return []

    def ingest(self, payload: Dict[str, Any]) -> List[RawNewsRecord]:
        now = datetime.now(timezone.utc)
        title = str(payload.get("title") or payload.get("headline") or "").strip()
        if not title:
            return []
        url = canonical_url(str(payload.get("url") or payload.get("link") or ""))
        return [
            RawNewsRecord(
                source_id=self.source.source_id,
                publisher=self.source.publisher,
                title=title,
                body=str(payload.get("body") or payload.get("text") or ""),
                url=url,
                guid=str(payload.get("id") or payload.get("guid") or url or title),
                published_at=parse_iso_time(payload.get("published")),
                ingested_at=now,
                trust_score=self.source.trust_score,
                category=self.source.category,
                raw={"adapter": "webhook", "official_domain": self.source.official_domain},
            )
        ]


class XNewsAdapter:
    """Official X/Twitter API v2 using the operator's own bearer token. No scraping, no leaked keys."""

    def __init__(self, source: NewsSourceConfig) -> None:
        self.source = source

    def _token(self) -> str:
        from urllib.parse import unquote

        raw = (
            os.getenv("X_BEARER_TOKEN")
            or os.getenv("TWITTER_BEARER")
            or os.getenv("TWITTER_BEARER_TOKEN")
            or ""
        ).strip()
        return unquote(raw) if raw else ""

    async def fetch(self, session: aiohttp.ClientSession, health: Optional[SourceHealth] = None) -> List[RawNewsRecord]:
        token = self._token()
        if not token:
            return []
        started = time.perf_counter()
        query = (self.source.items_path or self.source.url or "").strip()
        if query.startswith("http"):
            query = "SEC OR FOMC OR Bitcoin lang:en -is:retweet"
        params = {
            "query": query[:512],
            "max_results": str(min(20, max(10, self.source.max_entries))),
            "tweet.fields": "created_at,lang",
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "LighterNewsTerminal/2.0",
        }
        timeout = aiohttp.ClientTimeout(total=max(12.0, self.source.timeout_seconds))
        async with session.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params=params,
            headers=headers,
            timeout=timeout,
        ) as response:
            body = await response.text()
            if response.status == 429:
                raise RateLimitedError(parse_retry_after(response.headers.get("Retry-After")))
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status} {body[:200]}")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ValueError("X API returned non-JSON") from exc
        now = datetime.now(timezone.utc)
        records: List[RawNewsRecord] = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("text") or "").strip().split("\n")[0]
            if not title:
                continue
            tweet_id = str(item.get("id") or "")
            url = f"https://x.com/i/web/status/{tweet_id}" if tweet_id else ""
            records.append(
                RawNewsRecord(
                    source_id=self.source.source_id,
                    publisher=self.source.publisher,
                    title=title[:280],
                    body=str(item.get("text") or ""),
                    url=url,
                    guid=tweet_id or url or title,
                    published_at=parse_iso_time(item.get("created_at")),
                    ingested_at=now,
                    trust_score=self.source.trust_score,
                    category=self.source.category,
                    raw={"adapter": "x", "latency_ms": (time.perf_counter() - started) * 1000.0},
                )
            )
        return records[: self.source.max_entries]


class RateLimitedError(RuntimeError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"HTTP 429 rate limited retry_after={retry_after:.1f}s")
        self.retry_after = retry_after


def build_adapter(source: NewsSourceConfig) -> Any:
    if source.adapter in {"rss", "atom", "official"}:
        return RSSNewsAdapter(source)
    if source.adapter == "json":
        return JSONNewsAdapter(source)
    if source.adapter == "webhook":
        return WebhookNewsAdapter(source)
    if source.adapter == "x":
        return XNewsAdapter(source)
    raise ValueError(f"unsupported adapter: {source.adapter}")


class NewsSourceScheduler:
    def __init__(
        self,
        registry: NewsSourceRegistry,
        on_records: Callable[[Sequence[RawNewsRecord]], Awaitable[None]],
        max_concurrency: int = 64,
        db_path: Optional[str] = None,
    ) -> None:
        self.registry = registry
        self.on_records = on_records
        self.max_concurrency = max(1, max_concurrency)
        self.health_store = SourceHealthStore(db_path)
        self.health: Dict[str, SourceHealth] = {}
        for source in registry.enabled():
            restored = self.health_store.load(source.source_id)
            if restored:
                restored.circuit_open_until = 0.0
                restored.retry_after_until = 0.0
            self.health[source.source_id] = restored or SourceHealth(source.source_id)
        self.rate_limiter = DomainRateLimiter()
        self._stopped = asyncio.Event()
        self._tasks: List[asyncio.Task[Any]] = []
        self._last_poll: Dict[str, float] = {}
        self.webhooks = {
            source.source_id: WebhookNewsAdapter(source)
            for source in registry.enabled()
            if source.adapter == "webhook"
        }

    async def ingest_webhook(self, source_id: str, payload: Dict[str, Any]) -> int:
        adapter = self.webhooks.get(source_id)
        if adapter is None:
            raise KeyError(f"unknown webhook source: {source_id}")
        records = adapter.ingest(payload)
        if records:
            await self.on_records(records)
        return len(records)

    async def run_once(self, due_only: bool = False, session: Optional[aiohttp.ClientSession] = None) -> None:
        own_session = session is None
        if own_session:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(limit=self.max_concurrency, ssl=ssl_context, ttl_dns_cache=300)
            session = aiohttp.ClientSession(connector=connector)
        assert session is not None
        try:
            semaphore = asyncio.Semaphore(self.max_concurrency)
            now = time.time()
            sources = []
            for source in self.registry.enabled():
                if source.adapter == "webhook":
                    continue
                last = self._last_poll.get(source.source_id, 0.0)
                if due_only and now - last < source.interval_seconds:
                    continue
                sources.append(source)

            async def fetch_one(source: NewsSourceConfig) -> None:
                state = self.health.setdefault(source.source_id, SourceHealth(source.source_id))
                if state.circuit_open:
                    return
                async with semaphore:
                    started = time.perf_counter()
                    try:
                        await self.rate_limiter.acquire(source.url)
                        records = await build_adapter(source).fetch(session, state)
                        state.status = "healthy"
                        state.successes += 1
                        state.consecutive_failures = 0
                        state.circuit_open_until = 0.0
                        state.last_success_at = time.time()
                        state.last_latency_ms = (time.perf_counter() - started) * 1000.0
                        self._last_poll[source.source_id] = time.time()
                        if records:
                            state.last_article_at = time.time()
                            await self.on_records(records)
                    except RateLimitedError as exc:
                        state.status = "throttled"
                        state.failures += 1
                        state.consecutive_failures += 1
                        state.last_failure_at = time.time()
                        state.last_error = str(exc)[:300]
                        state.retry_after_until = time.time() + max(1.0, exc.retry_after)
                    except Exception as exc:
                        state.status = "degraded"
                        state.failures += 1
                        state.consecutive_failures += 1
                        state.last_failure_at = time.time()
                        state.last_error = str(exc)[:300]
                        if state.consecutive_failures >= 3:
                            state.circuit_open_until = time.time() + min(60.0, 2 ** state.consecutive_failures)
                    finally:
                        self.health_store.save(state)

            await asyncio.gather(*(fetch_one(source) for source in sources))
        finally:
            if own_session:
                await session.close()

    async def run_forever(self) -> None:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(limit=self.max_concurrency, ssl=ssl_context, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            while not self._stopped.is_set():
                await self.run_once(due_only=True, session=session)
                await asyncio.sleep(0.25 + random.uniform(0.0, 0.15))

    async def stop(self) -> None:
        self._stopped.set()
        for task in self._tasks:
            task.cancel()


def parse_entry_time(entry: Any) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    for key in ("published", "updated"):
        value = entry.get(key)
        if value:
            try:
                parsed = parsedate_to_datetime(str(value))
                return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
            except (TypeError, ValueError, OverflowError):
                return None
    return None


def parse_iso_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return parse_entry_time({"published": value})
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def parse_retry_after(value: Optional[str]) -> float:
    if not value:
        return 5.0
    try:
        return max(1.0, float(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(1.0, (when.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 5.0


def canonical_url(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value.strip())
    query = "&".join(
        part for part in parts.query.split("&") if part and not part.lower().startswith(("utm_", "fbclid", "gclid"))
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def stable_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "ignore")).hexdigest()


def extract_json_items(payload: Any, items_path: str = "") -> List[Any]:
    if items_path:
        current = payload
        for key in items_path.split("."):
            if not isinstance(current, dict):
                return []
            current = current.get(key)
        return current if isinstance(current, list) else []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("articles", "items", "data", "results", "entries"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and isinstance(value.get("items"), list):
                return value["items"]
    return []


def load_sources_from_env() -> List[NewsSourceConfig]:
    raw = os.getenv("NEWS_EXTRA_SOURCES", "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("NEWS_EXTRA_SOURCES must be valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("NEWS_EXTRA_SOURCES must be a JSON list")
    return [NewsSourceConfig(**item) for item in payload]


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _gnews(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


def ticker_news_source(symbol: str) -> NewsSourceConfig:
    """Google News feed for a Lighter market that just appeared."""
    from news_universe import ASSET_ALIASES

    sym = (symbol or "").upper()
    names = [alias for alias, mapped in ASSET_ALIASES.items() if mapped == sym][:4]
    parts = [sym] + [f'"{name}"' for name in names]
    query = "(" + " OR ".join(parts) + ") when:12h"
    return NewsSourceConfig(
        source_id=f"tkr_{sym.lower()}",
        publisher=f"{sym} news",
        url=_gnews(query),
        trust_score=0.70,
        interval_seconds=60.0,
        timeout_seconds=15.0,
        max_entries=12,
        allow_live=True,
        category="media",
    )


def register_ticker_sources(registry: "NewsSourceRegistry", symbols: Iterable[str], limit: int = 80) -> List[str]:
    """Attach a Google News RSS feed for newly listed (or open) Lighter tickers."""
    added: List[str] = []
    seen = set(registry._sources)
    for symbol in symbols:
        if len(added) >= limit:
            break
        sym = (symbol or "").upper().strip()
        if not sym:
            continue
        source = ticker_news_source(sym)
        if source.source_id in seen:
            continue
        registry.register(source)
        seen.add(source.source_id)
        added.append(sym)
    return added


def default_sources() -> List[NewsSourceConfig]:
    core = [
        NewsSourceConfig("cointelegraph", "Cointelegraph", "https://cointelegraph.com/rss", trust_score=0.72, timeout_seconds=15.0, fallback_urls=(_gnews("site:cointelegraph.com when:12h"),)),
        NewsSourceConfig("coindesk", "CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml", trust_score=0.78, timeout_seconds=15.0, fallback_urls=("https://www.coindesk.com/arc/outboundfeeds/rss/", _gnews("site:coindesk.com when:12h"))),
        NewsSourceConfig("decrypt", "Decrypt", "https://decrypt.co/feed", trust_score=0.68, timeout_seconds=15.0),
        NewsSourceConfig(
            "x_wires",
            "X Wires",
            "https://api.twitter.com/2/tweets/search/recent",
            adapter="x",
            items_path="(from:Reuters OR from:AP OR from:WSJ OR from:FT OR from:business) lang:en -is:retweet",
            trust_score=0.82,
            interval_seconds=90.0,
            timeout_seconds=15.0,
            max_entries=15,
        ),
        NewsSourceConfig(
            "x_crypto",
            "X Crypto",
            "https://api.twitter.com/2/tweets/search/recent",
            adapter="x",
            items_path="(from:CoinDesk OR from:WatcherGuru OR from:tier10k OR from:lookonchain) (Bitcoin OR Ethereum OR SEC OR ETF) lang:en -is:retweet",
            trust_score=0.62,
            interval_seconds=90.0,
            timeout_seconds=15.0,
            max_entries=15,
        ),
        NewsSourceConfig(
            "x_macro",
            "X Macro",
            "https://api.twitter.com/2/tweets/search/recent",
            adapter="x",
            items_path="(FOMC OR CPI OR NFP OR \"rate cut\" OR OPEC OR \"spot ETF\") lang:en -is:retweet",
            trust_score=0.70,
            interval_seconds=120.0,
            timeout_seconds=15.0,
            max_entries=15,
        ),
        NewsSourceConfig("theblock", "The Block", "https://www.theblock.co/rss.xml", trust_score=0.78, timeout_seconds=15.0, fallback_urls=(_gnews("site:theblock.co when:12h"),)),
        NewsSourceConfig("bitcoinmagazine", "Bitcoin Magazine", "https://bitcoinmagazine.com/.rss/full/", trust_score=0.68, timeout_seconds=15.0, fallback_urls=(_gnews("site:bitcoinmagazine.com when:12h"),)),
        NewsSourceConfig("thedefiant", "The Defiant", "https://thedefiant.io/feed", trust_score=0.74, timeout_seconds=15.0, fallback_urls=(_gnews("site:thedefiant.io when:12h"),)),
        NewsSourceConfig("blockworks", "Blockworks", "https://blockworks.co/feed", trust_score=0.74, timeout_seconds=15.0, fallback_urls=(_gnews("site:blockworks.co when:12h"),)),
        NewsSourceConfig("dlnews", "DL News", "https://www.dlnews.com/arc/outboundfeeds/rss/", trust_score=0.73, timeout_seconds=15.0, fallback_urls=(_gnews("site:dlnews.com when:12h"),)),
        NewsSourceConfig("cryptoslate", "CryptoSlate", "https://cryptoslate.com/feed/", trust_score=0.62, timeout_seconds=15.0, fallback_urls=(_gnews("site:cryptoslate.com when:12h"),)),
        NewsSourceConfig(
            "sec_crypto",
            "SEC Crypto",
            "https://www.sec.gov/news/pressreleases.rss",
            category="regulator",
            trust_score=0.95,
            adapter="official",
        ),
        NewsSourceConfig(
            "cftc",
            "CFTC",
            "https://www.cftc.gov/rss.xml",
            category="regulator",
            trust_score=0.93,
            adapter="official",
        ),
        NewsSourceConfig(
            "ethereum_blog",
            "Ethereum Foundation",
            "https://blog.ethereum.org/feed.xml",
            category="official",
            trust_score=0.96,
            adapter="official",
        ),
        NewsSourceConfig(
            "coinbase_blog",
            "Coinbase",
            "https://www.coinbase.com/blog/rss.xml",
            category="exchange",
            trust_score=0.88,
            adapter="official",
            timeout_seconds=15.0,
            fallback_urls=(_gnews("site:coinbase.com/blog when:12h"),),
        ),
        NewsSourceConfig(
            "kraken_blog",
            "Kraken",
            "https://blog.kraken.com/feed/",
            category="exchange",
            trust_score=0.86,
            adapter="official",
            timeout_seconds=20.0,
            fallback_urls=(_gnews("site:blog.kraken.com when:12h"),),
        ),
        NewsSourceConfig(
            "binance_blog",
            "Binance",
            _gnews("site:binance.com (listing OR listed OR delist OR announce) when:12h"),
            category="exchange",
            trust_score=0.80,
            adapter="official",
            timeout_seconds=15.0,
        ),
        NewsSourceConfig(
            "tree_news",
            "TreeNews",
            "https://news.treeofalpha.com/api/news",
            adapter="json",
            category="media",
            trust_score=0.80,
            interval_seconds=5.0,
            timeout_seconds=12.0,
        ),
        NewsSourceConfig(
            "coinbase_status",
            "Coinbase Status",
            "https://status.coinbase.com/history.atom",
            adapter="atom",
            category="exchange",
            trust_score=0.90,
        ),
        NewsSourceConfig(
            "kraken_status",
            "Kraken Status",
            "https://status.kraken.com/history.rss",
            adapter="rss",
            category="exchange",
            trust_score=0.88,
        ),
        NewsSourceConfig("bloomberg_crypto", "Bloomberg Crypto", "https://feeds.bloomberg.com/crypto/news.rss", trust_score=0.92, interval_seconds=15.0),
        NewsSourceConfig("bloomberg_markets", "Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss", trust_score=0.90, interval_seconds=20.0),
        NewsSourceConfig("bloomberg_econ", "Bloomberg Economics", "https://feeds.bloomberg.com/economics/news.rss", trust_score=0.90, interval_seconds=30.0),
        NewsSourceConfig("bloomberg_tech", "Bloomberg Technology", "https://feeds.bloomberg.com/technology/news.rss", trust_score=0.86, interval_seconds=30.0),
        NewsSourceConfig("wsj_markets", "WSJ Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", trust_score=0.90, interval_seconds=20.0),
        NewsSourceConfig("ft", "Financial Times", _gnews("site:ft.com (markets OR stocks OR oil OR fed OR bitcoin) when:12h"), trust_score=0.90, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("nyt_business", "NYT Business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", trust_score=0.86, interval_seconds=30.0),
        NewsSourceConfig("cnbc_crypto", "CNBC Crypto", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", trust_score=0.80, interval_seconds=15.0, timeout_seconds=15.0, fallback_urls=(_gnews("site:cnbc.com crypto when:12h"),)),
        NewsSourceConfig("cnbc_finance", "CNBC Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", trust_score=0.78, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("bbc_business", "BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", trust_score=0.84, interval_seconds=30.0),
        NewsSourceConfig("guardian_business", "The Guardian Business", "https://www.theguardian.com/uk/business/rss", trust_score=0.78, interval_seconds=30.0),
        NewsSourceConfig("marketwatch", "MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/", trust_score=0.74, interval_seconds=20.0),
        NewsSourceConfig("yahoo_finance", "Yahoo Finance", "https://finance.yahoo.com/news/rssindex", trust_score=0.66, interval_seconds=20.0),
        NewsSourceConfig("nasdaq_crypto", "Nasdaq Crypto", "https://www.nasdaq.com/feed/rssoutbound?category=Cryptocurrencies", trust_score=0.76, interval_seconds=20.0, timeout_seconds=15.0, fallback_urls=(_gnews("site:nasdaq.com crypto when:12h"),)),
        NewsSourceConfig("fortune", "Fortune", "https://fortune.com/feed/", trust_score=0.76, interval_seconds=30.0),
        NewsSourceConfig("investing_crypto", "Investing.com Crypto", "https://www.investing.com/rss/news_301.rss", trust_score=0.62, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig(
            "reuters_google",
            "Reuters via Google News",
            "https://news.google.com/rss/search?q=when:24h+site:reuters.com+(bitcoin+OR+ethereum+OR+crypto+OR+SEC+OR+ETF)&hl=en-US&gl=US&ceid=US:en",
            trust_score=0.88,
            interval_seconds=15.0,
        ),
        NewsSourceConfig(
            "bloomberg_google",
            "Bloomberg via Google News",
            "https://news.google.com/rss/search?q=when:24h+site:bloomberg.com+(bitcoin+OR+ethereum+OR+crypto+OR+ETF+OR+oil+OR+fed+OR+stocks)&hl=en-US&gl=US&ceid=US:en",
            trust_score=0.90,
            interval_seconds=15.0,
        ),
        NewsSourceConfig("bbc_world", "BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", trust_score=0.84, interval_seconds=20.0),
        NewsSourceConfig("aljazeera", "Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", trust_score=0.80, interval_seconds=20.0),
        NewsSourceConfig("dw", "Deutsche Welle", "https://rss.dw.com/rdf/rss-en-all", trust_score=0.80, interval_seconds=30.0),
        NewsSourceConfig("oilprice", "OilPrice", "https://oilprice.com/rss/main", category="research", trust_score=0.78, interval_seconds=15.0),
        NewsSourceConfig("mining_com", "Mining.com", "https://www.mining.com/feed/", category="research", trust_score=0.70, interval_seconds=30.0),
        NewsSourceConfig(
            "fxstreet",
            "FXStreet",
            _gnews("site:fxstreet.com (forex OR eur OR jpy OR gbp OR fed) when:12h"),
            category="research",
            trust_score=0.72,
            interval_seconds=15.0,
            timeout_seconds=15.0,
            fallback_urls=("https://www.fxstreet.com/rss", "https://www.fxstreet.com/news/feed"),
        ),
        NewsSourceConfig("dailyfx", "DailyFX", "https://www.dailyfx.com/feeds/market-news", category="research", trust_score=0.70, interval_seconds=20.0, timeout_seconds=15.0, fallback_urls=(_gnews("site:dailyfx.com when:12h"),)),
        NewsSourceConfig("federal_reserve", "Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml", category="regulator", trust_score=0.97, adapter="official", interval_seconds=30.0, timeout_seconds=20.0),
        NewsSourceConfig("boe", "Bank of England", "https://www.bankofengland.co.uk/rss/news", category="regulator", trust_score=0.95, adapter="official", interval_seconds=60.0, timeout_seconds=20.0),
        NewsSourceConfig(
            "ap_google",
            "AP via Google News",
            "https://news.google.com/rss/search?q=when:24h+site:apnews.com+(markets+OR+oil+OR+fed+OR+stocks+OR+bitcoin)&hl=en-US&gl=US&ceid=US:en",
            trust_score=0.88,
            interval_seconds=15.0,
        ),
        NewsSourceConfig(
            "nikkei_google",
            "Nikkei via Google News",
            "https://news.google.com/rss/search?q=when:24h+site:asia.nikkei.com+(markets+OR+yen+OR+oil+OR+stocks)&hl=en-US&gl=US&ceid=US:en",
            trust_score=0.86,
            interval_seconds=20.0,
        ),
        NewsSourceConfig(
            "scmp_google",
            "SCMP via Google News",
            "https://news.google.com/rss/search?q=when:24h+site:scmp.com+(markets+OR+china+OR+oil+OR+stocks)&hl=en-US&gl=US&ceid=US:en",
            trust_score=0.80,
            interval_seconds=20.0,
        ),
        NewsSourceConfig(
            "afp_google",
            "AFP via Google News",
            "https://news.google.com/rss/search?q=when:24h+site:afp.com+(markets+OR+oil+OR+fed+OR+stocks)&hl=en-US&gl=US&ceid=US:en",
            trust_score=0.86,
            interval_seconds=20.0,
        ),
        NewsSourceConfig("wsj_world", "WSJ World", "https://feeds.a.dj.com/rss/RSSWorldNews.xml", trust_score=0.88, interval_seconds=25.0),
        NewsSourceConfig("cnbc_top", "CNBC Top News", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", trust_score=0.80, interval_seconds=15.0, timeout_seconds=15.0),
        NewsSourceConfig("cnbc_world", "CNBC World", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362", trust_score=0.78, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("nyt_world", "NYT World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", trust_score=0.86, interval_seconds=30.0),
        NewsSourceConfig("nyt_dealbook", "NYT DealBook", "https://rss.nytimes.com/services/xml/rss/nyt/Dealbook.xml", trust_score=0.84, interval_seconds=30.0),
        NewsSourceConfig("guardian_world", "Guardian World", "https://www.theguardian.com/world/rss", trust_score=0.78, interval_seconds=30.0),
        NewsSourceConfig("kitco", "Kitco", "https://www.kitco.com/rss/KitcoNews.xml", category="research", trust_score=0.74, interval_seconds=20.0, timeout_seconds=15.0, fallback_urls=(_gnews("site:kitco.com (gold OR silver OR copper) when:12h"),)),
        NewsSourceConfig("eia", "EIA", "https://www.eia.gov/rss/todayinenergy.xml", category="regulator", trust_score=0.90, adapter="official", interval_seconds=60.0, timeout_seconds=20.0),
        NewsSourceConfig("ecb", "ECB", "https://www.ecb.europa.eu/rss/press.html", category="regulator", trust_score=0.96, adapter="official", interval_seconds=60.0, timeout_seconds=20.0),
        NewsSourceConfig("techcrunch", "TechCrunch", "https://techcrunch.com/feed/", trust_score=0.70, interval_seconds=30.0),
        NewsSourceConfig("theverge", "The Verge", "https://www.theverge.com/rss/index.xml", trust_score=0.66, interval_seconds=30.0),
        NewsSourceConfig("arstechnica", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", trust_score=0.70, interval_seconds=40.0),
        NewsSourceConfig("prnewswire", "PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss", trust_score=0.64, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("beincrypto", "BeInCrypto", "https://beincrypto.com/feed/", trust_score=0.60, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("bitcoincom", "Bitcoin.com", "https://news.bitcoin.com/feed/", trust_score=0.58, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("utoday", "U.Today", "https://u.today/rss", trust_score=0.56, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("investing_forex", "Investing.com FX", "https://www.investing.com/rss/news_1.rss", category="research", trust_score=0.62, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("investing_commodities", "Investing.com Commodities", "https://www.investing.com/rss/news_11.rss", category="research", trust_score=0.62, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("yahoo_markets", "Yahoo Markets", "https://finance.yahoo.com/news/rssindex", trust_score=0.64, interval_seconds=25.0),
        NewsSourceConfig("reuters_wire", "Reuters via Google News", _gnews("site:reuters.com (markets OR oil OR fed OR bitcoin OR stocks) when:12h"), trust_score=0.88, interval_seconds=15.0, timeout_seconds=15.0),
        NewsSourceConfig("ap_wire", "AP via Google News", _gnews("site:apnews.com (markets OR oil OR fed OR stocks) when:12h"), trust_score=0.86, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("blockworks", "Blockworks", "https://blockworks.co/feed", category="media", trust_score=0.82, interval_seconds=15.0, timeout_seconds=15.0),
        NewsSourceConfig("dlnews", "DL News", "https://www.dlnews.com/arc/outboundfeeds/rss/", category="media", trust_score=0.84, interval_seconds=15.0, timeout_seconds=15.0),
        NewsSourceConfig("crypto_briefing", "Crypto Briefing", "https://cryptobriefing.com/feed/", category="media", trust_score=0.74, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("newsbtc", "NewsBTC", "https://www.newsbtc.com/feed/", category="media", trust_score=0.68, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("cryptopotato", "CryptoPotato", "https://cryptopotato.com/feed/", category="media", trust_score=0.70, interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig("forexlive", "ForexLive", "https://www.forexlive.com/feed/news", category="research", trust_score=0.80, interval_seconds=10.0, timeout_seconds=12.0),
        NewsSourceConfig("seeking_alpha_currents", "Seeking Alpha Currents", "https://seekingalpha.com/market_currents.xml", category="media", trust_score=0.75, interval_seconds=15.0, timeout_seconds=15.0),
        NewsSourceConfig("cftc_official", "CFTC Press Releases", "https://www.cftc.gov/rss/pressreleases.xml", category="regulator", trust_score=0.95, adapter="official", interval_seconds=60.0, timeout_seconds=20.0),
        NewsSourceConfig("doj_crypto", "DOJ Press Releases", "https://www.justice.gov/feeds/opa/justice-news.xml", category="regulator", trust_score=0.92, adapter="official", interval_seconds=60.0, timeout_seconds=20.0),
        NewsSourceConfig("binance_listings", "Binance Listings", _gnews("site:binance.com (listing OR will list OR launchpool OR opens trading) when:6h"), category="exchange", trust_score=0.96, interval_seconds=8.0, timeout_seconds=12.0),
        NewsSourceConfig("upbit_announcements", "Upbit Korea", _gnews("site:upbit.com (KRW OR listing OR digital asset) when:6h"), category="exchange", trust_score=0.94, interval_seconds=10.0, timeout_seconds=12.0),
        NewsSourceConfig("bithumb_announcements", "Bithumb Korea", _gnews("site:bithumb.com (KRW OR listing OR market) when:6h"), category="exchange", trust_score=0.94, interval_seconds=10.0, timeout_seconds=12.0),
        NewsSourceConfig("coinbase_listings", "Coinbase Roadmap & Listings", _gnews("site:coinbase.com (roadmap OR will list OR adds support) when:6h"), category="exchange", trust_score=0.95, interval_seconds=10.0, timeout_seconds=12.0),
        NewsSourceConfig("cryptopanic_wire", "CryptoPanic Breaking", "https://cryptopanic.com/news/rss/", category="media", trust_score=0.88, interval_seconds=8.0, timeout_seconds=10.0),
        NewsSourceConfig("defillama_exploits", "DeFiLlama Exploits & Hacks", _gnews("site:defillama.com (hack OR exploit OR drained OR vulnerability) when:12h"), category="research", trust_score=0.94, interval_seconds=12.0, timeout_seconds=12.0),
        NewsSourceConfig("theblock", "The Block", "https://www.theblock.co/rss.xml", category="media", trust_score=0.90, interval_seconds=12.0, timeout_seconds=12.0),
        NewsSourceConfig("decrypt", "Decrypt News", "https://decrypt.co/feed", category="media", trust_score=0.86, interval_seconds=15.0, timeout_seconds=12.0),
        NewsSourceConfig("benzinga_wire", "Benzinga Real-Time Wire", "https://www.benzinga.com/feeds/rss/news", category="media", trust_score=0.85, interval_seconds=12.0, timeout_seconds=12.0),
        NewsSourceConfig("sec_edgar_wire", "SEC Press & S-1 Wire", "https://www.sec.gov/news/pressreleases.rss", category="regulator", trust_score=0.98, adapter="official", interval_seconds=20.0, timeout_seconds=15.0),
        NewsSourceConfig(
            "operator_webhook",
            "Operator Webhook",
            "webhook://operator",
            adapter="webhook",
            category="unverified",
            trust_score=0.40,
            allow_live=False,
        ),
    ]
    seen = {source.source_id for source in core}
    extra: List[NewsSourceConfig] = []
    try:
        from news_source_catalog import catalog_specs
        for item in catalog_specs():
            if item.get("source_id") in seen:
                continue
            try:
                cfg = NewsSourceConfig(**item)
            except Exception:
                continue
            seen.add(cfg.source_id)
            extra.append(cfg)
    except Exception:
        extra = []
    return core + extra
