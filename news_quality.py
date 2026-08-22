from __future__ import annotations

import re
from typing import Optional, Tuple

from news_pipeline import ASSET_ALIASES, NormalizedNewsEvent
from news_universe import (
    TICKER_HINT,
    _AMBIGUOUS,
    alias_is_english_collision,
    known_symbols,
    listed_symbols,
)

TRADEABLE_TYPES = frozenset({
    "listing", "delisting", "exploit", "approval", "rejection", "outage",
    "earnings", "macro", "regulatory", "opec", "sanction", "distress",
    "partnership", "breakout", "surge", "breakdown", "tariff",
    "upgrade", "mainnet", "etf", "whale", "arbitrage", "momentum", "general_crypto", "defi", "layer1",
})
REGULATOR_SOLO = frozenset({"regulator"})
HARD_VETO = re.compile(
    r"\b("
    r"op-ed|opinion column|how to buy|price prediction|airdrop checker|"
    r"mother-in-law|power of attorney|roth conversion|our son|"
    r"would rather rent|afraid ai will steal"
    r")\b",
    re.IGNORECASE,
)
PRICE_ACTION = re.compile(
    r"\b("
    r"soar(s|ed)?|rall(y|ies|ied)|jump(s|ed)?|surge(s|d)?|climb(s|ed)?|"
    r"plunge(s|d)?|slump(s|ed)?|tumble(s|d)?|"
    r"alongside bitcoin|crypto stocks|hits? (a |an )?(record|all-?time high|ath)"
    r")\b",
    re.IGNORECASE,
)
FRESH_CATALYST = re.compile(
    r"\b("
    r"list(s|ed|ing)|approv(ed|es|al)|etf|hack|exploit|delist|"
    r"opec|production cut|supply cut|sanction|tariff|embargo|"
    r"fomc|cpi|nfp|pce|rate (cut|hike)|hawkish|dovish|"
    r"bankrupt|chapter 11|insolvency|outage|halt(s|ed)?|"
    r"beats? estimates|earnings (beat|miss)|cuts? guidance|"
    r"buyback|treasury|partnership|invests?|acquisition|surges?|breaks? out"
    r")\b",
    re.IGNORECASE,
)


PROCESS_NOISE = re.compile(
    r"\b("
    r"roundtable|to host|hosts? (a |an )?(virtual )?(roundtable|workshop|webinar)|"
    r"seeks? (public )?comment|advisory committee|working group|"
    r"names? .{0,48}(director|officer|chair|coo|cfo)|"
    r"publishes updated (market )?statistics"
    r")\b",
    re.IGNORECASE,
)


def headline_has_subject_asset(headline: str, symbol: str) -> bool:
    if not headline or not symbol:
        return False
    sym = symbol.upper()
    if re.search(rf"\${re.escape(sym)}\b", headline, re.IGNORECASE):
        return True
    # Original case: ALL-CAPS ticker, not title-case English ("Virtual Roundtable").
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(sym)}(?![A-Za-z0-9])", headline, re.IGNORECASE):
        if sym in _AMBIGUOUS and not TICKER_HINT.search(headline):
            return False
        return True
    names = [alias for alias, mapped in ASSET_ALIASES.items() if mapped == sym and len(alias) > 2]
    lower = headline.lower()
    for name in names:
        if alias_is_english_collision(name, sym):
            continue
        if re.search(rf"\b{re.escape(name)}\b", lower):
            return True
    return False


def quality_veto(event: Optional[NormalizedNewsEvent]) -> Tuple[bool, str]:
    if event is None:
        return False, "news event is missing"
    if HARD_VETO.search(f"{event.headline} {event.body}"):
        return False, "headline matched hard veto (unrelated sector/noise)"
    if PROCESS_NOISE.search(event.headline or "") and event.event_type in {"regulatory", "macro"}:
        return False, "regulator calendar/process, not an asset catalyst"
    if event.event_type not in TRADEABLE_TYPES:
        return False, f"event type {event.event_type} is not auto-tradeable"
    if event.event_type in {"macro", "opec"} and PRICE_ACTION.search(event.headline or "") and not FRESH_CATALYST.search(f"{event.headline} {(event.body or '')[:240]}"):
        return False, "lagging price-action, not a fresh catalyst"
    if event.direction not in {"BULLISH", "BEARISH"}:
        return False, "no tradeable direction"
    universe = listed_symbols() | known_symbols()
    symbols = [item for item in event.entities if item in universe]
    if not symbols:
        # Check if asset exists in headline directly
        headline_upper = (event.headline or "").upper()
        for u_sym in ["BTC", "ETH", "SOL", "HYPE", "TRUMP", "DOGE", "AVAX", "NVDA", "TSLA", "AAPL"]:
            if u_sym in headline_upper:
                symbols = [u_sym]
                break
    if not symbols:
        return False, "no tradeable asset in entities"
    if not any(headline_has_subject_asset(event.headline, symbol) for symbol in symbols):
        return False, "asset is not the headline subject"
    return True, ""


def require_two_sources(event: NormalizedNewsEvent, independent_source_count: int, min_sources: int) -> bool:
    if event.source_score >= 0.75 or event.category in {"official", "regulator", "exchange"} or event.official_verified:
        return independent_source_count >= min_sources
    if event.event_type in {"listing", "approval", "exploit", "outage", "surge", "breakdown", "etf", "upgrade"}:
        return independent_source_count >= min_sources
    return independent_source_count >= min_sources
