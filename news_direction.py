from __future__ import annotations

import hashlib
import re
from typing import List, Optional, Tuple

from news_pipeline import NormalizedNewsEvent, normalize_title

BULLISH_LEX = re.compile(
    r"\b(beat(s|ing)? estimates|earnings beat|record (revenue|profit)|jump(s|ed)?|surge(s|d)?|soar(s|ed)?|"
    r"rall(y|ies|ied)|tops?|climb(s|ed)?|boom|squeeze|approved|lists|listed|dovish|rate cut)\b",
    re.I,
)
BEARISH_LEX = re.compile(
    r"\b(miss(es|ed)?|cuts? guidance|profit warning|plunge(s|d)?|slump(s|ed)?|crash(es|ed)?|"
    r"tumble(s|d)?|collapse(s|d)?|hack(ed)?|exploit|delist|rejected|hawkish|rate hike|"
    r"sanction(s)?|tariff|embargo|bankrupt|insolvency)\b",
    re.I,
)
THEME_OIL = re.compile(r"\b(oil|brent|wti|crude|opec|iran.{0,20}oil|lng|diesel)\b", re.I)
THEME_GOLD = re.compile(r"\b(gold|xau|bullion)\b", re.I)
THEME_RATES = re.compile(r"\b(fomc|fed |federal reserve|rate hike|rate cut|hawkish|dovish|cpi|nfp|pce|payrolls)\b", re.I)
THEME_COPPER = re.compile(r"\b(copper|xcu)\b", re.I)


def lead_text(headline: str, body: str, limit: int = 400) -> str:
    return f"{headline or ''} {(body or '')[:limit]}".strip()


def classify_with_body(headline: str, body: str) -> Tuple[str, str, float]:
    from news_pipeline import classify_event as lexical_classify

    lead = lead_text(headline, body)
    event_type, direction, materiality = lexical_classify(headline, lead)
    if event_type in {"correction", "rumor", "satire", "opinion"}:
        return event_type, direction, materiality
    if direction in {"BULLISH", "BEARISH"} and event_type not in {"unknown"}:
        return event_type, direction, materiality
    if BEARISH_LEX.search(lead):
        if THEME_OIL.search(lead):
            return "opec", "BEARISH", max(materiality, 0.62)
        if THEME_RATES.search(lead) or THEME_GOLD.search(lead) or THEME_COPPER.search(lead):
            return "macro", "BEARISH", max(materiality, 0.62)
        return event_type, "BEARISH", materiality
    if BULLISH_LEX.search(lead):
        if THEME_OIL.search(lead):
            return "opec", "BULLISH", max(materiality, 0.62)
        if re.search(r"\bearnings\b", lead, re.I):
            return "earnings", "BULLISH", max(materiality, 0.62)
        if THEME_RATES.search(lead) or THEME_GOLD.search(lead) or THEME_COPPER.search(lead):
            return "macro", "BULLISH", max(materiality, 0.62)
        return event_type, "BULLISH", materiality
    return event_type, direction, materiality


def theme_key(event: NormalizedNewsEvent) -> str:
    text = lead_text(event.headline, event.body)
    if THEME_OIL.search(text):
        return "theme:oil"
    if THEME_GOLD.search(text):
        return "theme:gold"
    if THEME_RATES.search(text):
        return "theme:rates"
    if THEME_COPPER.search(text):
        return "theme:copper"
    entity = event.entities[0] if event.entities else normalize_title(event.headline)[:80]
    return f"theme:{entity}|{event.event_type}|{event.direction}"


def theme_cluster_id(event: NormalizedNewsEvent) -> str:
    return hashlib.sha256(theme_key(event).encode()).hexdigest()[:24]


def macro_routes(event: NormalizedNewsEvent) -> List[Tuple[str, str]]:
    """Return (symbol, side) overrides for rates/oil/gold shocks."""
    text = lead_text(event.headline, event.body).lower()
    routes: List[Tuple[str, str]] = []
    if THEME_RATES.search(text):
        if re.search(r"\b(hawkish|rate hike|hikes rates|hot cpi|hot inflation)\b", text):
            routes = [("SPY", "SELL/SHORT"), ("QQQ", "SELL/SHORT"), ("USDJPY", "BUY/LONG")]
        elif re.search(r"\b(dovish|rate cut|cuts rates|cool(s|ed)? inflation)\b", text):
            routes = [("SPY", "BUY/LONG"), ("QQQ", "BUY/LONG"), ("USDJPY", "SELL/SHORT")]
    elif THEME_OIL.search(text):
        if event.direction == "BEARISH" or re.search(r"\b(plunge|slump|crash|glut)\b", text):
            routes = [("WTI", "SELL/SHORT"), ("BRENTOIL", "SELL/SHORT")]
        else:
            routes = [("WTI", "BUY/LONG"), ("BRENTOIL", "BUY/LONG")]
    elif THEME_GOLD.search(text):
        if event.direction == "BEARISH":
            routes = [("XAU", "SELL/SHORT"), ("XAG", "SELL/SHORT")]
        else:
            routes = [("XAU", "BUY/LONG"), ("XAG", "BUY/LONG")]
    elif THEME_COPPER.search(text):
        side = "SELL/SHORT" if event.direction == "BEARISH" else "BUY/LONG"
        routes = [("XCU", side)]
    return routes
