from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

# Explicit name -> Lighter symbol. Keep aliases unique enough to avoid English collisions.
ASSET_ALIASES: Dict[str, str] = {
    # crypto
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "ether": "ETH",
    "solana": "SOL",
    "hyperliquid": "HYPE",
    "ripple": "XRP",
    "dogecoin": "DOGE",
    "cardano": "ADA",
    "avalanche": "AVAX",
    "binance coin": "BNB",
    "litecoin": "LTC",
    "chainlink": "LINK",
    "polkadot": "DOT",
    # mega-cap equity
    "apple": "AAPL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "nvidia": "NVDA",
    "tesla": "TSLA",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "meta platforms": "META",
    "facebook": "META",
    "broadcom": "AVGO",
    "intel": "INTC",
    "amd": "AMD",
    "advanced micro devices": "AMD",
    "oracle": "ORCL",
    "palantir": "PLTR",
    "coinbase": "COIN",
    "robinhood": "HOOD",
    "microstrategy": "MSTR",
    "strategy": "MSTR",
    "alibaba": "BABA",
    "tsmc": "TSM",
    "taiwan semiconductor": "TSM",
    "asml": "ASML",
    "samsung": "SAMSUNGUSD",
    "hyundai": "HYUNDAIUSD",
    "tencent": "TENCENT",
    "xiaomi": "XIAOMI",
    "byd": "BYD",
    "nokia": "NOK",
    "ibm": "IBM",
    "dell": "DELL",
    "qualcomm": "QCOM",
    "micron": "MU",
    "arm holdings": "ARM",
    "gamestop": "GME",
    "moderna": "MRNA",
    "openai": "OPENAI",
    "anthropic": "ANTHROPIC",
    "spacex": "SPCX",
    # fx
    "eurusd": "EURUSD",
    "euro dollar": "EURUSD",
    "euro": "EURUSD",
    "gbpusd": "GBPUSD",
    "sterling": "GBPUSD",
    "cable": "GBPUSD",
    "british pound": "GBPUSD",
    "usdjpy": "USDJPY",
    "yen": "USDJPY",
    "japanese yen": "USDJPY",
    "audusd": "AUDUSD",
    "australian dollar": "AUDUSD",
    "nzdusd": "NZDUSD",
    "kiwi": "NZDUSD",
    "usdcad": "USDCAD",
    "loonie": "USDCAD",
    "canadian dollar": "USDCAD",
    "usdchf": "USDCHF",
    "swiss franc": "USDCHF",
    "usdhkd": "USDHKD",
    "hong kong dollar": "USDHKD",
    "usdkrw": "USDKRW",
    "korean won": "USDKRW",
    # commodities / rwa
    "gold": "XAU",
    "spot gold": "XAU",
    "silver": "XAG",
    "copper": "XCU",
    "wti": "WTI",
    "crude oil": "WTI",
    "crude": "WTI",
    "brent": "BRENTOIL",
    "brent oil": "BRENTOIL",
    "natural gas": "NATGAS",
    "natgas": "NATGAS",
    "wheat": "WHEAT",
    "platinum": "XPT",
    "palladium": "XPD",
    "pax gold": "PAXG",
    "xaut": "XAUT",
    "tether gold": "XAUT",
    "tethergold": "XAUT",
    "xau tether": "XAUT",
    "gold token": "XAUT",
    # indices / rates
    "s&p 500": "SPY",
    "s&p": "SPY",
    "sp500": "SPY",
    "spy": "SPY",
    "us500": "SPY",
    "nasdaq 100": "QQQ",
    "nasdaq-100": "QQQ",
    "nasdaq100": "QQQ",
    "qqq": "QQQ",
    "us100": "QQQ",
    "russell 2000": "IWM",
    "us 10-year": "US10Y",
    "10-year treasury": "US10Y",
    "treasury yield": "US10Y",
    # distinctive names only — never alias the English word "virtual"
    "virtuals": "VIRTUAL",
    "virtuals protocol": "VIRTUAL",
    "virtual protocol": "VIRTUAL",
}

_LISTED: Set[str] = set(ASSET_ALIASES.values())
# Tickers that are also ordinary English. Matching the lowercase word is banned;
# only $TICKER, ALL-CAPS ticker, or a distinctive multi-word alias may trade.
_AMBIGUOUS = {
    "S", "BE", "CC", "MU", "NOW", "BOT", "CAP", "SKY", "MET", "LIT", "EDGE",
    "DATA", "OPEN", "RAIL", "CHIP", "ALL", "NEW", "LOW", "TOP", "BIG", "ONE",
    "FOR", "THE", "AND", "ARE", "YOU", "NOT", "OUT", "CAN", "HAS", "WAS",
    "VIRTUAL", "RIVER", "PROVE", "PUMP", "GRASS", "MEGA", "LITE", "CORE",
    "LINE", "CASH", "POP", "MON", "ARC", "BIO", "FOLKS", "GRAM", "AERO",
    "TRUE", "REAL", "SAFE", "JUST", "MOVE", "PLAY", "GAME", "TIME", "WELL",
    "BEST", "FAST", "FREE", "HIGH", "LONG", "SHORT", "NEXT", "PLUS", "PRIME",
    "SMART", "LIGHT", "MOON", "PEACE", "HOPE", "LOVE", "LIFE", "WORLD",
}

TICKER_HINT = re.compile(
    r"\b(token|coin|protocol|perp|perps|futures|ticker|listing|listed|airdrop|otc)\b|\$",
    re.IGNORECASE,
)


_UNIVERSE_PATH = Path(__file__).with_name("lighter_universe.json")


def register_listed(symbols: Iterable[str]) -> None:
    for symbol in symbols:
        if symbol:
            _LISTED.add(symbol.upper())


def is_ambiguous_ticker(symbol: str) -> bool:
    return (symbol or "").upper() in _AMBIGUOUS


def alias_is_english_collision(alias: str, symbol: str) -> bool:
    """True when the alias is just the ticker written as an English word."""
    a = (alias or "").strip().lower()
    s = (symbol or "").upper()
    if not a or " " in a:
        return False
    return is_ambiguous_ticker(s) and a == s.lower()


def alias_symbol(symbol: str) -> None:
    """Map a newly listed Lighter ticker so headlines like 'XAUT' resolve."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return
    _LISTED.add(sym)
    if is_ambiguous_ticker(sym):
        return
    ASSET_ALIASES.setdefault(sym.lower(), sym)


def listed_symbols() -> Set[str]:
    return set(_LISTED)


def known_symbols() -> Set[str]:
    return set(_LISTED) | set(ASSET_ALIASES.values())


def load_catalog_snapshot(path: Optional[Path] = None) -> Set[str]:
    target = path or _UNIVERSE_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return {str(item).upper() for item in (data.get("symbols") or []) if item}
    except Exception:
        return set()


def save_catalog_snapshot(symbols: Iterable[str], path: Optional[Path] = None) -> None:
    target = path or _UNIVERSE_PATH
    payload = {
        "symbols": sorted({str(item).upper() for item in symbols if item}),
        "updated_at": time.time(),
    }
    target.write_text(json.dumps(payload), encoding="utf-8")


def sync_catalog(symbols: Iterable[str], path: Optional[Path] = None) -> Tuple[List[str], bool]:
    """Register live Lighter symbols. Returns (new_vs_disk, first_boot)."""
    now = {str(item).upper() for item in symbols if item}
    prev = load_catalog_snapshot(path)
    first = not prev
    for symbol in now:
        alias_symbol(symbol)
    register_listed(now)
    save_catalog_snapshot(now, path)
    return sorted(now - prev), first
