from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from lighter_news_risk import MarketSnapshot
from news_pipeline import NormalizedNewsEvent
from news_quality import headline_has_subject_asset
from news_universe import register_listed


@dataclass(frozen=True)
class AssetMarket:
    symbol: str
    market_index: int
    decimals: int = 4
    enabled_sides: Tuple[str, ...] = ("BUY/LONG", "SELL/SHORT")
    default_price: float = 0.0
    tp_pct: float = 2.5
    sl_pct: float = 1.5
    price_source: str = "env"
    enabled: bool = True


class MarketRegistry:
    def __init__(self, markets: Optional[Iterable[AssetMarket]] = None) -> None:
        self._markets: Dict[str, AssetMarket] = {}
        for market in markets or default_markets():
            self._markets[market.symbol] = market

    def get(self, symbol: str) -> Optional[AssetMarket]:
        return self._markets.get(symbol.upper())

    def ingest_catalog(self, books) -> None:
        for book in books or []:
            if not isinstance(book, dict):
                continue
            status = str(book.get("status") or "active").lower()
            if status and status not in {"active", "listed"}:
                continue
            symbol = str(book.get("symbol") or "").upper()
            if not symbol:
                continue
            try:
                market_index = int(book.get("market_id", book.get("market_index", -1)))
            except (TypeError, ValueError):
                continue
            if market_index < 0:
                continue
            self._markets[symbol] = AssetMarket(
                symbol=symbol,
                market_index=market_index,
                decimals=int(book.get("size_decimals") or 4),
                default_price=float(book.get("last_trade_price") or book.get("mark_price") or 0),
            )
        register_listed(self._markets.keys())

    def resolve(self, event: NormalizedNewsEvent) -> Tuple[Optional[AssetMarket], str]:
        symbols = [item for item in event.entities if item in self._markets]
        unique = list(dict.fromkeys(symbols))
        subjects = [symbol for symbol in unique if headline_has_subject_asset(event.headline, symbol)]
        pick = subjects[0] if subjects else (unique[0] if len(unique) == 1 else None)
        if pick is None:
            if unique:
                return None, "ambiguous assets: " + ",".join(unique)
            return None, "unknown asset"
        market = self._markets[pick]
        if not market.enabled:
            return None, f"{market.symbol} market is disabled"
        return market, ""

    def enabled(self) -> List[AssetMarket]:
        return [market for market in self._markets.values() if market.enabled]


class TickerCache:
    def __init__(self) -> None:
        self._snapshots: Dict[str, MarketSnapshot] = {}

    def update(self, snapshot: MarketSnapshot) -> None:
        self._snapshots[snapshot.asset.upper()] = snapshot

    def get(self, asset: str) -> Optional[MarketSnapshot]:
        return self._snapshots.get(asset.upper())

    def snapshot_or_env(self, market: AssetMarket) -> MarketSnapshot:
        cached = self.get(market.symbol)
        if cached is not None:
            return cached
        price = float(os.getenv(f"LIGHTER_{market.symbol}_PRICE", str(market.default_price or 0.0)))
        return MarketSnapshot(market.symbol, price, timestamp=time.time())


def default_markets() -> List[AssetMarket]:
    return [
        AssetMarket("ETH", int(os.getenv("LIGHTER_ETH_MARKET_INDEX", "0")), default_price=float(os.getenv("LIGHTER_ETH_PRICE", "2650"))),
        AssetMarket("BTC", int(os.getenv("LIGHTER_BTC_MARKET_INDEX", "1")), default_price=float(os.getenv("LIGHTER_BTC_PRICE", "68500"))),
        AssetMarket("HYPE", int(os.getenv("LIGHTER_HYPE_MARKET_INDEX", "24")), default_price=float(os.getenv("LIGHTER_HYPE_PRICE", "20"))),
        AssetMarket("SOL", int(os.getenv("LIGHTER_SOL_MARKET_INDEX", "2")), default_price=float(os.getenv("LIGHTER_SOL_PRICE", "150"))),
    ]
