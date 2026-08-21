import asyncio
from datetime import datetime, timezone

from lighter_news_risk import LighterNewsRiskGate, MarketSnapshot
from news_pipeline import NormalizedNewsEvent


def event(confidence=0.9):
    now = datetime.now(timezone.utc)
    return NormalizedNewsEvent(
        event_id="event-1", source_id="source", publisher="Source", headline="Binance lists ETH", body="official listing",
        url="https://example.test", guid="g", published_at=now, ingested_at=now, source_score=0.9,
        category="official", content_hash="hash", entities=("ETH",), event_type="listing", direction="BULLISH",
        confidence=confidence, materiality=0.8,
    )


def test_paper_gate_reserves_and_releases():
    gate = LighterNewsRiskGate(live=False)
    decision = asyncio.run(gate.approve(event(), MarketSnapshot("ETH", 2500), 25, confirmed=False))
    assert decision.approved
    assert gate.reserved_usd == 25
    asyncio.run(gate.release(decision.reservation_id))
    assert gate.reserved_usd == 0


def test_live_gate_rejects_unconfirmed_and_unauthorized():
    gate = LighterNewsRiskGate(live=True)
    decision = asyncio.run(gate.approve(event(), MarketSnapshot("ETH", 2500), 25, confirmed=False, authorized=False))
    assert not decision.approved
    assert "authorization" in " ".join(decision.reasons)
    assert "confirmation" in " ".join(decision.reasons)


def test_live_gate_rejects_stale_market_data():
    gate = LighterNewsRiskGate(live=True)
    snapshot = MarketSnapshot("ETH", 2500, timestamp=0)
    decision = asyncio.run(gate.approve(event(), snapshot, 25, confirmed=True, authorized=True))
    assert not decision.approved
    assert "stale" in " ".join(decision.reasons)


def test_kill_switch_blocks_live():
    import os
    os.environ["NEWS_KILL_SWITCH"] = "true"
    try:
        from lighter_news_risk import live_execution_allowed
        assert live_execution_allowed(True) is False
        gate = LighterNewsRiskGate(live=True)
        decision = asyncio.run(gate.approve(event(), MarketSnapshot("ETH", 2500), 25, confirmed=True, authorized=True, collateral_usd=100))
        assert not decision.approved
        assert "kill switch" in " ".join(decision.reasons)
    finally:
        os.environ.pop("NEWS_KILL_SWITCH", None)


def test_cli_live_enables_execution():
    from lighter_news_risk import live_execution_allowed
    assert live_execution_allowed(False) is False
    assert live_execution_allowed(True) is True


def test_cooldown_applies_only_after_fill():
    gate = LighterNewsRiskGate(live=False)
    first = asyncio.run(gate.approve(event(), MarketSnapshot("ETH", 2500), 25, confirmed=False, asset="ETH", side="BUY/LONG"))
    assert first.approved
    second = asyncio.run(gate.approve(event(), MarketSnapshot("ETH", 2500), 25, confirmed=False, asset="ETH", side="BUY/LONG"))
    assert second.approved
    gate.record_fill("ETH")
    third = asyncio.run(gate.approve(event(), MarketSnapshot("ETH", 2500), 25, confirmed=False, asset="ETH", side="BUY/LONG"))
    assert not third.approved
    assert "cooldown" in " ".join(third.reasons)
