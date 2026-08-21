import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from news_pipeline import NewsDeduplicator, NewsPipeline, NewsNormalizer, classify_event
from news_sources import NewsSourceConfig, NewsSourceRegistry, RawNewsRecord, canonical_url, parse_entry_time


def record(source_id="one", title="Exchange lists ETH", body="Official listing announced", url="https://example.com/a", score=0.9):
    return RawNewsRecord(
        source_id=source_id,
        publisher=source_id.title(),
        title=title,
        body=body,
        url=url,
        guid=url,
        published_at=datetime.now(timezone.utc),
        ingested_at=datetime.now(timezone.utc),
        trust_score=score,
        category="official",
    )


def test_default_registry_includes_bloomberg_and_wire_feeds():
    registry = NewsSourceRegistry()
    ids = {source.source_id for source in registry.enabled()}
    for required in ("bloomberg_crypto", "bloomberg_markets", "wsj_markets", "ft", "cnbc_crypto", "reuters_google", "oilprice", "fxstreet", "federal_reserve", "kitco", "ecb", "techcrunch", "prnewswire", "beincrypto", "dailyfx"):
        assert required in ids
    from news_sources import BROWSER_UA, browser_headers, build_adapter
    assert "Chrome" in browser_headers("https://cointelegraph.com/rss")["User-Agent"]
    assert "Chrome" in BROWSER_UA
    assert len(ids) >= 1000
    assert "x_wires" in ids
    x_src = next(s for s in registry.enabled() if s.source_id == "x_wires")
    assert x_src.adapter == "x"


def test_equity_and_commodity_aliases():
    from news_pipeline import extract_entities
    from news_universe import register_listed
    register_listed(["AAPL", "XAU", "WTI", "EURUSD", "XAUT", "SPY", "QQQ"])
    text = "Apple beats estimates as gold jumps and WTI crude oil surges; EURUSD slides"
    found = set(extract_entities(text))
    assert "AAPL" in found
    assert "XAU" in found
    assert "WTI" in found
    listed = set(extract_entities("Tether Gold XAUT rallies as SPY and QQQ futures jump"))
    assert "XAUT" in listed
    assert "SPY" in listed
    assert "QQQ" in listed
    assert classify_event("Brent Oil Price Tops $93 as U.S.-Iran Impasse Persists", "")[0] == "opec"


def test_direction_body_and_macro_routes():
    from news_direction import classify_with_body, macro_routes, theme_key
    from news_pipeline import NewsNormalizer

    kind, direction, _ = classify_with_body("Brent Oil Price Tops $93", "Traders bid crude after the jump in prices.")
    assert kind == "opec"
    assert direction == "BULLISH"
    event = NewsPipeline(min_sources=1).process([
        record("one", title="Fed signals hawkish rate hike", body="FOMC hikes rates, stocks sold off"),
    ])[0]
    routes = macro_routes(event)
    assert ("SPY", "SELL/SHORT") in routes
    assert theme_key(event).startswith("theme:rates")


def test_oil_headlines_share_one_theme_cluster():
    pipeline = NewsPipeline(min_sources=2)
    first = pipeline.process([record("one", title="Brent Oil Price Tops $93 as Iran impasse persists", body="crude jumps")])
    second = pipeline.process([record("two", title="WTI crude oil surges after supply cut", body="opec")])
    assert first and second
    assert first[0].cluster_id == second[0].cluster_id


def test_source_registry_validates_and_preserves_identity():
    registry = NewsSourceRegistry([NewsSourceConfig("sec", "SEC", "https://example.test/feed", category="regulator", trust_score=0.95)])
    assert registry.get("sec").publisher == "SEC"
    assert registry.get("sec").category == "regulator"


def test_url_and_timestamp_normalization():
    assert canonical_url("HTTPS://Example.COM/a/?utm_source=x&keep=1#frag") == "https://example.com/a?keep=1"
    parsed = parse_entry_time({"published": "Tue, 20 Aug 2026 12:00:00 GMT"})
    assert parsed is not None and parsed.tzinfo == timezone.utc


def test_normalizer_adds_provenance_and_entities():
    event = NewsNormalizer().normalize(record(title="ETH listing at 0x" + "a" * 40))
    assert event is not None
    assert event.publisher == "One"
    assert event.source_id == "one"
    assert "ETH" in event.entities
    assert len(event.content_hash) == 64


def test_dedupe_keeps_independent_sources_for_confirmation():
    pipeline = NewsPipeline(min_sources=2)
    events = pipeline.process([
        record("one", url="https://one.test/a"),
        record("two", url="https://two.test/a"),
    ])
    assert len(events) == 2
    assert pipeline.confirmed(events[-1])


def test_dedupe_rejects_same_source_near_duplicate():
    dedupe = NewsDeduplicator()
    first = NewsNormalizer().normalize(record("one", title="Exchange lists ETH today"))
    second = NewsNormalizer().normalize(record("one", title="Exchange lists ETH today!", url="https://example.com/b"))
    assert dedupe.accept(first)
    assert not dedupe.accept(second)


def test_classification_rejects_rumor_and_handles_correction():
    assert classify_event("Rumor: ETH may be listed", "reportedly soon")[0] == "rumor"
    assert classify_event("Not hacked, exchange confirms", "false report")[0] == "correction"


def test_classifier_taxonomy_and_negation():
    assert classify_event("Binance lists SOL", "")[0] == "listing"
    assert classify_event("Protocol drained in exploit", "") == ("exploit", "BEARISH", 0.85)
    assert classify_event("No approval from SEC", "")[0] == "correction"


def test_virtual_roundtable_is_not_virtual_token():
    from news_pipeline import extract_entities
    from news_quality import headline_has_subject_asset, quality_veto
    from news_universe import alias_symbol, register_listed

    register_listed(["VIRTUAL", "ETH"])
    alias_symbol("VIRTUAL")
    headline = "SEC to Host Virtual Roundtable on Modernizing IPOs and Expanding Access to Public Markets"
    found = set(extract_entities(headline))
    assert "VIRTUAL" not in found
    assert headline_has_subject_asset(headline, "VIRTUAL") is False
    assert "VIRTUAL" in set(extract_entities("Virtuals Protocol lists on Binance"))
    assert headline_has_subject_asset("$VIRTUAL dumps after exploit", "VIRTUAL") is True
    event = NewsPipeline(min_sources=1).process([
        record("sec", title=headline, body="The SEC will host a virtual roundtable.", url="https://www.sec.gov/news/a", score=0.95),
    ])
    if event:
        ok, reason = quality_veto(event[0])
        assert ok is False
        assert "process" in reason or "subject" in reason or "asset" in reason or "tradeable" in reason


def test_quality_gate_blocks_unrelated_and_requires_headline_subject():
    from news_quality import quality_veto

    junk = NewsPipeline(min_sources=1).process([
        record("one", title="Chinese InsurTech Firm Zhibao Adds 2,380 BTC", body="insurance treasury"),
    ])[0]
    ok, reason = quality_veto(junk)
    assert ok is False
    assert "veto" in reason or "tradeable" in reason or "subject" in reason

    listing = NewsPipeline(min_sources=1).process([
        record("one", title="Binance lists ETH perpetual", body="Official listing announced"),
    ])[0]
    ok, reason = quality_veto(listing)
    assert ok is True, reason

    chase = NewsPipeline(min_sources=1).process([
        record("one", title="Crypto Stocks Soar Alongside Bitcoin As Traders Cheer", body="bitcoin jumped overnight"),
    ])[0]
    ok, reason = quality_veto(chase)
    assert ok is False
    assert "price-action" in reason or "tradeable" in reason or "catalyst" in reason


def test_correction_invalidates_cluster():
    pipeline = NewsPipeline(min_sources=2)
    listing = pipeline.process([
        record("one", title="Exchange lists ETH", url="https://one.test/a"),
        record("two", title="Exchange lists ETH", url="https://two.test/a"),
    ])
    assert pipeline.confirmed(listing[-1])
    correction = pipeline.process([
        record("one", title="Correction: ETH listing false report", body="We retract this. not approved", url="https://one.test/c"),
    ])
    assert correction[0].event_type == "correction"
    assert any(item.invalidated for item in pipeline.confirmation._clusters.get(listing[-1].cluster_id, []))
