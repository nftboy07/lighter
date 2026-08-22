#!/usr/bin/env python3
"""
Lighter DEX News Catalyst & Manual Quick-Snipe Execution Engine
==============================================================
Features:
- Sub-5ms Regex NLP Catalyst Classifier for breaking news
- One-Tap / Text-Triggered Manual Max-Size Entries ('btc', 'eth', 'short eth')
- Automated Multi-Stage Take-Profit (TP) & Trailing Stop-Loss (SL) Engine
- zkLighter Mainnet Execution with Max Collateral Margin
- Instant Telegram Alerts & Remote Trade Management
"""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp
from dotenv import load_dotenv

from news_pipeline import NewsPipeline, NormalizedNewsEvent
from news_sources import NewsSourceRegistry, NewsSourceScheduler
from news_markets import MarketRegistry, TickerCache
from news_lifecycle import PaperFillSimulator, PositionBook, PositionClock, TradeIntentQueue
from news_observability import AuditLog, NewsMetrics
from lighter_news_risk import LighterNewsRiskGate, MarketSnapshot, live_execution_allowed
from treenews_ws import TreeNewsWebSocketClient
from cross_exchange_momentum import CrossExchangeMomentumFilter, MomentumConfirmation
from depth_vwap_engine import (
    DepthVWAPEngine,
    MicrostructureDepthBook,
    calculate_vwap,
    liquidity_adjusted_size,
    global_depth_vwap_engine,
)

load_dotenv(Path(__file__).with_name(".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
try:
    _file_handler = logging.FileHandler(Path(__file__).with_name("sniper_app.log"), encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(_file_handler)
except OSError:
    pass
logger = logging.getLogger("LighterNewsSniper")


def unpack_signer_result(result: Any) -> Tuple[Any, Optional[str]]:
    """SignerClient returns (tx, resp, err) today; older code expected (tx_hash, err)."""
    if result is None:
        return None, "empty signer result"
    if not isinstance(result, (tuple, list)):
        return result, None
    if len(result) >= 3:
        tx, resp, err = result[0], result[1], result[2]
        return (resp if resp is not None else tx), err
    if len(result) == 2:
        return result[0], result[1]
    return result[0], None


def signer_tx_id(resp: Any) -> str:
    if resp is None:
        return ""
    for attr in ("tx_hash", "hash", "txHash"):
        value = getattr(resp, attr, None)
        if value:
            return str(value)
    if isinstance(resp, dict):
        for key in ("tx_hash", "hash", "txHash"):
            if resp.get(key):
                return str(resp[key])
    return str(resp)


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class NewsItem:
    source: str
    headline: str
    body: str = ""
    timestamp: float = field(default_factory=time.time)
    url: str = ""


@dataclass
class CatalystSignal:
    news_id: str
    headline: str
    target_asset: str
    market_index: int
    sentiment: str  # "BULLISH" or "BEARISH"
    conviction_score: float
    matched_keywords: List[str]


@dataclass
class ActivePosition:
    position_id: str
    asset: str
    market_index: int
    side: str  # "BUY/LONG" or "SELL/SHORT"
    entry_price: float
    size_eth: float
    notional_usd: float
    tp_pct: float = 2.5
    sl_pct: float = 1.5
    highest_price: float = 0.0
    lowest_price: float = float("inf")
    entry_time: float = field(default_factory=time.time)
    is_active: bool = True
    tp_price: float = 0.0
    sl_price: float = 0.0
    ordered_size: float = 0.0
    exchange_tp: bool = False
    exchange_sl: bool = False
    max_hold_seconds: float = 2700.0
    trail_arm_pct: float = 1.5
    trail_gap_pct: float = 1.0
    tp_client_index: int = 0
    sl_client_index: int = 0
    tp_order_index: int = 0
    sl_order_index: int = 0
    exchange_sl_price: float = 0.0
    last_sl_amend_ts: float = 0.0
    pending_sl_amend: bool = False
    last_protect_attempt: float = 0.0
    original_size: float = 0.0
    tp_hits: int = 0
    atr_multiplier: float = 1.0
    volatility_expanded: bool = False


# =============================================================================
# CATALYST CLASSIFIER
# =============================================================================

class CatalystClassifier:
    """Classifies breaking crypto news with ultra-low latency regex rules."""

    CATALYSTS = [
        {
            "pattern": r"(?=.*\b(trump|donald\s*trump|potus)\b)(?=.*\b(hyperliquid|hype)\b)",
            "target": "HYPE",
            "market_index": 0,
            "sentiment": "BULLISH",
            "conviction": 0.98,
        },
        {
            "pattern": r"(?=.*\b(trump|donald\s*trump|white\s*house)\b)(?=.*\b(crypto|bitcoin|btc|ethereum|eth|reserve)\b)",
            "target": "ETH",
            "market_index": 0,
            "sentiment": "BULLISH",
            "conviction": 0.90,
        },
        {
            "pattern": r"(?=.*\b(sec|gary\s*gensler)\b)(?=.*\b(approv(ed|es|al)|etf|settlement)\b)",
            "target": "ETH",
            "market_index": 0,
            "sentiment": "BULLISH",
            "conviction": 0.95,
        },
        {
            "pattern": r"(?=.*\b(binance|coinbase|robinhood)\b)(?=.*\b(list(s|ing|ed)?|launch(es)?)\b)",
            "target": "ETH",
            "market_index": 0,
            "sentiment": "BULLISH",
            "conviction": 0.85,
        },
        {
            "pattern": r"(?=.*\b(hack|exploit|drained|stolen|breach|vulnerability)\b)(?=.*\b(bridge|dex|lighter|protocol|millions)\b)",
            "target": "ETH",
            "market_index": 0,
            "sentiment": "BEARISH",
            "conviction": 0.95,
        },
    ]

    def __init__(self, max_news_age_sec: float = 60.0, min_conviction: float = 0.0, fingerprint_window_sec: float = 900.0):
        self.max_news_age_sec = max_news_age_sec
        self.min_conviction = min_conviction
        self.fingerprint_window_sec = fingerprint_window_sec
        self.seen_headlines: set = set()
        self.story_fingerprints: List[Tuple[str, str, set, float]] = []
        self.compiled_rules = [
            (re.compile(c["pattern"], re.IGNORECASE), c) for c in self.CATALYSTS
        ]

    def _prune_fingerprints(self, now: float) -> None:
        cutoff = now - self.fingerprint_window_sec
        self.story_fingerprints = [fp for fp in self.story_fingerprints if fp[3] >= cutoff]

    def _extract_tokens(self, text: str) -> set:
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        stopwords = {
            "a", "an", "the", "in", "on", "at", "to", "for", "of", "with", "by", "from",
            "and", "or", "as", "is", "are", "was", "were", "it", "this", "that", "be", "has", "have"
        }
        return words - stopwords

    def is_duplicate_fingerprint(self, target_asset: str, sentiment: str, text: str, now: Optional[float] = None) -> bool:
        ts = now if now is not None else time.time()
        self._prune_fingerprints(ts)
        tokens = self._extract_tokens(text)
        for cached_target, cached_sentiment, cached_tokens, fp_ts in self.story_fingerprints:
            if cached_target.upper() == target_asset.upper() and cached_sentiment == sentiment:
                union = tokens | cached_tokens
                jaccard = len(tokens & cached_tokens) / len(union) if union else 1.0
                if jaccard >= 0.35 or len(tokens & cached_tokens) >= 3:
                    return True
        return False

    def process_news(self, news: NewsItem) -> Optional[CatalystSignal]:
        now = time.time()
        if now - news.timestamp > self.max_news_age_sec:
            return None

        clean_title = news.headline.strip().lower()
        if clean_title in self.seen_headlines:
            return None

        self._prune_fingerprints(now)
        full_text = f"{news.headline} {news.body}".strip()
        tokens = self._extract_tokens(full_text)

        for regex, rule in self.compiled_rules:
            if regex.search(full_text) and rule["conviction"] >= self.min_conviction:
                target = rule["target"]
                sentiment = rule["sentiment"]

                # Check story fingerprint cache (15-minute lockout)
                for cached_target, cached_sentiment, cached_tokens, fp_ts in self.story_fingerprints:
                    if cached_target.upper() == target.upper() and cached_sentiment == sentiment:
                        union = tokens | cached_tokens
                        jaccard = len(tokens & cached_tokens) / len(union) if union else 1.0
                        if jaccard >= 0.35 or len(tokens & cached_tokens) >= 3:
                            return None

                self.seen_headlines.add(clean_title)
                self.story_fingerprints.append((target, sentiment, tokens, now))
                return CatalystSignal(
                    news_id=f"cat_{int(now*1000)}",
                    headline=news.headline,
                    target_asset=rule["target"],
                    market_index=rule["market_index"],
                    sentiment=rule["sentiment"],
                    conviction_score=rule["conviction"],
                    matched_keywords=list(regex.findall(full_text)),
                )
        return None


# =============================================================================
# MAX-SIZE & TAKE-PROFIT EXECUTION ENGINE
# =============================================================================

class MaxSizeExecutionEngine:
    """Manages Max-Size orders, dynamic margin sizing, and Take-Profit watchdog."""

    def __init__(
        self,
        is_live: bool = False,
        max_margin_utilization_pct: float = 85.0,
        slippage_tolerance_pct: float = 0.5,
        default_tp_pct: float = 2.5,
        default_sl_pct: float = 1.5,
    ):
        self.is_live = is_live
        self.max_margin_utilization_pct = max_margin_utilization_pct
        self.slippage_tolerance_pct = slippage_tolerance_pct
        self.default_tp_pct = default_tp_pct
        self.default_sl_pct = default_sl_pct

        self.base_url = os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
        self.account_index = int(os.getenv("LIGHTER_ACCOUNT_INDEX", 737649))
        self.api_key_index = int(os.getenv("LIGHTER_API_KEY_INDEX", 5))
        self.api_private_key = os.getenv("LIGHTER_API_PRIVATE_KEY", "")

        self.signer_client = None
        self.active_positions: Dict[str, ActivePosition] = {}
        self.market_meta: Dict[str, Dict[str, Any]] = {}
        self.depth_engine = global_depth_vwap_engine
        from volatility_adaptive_exits import get_volatility_engine
        self.volatility_engine = get_volatility_engine()
        self._http: Optional[aiohttp.ClientSession] = None
        self._order_lock = asyncio.Lock()
        self._last_care_ts: float = 0.0
        self.clock = PositionClock(os.getenv("NEWS_DB_PATH", str(Path(__file__).with_name("lighter_news.db"))))

    async def _ensure_signer(self):
        if self.is_live and self.signer_client is None and self.api_private_key and self.account_index > 0:
            try:
                import lighter
                self.signer_client = lighter.SignerClient(
                    url=self.base_url,
                    account_index=self.account_index,
                    api_private_keys={self.api_key_index: self.api_private_key},
                )
                logger.info(f"⚡ [EXEC] SignerClient connected for Account #{self.account_index}")
            except Exception as e:
                logger.error("❌ [EXEC] SignerClient init error: %s: %s", type(e).__name__, e or "no message")

    async def _http_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8.0))
        return self._http

    def _select_subaccount(self, sub_accs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for acc in sub_accs:
            try:
                if int(acc.get("index", acc.get("account_index", -1))) == self.account_index:
                    return acc
            except (TypeError, ValueError):
                continue
        return None

    def _parse_collateral(self, acc: Dict[str, Any]) -> Optional[float]:
        for key in ("collateral", "available_balance", "available_collateral", "total_collateral", "balance"):
            value = acc.get(key)
            if value is None or value == "":
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    async def fetch_available_collateral_usd(self) -> Optional[float]:
        """Fetches the configured sub-account collateral with resilient caching."""
        wallet = os.getenv("WALLET_ADDRESS", "").strip()
        if not wallet:
            if self.is_live:
                logger.error("Live collateral query failed: WALLET_ADDRESS is not set")
                return None
            return float(os.getenv("NEWS_PAPER_COLLATERAL_USD", "100"))

        if not hasattr(self, "_last_cached_collateral"):
            self._last_cached_collateral = float(os.getenv("LIGHTER_FALLBACK_COLLATERAL", "5.5208"))
        try:
            session = await self._http_session()
            # 1. Primary: Direct query by subaccount index (fastest & most reliable)
            url_idx = f"{self.base_url}/api/v1/account?by=index&value={self.account_index}"
            async with session.get(url_idx, timeout=aiohttp.ClientTimeout(total=4.0)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    collat = self._parse_collateral(data)
                    if collat is not None and collat > 0:
                        self._last_cached_collateral = collat
                        return collat

            # 2. Secondary: Query by L1 Wallet address if index returned empty
            if wallet:
                url_wallet = f"{self.base_url}/api/v1/accountsByL1Address?l1_address={wallet}"
                async with session.get(url_wallet, timeout=aiohttp.ClientTimeout(total=4.0)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        sub_accs = data.get("sub_accounts") or data.get("accounts") or data.get("data") or []
                        if isinstance(sub_accs, dict):
                            sub_accs = sub_accs.get("sub_accounts") or [sub_accs]
                        acc = self._select_subaccount(list(sub_accs))
                        if acc is not None:
                            collat = self._parse_collateral(acc)
                            if collat is not None and collat > 0:
                                self._last_cached_collateral = collat
                                return collat

            # Return cached collateral if endpoint temporarily timed out
            return self._last_cached_collateral
        except Exception as e:
            logger.debug("Live collateral query using cached value ($%.2f): %s", self._last_cached_collateral, e)
            return self._last_cached_collateral
            logger.warning("Paper collateral fallback after API error: %s", detail)
            return float(os.getenv("NEWS_PAPER_COLLATERAL_USD", "100"))

    def _book_to_snapshot(self, asset: str, book: Dict[str, Any]) -> Optional[MarketSnapshot]:
        try:
            idx = int(book.get("market_id", book.get("market_index", -1)))
        except (TypeError, ValueError):
            return None
        last = float(book.get("last_trade_price") or book.get("mark_price") or book.get("index_price") or 0)
        mark = float(book.get("mark_price") or last)
        spread_bps = abs(mark - last) / last * 10_000.0 if last else 0.0
        size_decimals = self._int_or(book.get("size_decimals"), 4)
        price_decimals = self._int_or(book.get("price_decimals"), 2)
        min_base = float(book.get("min_base_amount") or 0.0)
        min_quote = float(book.get("min_quote_amount") or 0.0)
        self.market_meta[asset.upper()] = {
            "market_index": idx,
            "size_decimals": size_decimals,
            "price_decimals": price_decimals,
            "min_base_amount": min_base,
            "min_quote_amount": min_quote,
        }
        if last <= 0:
            return None
        return MarketSnapshot(
            asset.upper(),
            last,
            spread_bps=spread_bps,
            timestamp=time.time(),
            size_decimals=size_decimals,
            price_decimals=price_decimals,
            min_base_amount=min_base,
            market_index=idx,
        )

    async def fetch_order_catalog(self) -> List[Dict[str, Any]]:
        try:
            session = await self._http_session()
            async with session.get(f"{self.base_url}/api/v1/orderBookDetails") as resp:
                if resp.status != 200:
                    logger.warning("orderBookDetails HTTP %s", resp.status)
                    return []
                data = await resp.json(content_type=None)
            books = data.get("order_book_details") or data.get("order_books") or data.get("data") or []
            if isinstance(books, dict):
                books = books.get("order_book_details") or [books]
            books = list(books)
            for book in books:
                symbol = str(book.get("symbol") or "").upper()
                if symbol:
                    self._book_to_snapshot(symbol, book)
            return books
        except Exception as e:
            logger.warning("Market catalog fetch failed: %s: %s", type(e).__name__, e or "no message")
            return []

    async def fetch_market_snapshot(self, asset: str, market_index: int) -> Optional[MarketSnapshot]:
        books = await self.fetch_order_catalog()
        snapshots = self.snapshots_from_catalog(books, [(asset, market_index)])
        return snapshots.get(asset.upper())

    async def fetch_orderbook_depth(self, market_index: int) -> MicrostructureDepthBook:
        """Fetches live L2 depth orderbook from Lighter API and updates in-memory engine."""
        try:
            session = await self._http_session()
            url = f"{self.base_url}/api/v1/orderBookOrders?market_id={market_index}&limit=50"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    bids = data.get("bids") or []
                    asks = data.get("asks") or []
                    return self.depth_engine.update_from_raw_l2(market_index, bids, asks)
        except Exception as e:
            logger.debug("fetch_orderbook_depth error: %s", e)
        return self.depth_engine.get_or_create_book(market_index)

    def snapshots_from_catalog(self, books: List[Dict[str, Any]], wanted: List[Tuple[str, int]]) -> Dict[str, MarketSnapshot]:
        by_symbol: Dict[str, Dict[str, Any]] = {}
        by_id: Dict[int, Dict[str, Any]] = {}
        for book in books:
            symbol = str(book.get("symbol", "")).upper()
            if symbol:
                by_symbol[symbol] = book
            try:
                by_id[int(book.get("market_id", book.get("market_index", -1)))] = book
            except (TypeError, ValueError):
                continue
        out: Dict[str, MarketSnapshot] = {}
        for asset, market_index in wanted:
            book = by_symbol.get(asset.upper()) or by_id.get(market_index)
            if not book:
                continue
            snapshot = self._book_to_snapshot(asset, book)
            if snapshot:
                out[asset.upper()] = snapshot
        return out

    def calculate_max_order_size(
        self,
        collateral_usd: float,
        current_price_usd: float,
        conviction: Optional[float] = None,
        margin_utilization_pct: Optional[float] = None,
    ) -> float:
        if margin_utilization_pct is not None:
            utilization = margin_utilization_pct
        elif conviction is not None:
            from trade_exits import dynamic_kelly_margin
            utilization = dynamic_kelly_margin(conviction)
        else:
            utilization = self.max_margin_utilization_pct
        usable_usd = collateral_usd * (utilization / 100.0)
        size_eth = usable_usd / max(1.0, current_price_usd)
        return round(size_eth, 8)

    def _int_or(self, value: Any, default: int) -> int:
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _meta(self, asset: str) -> Dict[str, Any]:
        defaults = {"size_decimals": 4, "price_decimals": 2, "min_base_amount": 0.0, "market_index": 0}
        merged = dict(defaults)
        merged.update(self.market_meta.get(asset.upper(), {}))
        return merged

    def ensure_exit_prices(self, pos: ActivePosition) -> None:
        """Every position always has local TP and SL prices for the watchdog with volatility adaptation."""
        from trade_exits import policy_for, tp_sl_prices, scale_tp_price
        from volatility_adaptive_exits import get_volatility_engine

        engine = getattr(self, "volatility_engine", None) or get_volatility_engine()
        vol_state = engine.get_state(pos.asset) if engine else None
        mult = getattr(pos, "atr_multiplier", 1.0) or 1.0
        if mult == 1.0 and vol_state and vol_state.atr_multiplier > 1.0:
            mult = vol_state.atr_multiplier
            pos.atr_multiplier = mult
            pos.volatility_expanded = vol_state.is_violent_catalyst

        policy = policy_for(pos.asset, override_tp=pos.tp_pct or None, override_sl=pos.sl_pct or None, atr_multiplier=mult)
        pos.tp_pct = pos.tp_pct or policy.tp_pct
        pos.sl_pct = pos.sl_pct or policy.sl_pct
        pos.max_hold_seconds = pos.max_hold_seconds or policy.max_hold_seconds
        pos.trail_arm_pct = pos.trail_arm_pct or policy.trail_arm_pct
        pos.trail_gap_pct = policy.trail_gap_pct if (pos.volatility_expanded or not pos.trail_gap_pct) else pos.trail_gap_pct
        if pos.entry_price <= 0:
            return
        tp, sl = tp_sl_prices(pos.side, pos.entry_price, policy)
        if not pos.original_size:
            pos.original_size = pos.size_eth or pos.ordered_size
        nxt = (pos.tp_hits or 0) + 1
        if nxt <= 2:
            pos.tp_price = scale_tp_price(pos.side, pos.entry_price, policy, nxt, atr_multiplier=mult)
        else:
            pos.tp_price = 0.0
            pos.trail_gap_pct = 1.0
        if not pos.sl_price:
            pos.sl_price = sl

    async def _refresh_nonce(self) -> None:
        manager = getattr(self.signer_client, "nonce_manager", None)
        if manager is None:
            return
        try:
            await manager.async_hard_refresh_nonce(self.api_key_index)
        except TypeError:
            await manager.async_hard_refresh_nonce()
        except Exception as e:
            logger.warning("Nonce refresh failed: %s", e)

    async def _submit_live_order(
        self,
        asset: str,
        market_index: int,
        size: float,
        price: float,
        is_ask: bool,
        reduce_only: bool = False,
    ) -> Tuple[Optional[str], Optional[str]]:
        await self._ensure_signer()
        if not self.signer_client:
            return None, "SignerClient unavailable"
        async with self._order_lock:
            return await self._submit_live_order_locked(asset, market_index, size, price, is_ask, reduce_only)

    async def _submit_live_order_locked(
        self,
        asset: str,
        market_index: int,
        size: float,
        price: float,
        is_ask: bool,
        reduce_only: bool = False,
    ) -> Tuple[Optional[str], Optional[str]]:
        meta = self._meta(asset)
        size_decimals = self._int_or(meta.get("size_decimals"), 4)
        price_decimals = self._int_or(meta.get("price_decimals"), 2)
        size_int = int(round(size * (10 ** size_decimals)))
        price_int = int(round(price * (10 ** price_decimals)))
        if size_int <= 0:
            return None, f"size {size} rounds to 0 with {size_decimals} decimals"
        last_err = "order failed"
        for attempt in range(2):
            client_order_index = int(time.time() * 1000) % 100_000_000
            try:
                if hasattr(self.signer_client, "create_market_order"):
                    result = await self.signer_client.create_market_order(
                        market_index=market_index,
                        client_order_index=client_order_index,
                        base_amount=size_int,
                        avg_execution_price=price_int,
                        is_ask=is_ask,
                        reduce_only=reduce_only,
                    )
                else:
                    result = await self.signer_client.create_order(
                        market_index=market_index,
                        client_order_index=client_order_index,
                        base_amount=size_int,
                        price=price_int,
                        is_ask=is_ask,
                        order_type=getattr(self.signer_client, "ORDER_TYPE_MARKET", 1),
                        time_in_force=getattr(self.signer_client, "ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", 1),
                    )
            except Exception as e:
                last_err = f"{type(e).__name__}: {e or 'no message'}"
                if "invalid nonce" in last_err.lower() and attempt == 0:
                    await self._refresh_nonce()
                    await asyncio.sleep(0.2)
                    continue
                return None, last_err
            resp, err = unpack_signer_result(result)
            if err:
                last_err = str(err)
                if "invalid nonce" in last_err.lower() and attempt == 0:
                    await self._refresh_nonce()
                    await asyncio.sleep(0.2)
                    continue
                return None, last_err
            return signer_tx_id(resp), None
        return None, last_err

    def parse_account_position(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Lighter sends abs `position` plus `sign` (1 long / -1 short). market_id 0 is ETH."""
        if not isinstance(item, dict):
            return None
        try:
            size = float(item.get("position") or item.get("size") or item.get("base_amount") or 0)
        except (TypeError, ValueError):
            return None
        if abs(size) <= 0:
            return None
        sign_raw = item.get("sign")
        try:
            sign = int(sign_raw) if sign_raw is not None and sign_raw != "" else (1 if size > 0 else -1)
        except (TypeError, ValueError):
            sign = 1 if size > 0 else -1
        raw_mid = item.get("market_id")
        if raw_mid is None:
            raw_mid = item.get("market_index")
        try:
            market_id = int(raw_mid) if raw_mid is not None and raw_mid != "" else -1
        except (TypeError, ValueError):
            market_id = -1
        symbol = str(item.get("symbol") or item.get("market_symbol") or "").upper()
        try:
            entry = float(item.get("avg_entry_price") or item.get("entry_price") or item.get("avg_price") or 0)
        except (TypeError, ValueError):
            entry = 0.0
        try:
            open_n = int(item.get("open_order_count") or item.get("position_tied_order_count") or item.get("pending_order_count") or 0)
        except (TypeError, ValueError):
            open_n = 0
        return {
            "symbol": symbol,
            "market_index": market_id,
            "size": abs(size),
            "signed": abs(size) * (1 if sign >= 0 else -1),
            "side": "BUY/LONG" if sign >= 0 else "SELL/SHORT",
            "entry_price": entry,
            "open_order_count": open_n,
        }

    async def fetch_account_positions(self) -> List[Dict[str, Any]]:
        try:
            session = await self._http_session()
            url = f"{self.base_url}/api/v1/account?by=index&value={self.account_index}&active_only=true"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.warning("Account position fetch failed: %s", e)
            return []
        accounts = data.get("accounts") or data.get("data") or ([data] if isinstance(data, dict) else [])
        positions: List[Dict[str, Any]] = []
        for acc in accounts if isinstance(accounts, list) else [accounts]:
            if not isinstance(acc, dict):
                continue
            raw = acc.get("positions") or acc.get("position") or []
            if isinstance(raw, dict):
                raw = list(raw.values())
            for item in raw:
                parsed = self.parse_account_position(item)
                if parsed:
                    positions.append(parsed)
        return positions

    async def sync_and_adopt_all_live_positions(self) -> Dict[str, float]:
        """Automatically adopts all open on-chain positions from zkLighter into the TP/SL watchdog."""
        prices: Dict[str, float] = {}
        try:
            live = await self.fetch_account_positions()
            books = await self.fetch_order_catalog()
            catalog_prices: Dict[str, float] = {}
            for b in books:
                sym = str(b.get("symbol", "")).upper()
                last_p = float(b.get("last_trade_price") or b.get("mark_price") or b.get("index_price") or 0)
                if sym and last_p > 0:
                    catalog_prices[sym] = last_p

            for item in live:
                sym = str(item.get("symbol") or "").upper()
                size = float(item.get("size") or 0)
                if not sym or size <= 0:
                    continue
                side = item.get("side") or "BUY/LONG"
                entry = float(item.get("entry_price") or 0)
                mkt_idx = int(item.get("market_index") or 0)
                mark = catalog_prices.get(sym, entry)
                if mark > 0:
                    prices[sym] = mark
                open_n = int(item.get("open_order_count") or item.get("position_tied_order_count") or 0)

                pos = self.existing_position(sym)
                if pos is None or not pos.is_active:
                    from trade_exits import policy_for
                    policy = policy_for(sym)
                    pos = ActivePosition(
                        position_id=f"adopted_{sym}_{mkt_idx}",
                        asset=sym,
                        market_index=mkt_idx,
                        side=side,
                        entry_price=entry if entry > 0 else (mark if mark > 0 else 1.0),
                        size_eth=size,
                        notional_usd=size * (entry if entry > 0 else mark),
                        tp_pct=policy.tp_pct or self.default_tp_pct or 2.5,
                        sl_pct=policy.sl_pct or 1.5,
                        is_active=True,
                    )
                    self.ensure_exit_prices(pos)
                    self.active_positions[pos.position_id] = pos
                    logger.info(
                        "🛡️ [ADOPTED POSITION] %s %s Size=%s @ $%.4f | TP: $%.4f (+%.1f%%) | SL: $%.4f (-%.1f%%)",
                        sym, side, size, pos.entry_price, pos.tp_price, pos.tp_pct, pos.sl_price, pos.sl_pct
                    )
                else:
                    pos.size_eth = size
                    self.ensure_exit_prices(pos)

                if open_n < 2 and self.is_live:
                    asyncio.create_task(self.sync_position_orders(pos, open_n))
        except Exception as e:
            logger.debug(f"Position adoption sync error: {e}")
        return prices

    def match_exchange_position(self, positions: List[Dict[str, Any]], asset: str, market_index: int = -1) -> Optional[Dict[str, Any]]:
        want = (asset or "").upper()
        if want:
            for item in positions:
                if item.get("symbol") == want:
                    return item
            return None
        for item in positions:
            if market_index not in (None, -1) and item.get("market_index") == market_index:
                return item
        return None

    async def wait_for_exchange_position(self, asset: str, market_index: int, timeout: float = 20.0) -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.match_exchange_position(await self.fetch_account_positions(), asset, market_index)
            if found:
                return found
            await asyncio.sleep(0.8)
        return None

    async def fetch_spread_bps(self, market_index: int) -> float:
        try:
            session = await self._http_session()
            url = f"{self.base_url}/api/v1/orderBookOrders?market_id={market_index}&limit=1"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return 0.0
                data = await resp.json(content_type=None)
            bids = data.get("bids") or []
            asks = data.get("asks") or []
            if not bids or not asks:
                return 0.0
            bid = float(bids[0].get("price") if isinstance(bids[0], dict) else bids[0][0])
            ask = float(asks[0].get("price") if isinstance(asks[0], dict) else asks[0][0])
            mid = (bid + ask) / 2.0
            if mid <= 0:
                return 0.0
            return abs(ask - bid) / mid * 10_000.0
        except Exception:
            return 0.0

    def _extract_orders(self, data: Any) -> List[Dict[str, Any]]:
        orders: List[Dict[str, Any]] = []
        if data is None:
            return orders
        if isinstance(data, list):
            blobs = data
        elif isinstance(data, dict):
            blobs = data.get("orders") or data.get("open_orders") or data.get("pending_orders") or data.get("data") or data.get("accounts") or [data]
            if isinstance(blobs, dict):
                blobs = list(blobs.values())
        else:
            return orders
        for item in blobs:
            if not isinstance(item, dict):
                continue
            nested = item.get("orders") or item.get("open_orders") or item.get("pending_orders")
            if isinstance(nested, list):
                orders.extend(x for x in nested if isinstance(x, dict))
            elif isinstance(nested, dict):
                orders.extend(x for x in nested.values() if isinstance(x, dict))
            elif item.get("order_index") is not None or item.get("client_order_index") is not None or item.get("price") is not None:
                orders.append(item)
        return orders

    async def fetch_open_orders(self) -> List[Dict[str, Any]]:
        orders: List[Dict[str, Any]] = []
        try:
            session = await self._http_session()
            url = f"{self.base_url}/api/v1/account?by=index&value={self.account_index}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    orders.extend(self._extract_orders(await resp.json(content_type=None)))
        except Exception:
            pass
        try:
            session = await self._http_session()
            url = f"{self.base_url}/api/v1/accountActiveOrders?account_index={self.account_index}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    orders.extend(self._extract_orders(await resp.json(content_type=None)))
        except Exception:
            pass
        seen = set()
        uniq: List[Dict[str, Any]] = []
        for item in orders:
            key = (
                self._order_int(item, "market_id", "market_index"),
                self._order_int(item, "order_index", "order_id", "index", "client_order_index"),
            )
            if key in seen:
                continue
            seen.add(key)
            uniq.append(item)
        return uniq

    def existing_position(self, asset: str, market_index: Optional[int] = None) -> Optional[ActivePosition]:
        want = (asset or "").upper()
        for pos in self.active_positions.values():
            if not pos.is_active:
                continue
            if pos.asset.upper() == want:
                return pos
            # Market 0 is ETH's default; only treat a real catalog id as a duplicate slot.
            if market_index not in (None, 0, -1) and pos.market_index == market_index:
                return pos
        return None

    def _protect_fns(self):
        # Market trigger first (no extra GTT margin); limit GTT as fallback.
        tp_fns = [fn for fn in (
            getattr(self.signer_client, "create_tp_order", None),
            getattr(self.signer_client, "create_tp_limit_order", None),
        ) if fn]
        sl_fns = [fn for fn in (
            getattr(self.signer_client, "create_sl_order", None),
            getattr(self.signer_client, "create_sl_limit_order", None),
        ) if fn]
        return tp_fns, sl_fns

    def _order_int(self, item: Dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = item.get(key)
            if value is None or value == "":
                continue
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
        return 0

    async def verify_protective_exits(self, pos: ActivePosition, timeout: float = 6.0) -> Dict[str, Any]:
        deadline = time.time() + timeout
        found_tp = False
        found_sl = False
        while time.time() < deadline:
            orders = await self.fetch_open_orders()
            for item in orders:
                market_id = self._order_int(item, "market_id", "market_index")
                if market_id != pos.market_index:
                    continue
                client_idx = self._order_int(item, "client_order_index", "client_order_id")
                order_idx = self._order_int(item, "order_index", "order_id", "index")
                if pos.tp_client_index and client_idx == pos.tp_client_index:
                    found_tp = True
                    if order_idx:
                        pos.tp_order_index = order_idx
                elif pos.sl_client_index and client_idx == pos.sl_client_index:
                    found_sl = True
                    if order_idx:
                        pos.sl_order_index = order_idx
            if found_tp or found_sl:
                break
            await asyncio.sleep(0.5)
        pos.exchange_tp = found_tp or pos.exchange_tp
        pos.exchange_sl = found_sl or pos.exchange_sl
        return {"tp": pos.exchange_tp, "sl": pos.exchange_sl, "tp_idx": pos.tp_order_index, "sl_idx": pos.sl_order_index}

    async def place_protective_exits(self, pos: ActivePosition, tp_price: float, sl_price: float) -> Dict[str, Any]:
        from trade_exits import protect_limit_price

        self.ensure_exit_prices(pos)
        tp_price = pos.tp_price or tp_price
        sl_price = pos.sl_price or sl_price
        status = {"tp": False, "sl": False, "detail": "local watchdog only", "on_book": False}
        if self.is_live and self.signer_client is None:
            await self._ensure_signer()
        if not self.is_live or not self.signer_client:
            status["detail"] = "local watchdog TP/SL armed"
            return status
        live = self.match_exchange_position(await self.fetch_account_positions(), pos.asset, pos.market_index)
        if live:
            pos.size_eth = float(live.get("size") or pos.size_eth)
            if live.get("market_index") not in (None, -1):
                pos.market_index = int(live["market_index"])
        try:
            await self.fetch_order_catalog()
        except Exception:
            pass
        is_ask = pos.side == "BUY/LONG"
        meta = self._meta(pos.asset)
        price_decimals = self._int_or(meta.get("price_decimals"), 2)
        size_decimals = self._int_or(meta.get("size_decimals"), 4)
        from trade_exits import partial_qty
        if not pos.original_size:
            pos.original_size = pos.size_eth or pos.ordered_size
        level = min(4, int(pos.tp_hits or 0) + 1)
        tp_qty = partial_qty(pos.original_size, pos.size_eth, level)
        sl_qty = pos.size_eth
        tp_size_int = int(tp_qty * (10 ** size_decimals))
        sl_size_int = int(sl_qty * (10 ** size_decimals))  # floor; size_decimals 0 is valid (XRP)
        size_int = sl_size_int
        if sl_size_int <= 0:
            status["detail"] = "local watchdog TP/SL armed (size too small for exchange)"
            logger.info("No exchange TP/SL for %s size=%s — watchdog will exit at TP/SL", pos.asset, pos.size_eth)
            return status
        tp_trigger = int(round(tp_price * (10 ** price_decimals)))
        sl_trigger = int(round(sl_price * (10 ** price_decimals)))
        tp_limit = int(round(protect_limit_price(pos.side, "tp", tp_price) * (10 ** price_decimals)))
        sl_limit = int(round(protect_limit_price(pos.side, "sl", sl_price) * (10 ** price_decimals)))
        logger.info(
            "Placing TP/SL %s mkt=%s tp_qty_int=%s sl_qty_int=%s lvl=%s px_dec=%s sz_dec=%s tp_trig=%s sl_trig=%s",
            pos.asset, pos.market_index, tp_size_int, sl_size_int, level, price_decimals, size_decimals, tp_trigger, sl_trigger,
        )
        tp_fns, sl_fns = self._protect_fns()
        if not pos.tp_client_index:
            pos.tp_client_index = int(time.time() * 1000) % 100_000_000
        if not pos.sl_client_index:
            pos.sl_client_index = (pos.tp_client_index + 7) % 100_000_000
        async with self._order_lock:
            try:
                last_tp_err = last_sl_err = None
                for fn in tp_fns:
                    if status["tp"] or tp_size_int <= 0:
                        break
                    result = await fn(
                        market_index=pos.market_index,
                        client_order_index=pos.tp_client_index,
                        base_amount=tp_size_int,
                        trigger_price=tp_trigger,
                        price=tp_limit if "limit" in getattr(fn, "__name__", "") else tp_trigger,
                        is_ask=is_ask,
                        reduce_only=True,
                    )
                    _, err = unpack_signer_result(result)
                    status["tp"] = err is None
                    if err:
                        last_tp_err = err
                        logger.warning("Exchange TP attach failed (%s): %s", getattr(fn, "__name__", fn), err)
                        if "invalid nonce" in str(err).lower():
                            await self._refresh_nonce()
                        pos.tp_client_index = (pos.tp_client_index + 11) % 100_000_000
                        await asyncio.sleep(0.25)
                await asyncio.sleep(0.3)
                for fn in sl_fns:
                    if status["sl"]:
                        break
                    result = await fn(
                        market_index=pos.market_index,
                        client_order_index=pos.sl_client_index,
                        base_amount=sl_size_int,
                        trigger_price=sl_trigger,
                        price=sl_limit if "limit" in getattr(fn, "__name__", "") else sl_trigger,
                        is_ask=is_ask,
                        reduce_only=True,
                    )
                    _, err = unpack_signer_result(result)
                    status["sl"] = err is None
                    if err:
                        last_sl_err = err
                        logger.warning("Exchange SL attach failed (%s): %s", getattr(fn, "__name__", fn), err)
                        if "invalid nonce" in str(err).lower():
                            await self._refresh_nonce()
                        pos.sl_client_index = (pos.sl_client_index + 11) % 100_000_000
                        await asyncio.sleep(0.25)
                if not status["tp"] and last_tp_err:
                    logger.warning("XRP-scale debug %s last TP err=%s trig=%s size=%s", pos.asset, last_tp_err, tp_trigger, size_int)
                pos.exchange_tp, pos.exchange_sl = status["tp"], status["sl"]
                pos.exchange_sl_price = sl_price if status["sl"] else pos.exchange_sl_price
            except Exception as e:
                logger.warning("Exchange TP/SL attach error: %s", e)
                status["detail"] = f"TP/SL error: {e}"
                return status
        verified = await self.verify_protective_exits(pos)
        status["tp"] = bool(verified.get("tp") or status["tp"])
        status["sl"] = bool(verified.get("sl") or status["sl"])
        status["on_book"] = bool(status["tp"] or status["sl"])
        pos.exchange_tp, pos.exchange_sl = status["tp"], status["sl"]
        if status["tp"] and status["sl"]:
            status["detail"] = "exchange TP/SL on book"
        elif status["tp"] or status["sl"]:
            status["detail"] = "partial exchange protect; local watchdog backup"
        else:
            status["detail"] = "TP/SL FAILED — watchdog only"
        return status

    async def amend_trailing_sl(self, pos: ActivePosition) -> bool:
        if self.is_live and self.signer_client is None:
            await self._ensure_signer()
        if not self.is_live or not self.signer_client or not pos.sl_price:
            pos.pending_sl_amend = False
            return False
        now = time.time()
        if now - pos.last_sl_amend_ts < 15:
            return False
        if pos.exchange_sl_price and pos.entry_price:
            moved = abs(pos.sl_price - pos.exchange_sl_price) / pos.entry_price
            if moved < 0.0008:
                pos.pending_sl_amend = False
                return False
        from trade_exits import protect_limit_price

        meta = self._meta(pos.asset)
        price_decimals = self._int_or(meta.get("price_decimals"), 2)
        size_decimals = self._int_or(meta.get("size_decimals"), 4)
        size_int = int(pos.size_eth * (10 ** size_decimals))
        sl_trigger = int(round(pos.sl_price * (10 ** price_decimals)))
        sl_limit = int(round(protect_limit_price(pos.side, "sl", pos.sl_price) * (10 ** price_decimals)))
        _, sl_fns = self._protect_fns()
        sl_fn = sl_fns[0] if sl_fns else None
        if not sl_fn or size_int <= 0:
            pos.pending_sl_amend = False
            return False
        async with self._order_lock:
            try:
                if pos.sl_order_index and hasattr(self.signer_client, "cancel_order"):
                    result = await self.signer_client.cancel_order(pos.market_index, pos.sl_order_index)
                    _, err = unpack_signer_result(result)
                    if err:
                        logger.warning("Trail SL cancel failed: %s", err)
                        if "invalid nonce" in str(err).lower():
                            await self._refresh_nonce()
                        return False
                    await asyncio.sleep(0.3)
                pos.sl_client_index = int(time.time() * 1000) % 100_000_000
                result = await sl_fn(
                    market_index=pos.market_index,
                    client_order_index=pos.sl_client_index,
                    base_amount=size_int,
                    trigger_price=sl_trigger,
                    price=sl_limit,
                    is_ask=(pos.side == "BUY/LONG"),
                    reduce_only=True,
                )
                _, err = unpack_signer_result(result)
                if err:
                    logger.warning("Trail SL replace failed: %s", err)
                    return False
            except Exception as e:
                logger.warning("Trail SL amend error: %s", e)
                return False
        pos.last_sl_amend_ts = now
        pos.exchange_sl_price = pos.sl_price
        pos.pending_sl_amend = False
        verified = await self.verify_protective_exits(pos, timeout=4.0)
        pos.exchange_sl = bool(verified.get("sl"))
        logger.info("Trail SL amended for %s -> %s on_book=%s", pos.asset, pos.sl_price, pos.exchange_sl)
        return True

    async def execute_trade(
        self,
        asset: str = "ETH",
        market_index: int = 0,
        is_ask: bool = False,
        current_market_price: float = 2650.0,
        custom_tp_pct: Optional[float] = None,
        reason: str = "MANUAL_ENTRY",
        notional_usd: Optional[float] = None,
        conviction: Optional[float] = None,
        margin_utilization_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Executes a sized trade and registers Take-Profit watchdog. Live is fail-closed."""
        from trade_exits import policy_for, tp_sl_prices

        existing = self.existing_position(asset, market_index)
        if existing:
            return {"success": False, "error": f"{asset} already has an open {existing.side} position"}
        collateral_usd = await self.fetch_available_collateral_usd()
        if collateral_usd is None:
            return {"success": False, "error": "live collateral query failed"}
        if notional_usd is not None:
            order_size = float(notional_usd) / max(1.0, current_market_price)
        else:
            order_size = self.calculate_max_order_size(
                collateral_usd,
                current_market_price,
                conviction=conviction,
                margin_utilization_pct=margin_utilization_pct,
            )
        meta = self._meta(asset)
        min_base = float(meta.get("min_base_amount") or 0.0)
        if min_base and order_size < min_base:
            min_notional = min_base * current_market_price
            budget = float(notional_usd) if notional_usd is not None else float(os.getenv("NEWS_MAX_TRADE_USD", "25"))
            return {
                "success": False,
                "error": f"exchange min {min_base} {asset} is ${min_notional:.2f}, above risk budget ${budget:.2f}",
            }
        size_decimals = self._int_or(meta.get("size_decimals"), 4)
        order_size = round(order_size, size_decimals) if size_decimals > 0 else float(int(order_size))
        if order_size <= 0:
            return {"success": False, "error": "order size is zero"}
        if meta.get("market_index") not in (None, 0) or asset.upper() != "ETH":
            market_index = int(meta.get("market_index") or market_index)
        side_str = "SELL/SHORT" if is_ask else "BUY/LONG"
        policy = policy_for(asset, override_tp=custom_tp_pct, override_sl=self.default_sl_pct if custom_tp_pct else None)
        spread = await self.fetch_spread_bps(market_index) if self.is_live else 0.0
        if spread > policy.max_spread_bps:
            return {"success": False, "error": f"spread {spread:.1f} bps above {policy.max_spread_bps:.0f} bps exit cap"}

        # Microstructure Depth & VWAP Sizing Guard
        book = self.depth_engine.get_or_create_book(market_index, symbol=asset)
        if self.is_live and (not book.sorted_bid_prices or not book.sorted_ask_prices):
            book = await self.fetch_orderbook_depth(market_index)

        max_slip_bps = getattr(policy, "max_slippage_bps", 50.0) if hasattr(policy, "max_slippage_bps") else 50.0
        requested_notional = float(notional_usd) if notional_usd is not None else (order_size * current_market_price)

        if book.sorted_ask_prices or book.sorted_bid_prices:
            adj_notional = liquidity_adjusted_size(
                orderbook=book,
                side="SELL" if is_ask else "BUY",
                requested_usd=requested_notional,
                max_slippage_bps=max_slip_bps,
                fallback_price=current_market_price,
            )
            vwap_price, filled_usd, expected_slippage_bps, depth_exhausted = calculate_vwap(
                orderbook=book,
                side="SELL" if is_ask else "BUY",
                target_notional_usd=adj_notional,
                fallback_price=current_market_price,
            )
            if adj_notional < requested_notional:
                order_size = adj_notional / max(1.0, current_market_price)
                order_size = round(order_size, size_decimals) if size_decimals > 0 else float(int(order_size))
                logger.info(
                    f"⚠️ [VWAP SIZING] Adjusted size: ${requested_notional:.2f} -> ${adj_notional:.2f} "
                    f"(VWAP: ${vwap_price:.2f}, Slip: {expected_slippage_bps:.1f} bps, Cap: {max_slip_bps:.0f} bps)"
                )
        else:
            vwap_price = current_market_price
            expected_slippage_bps = 0.0
            depth_exhausted = False

        if order_size <= 0:
            return {"success": False, "error": "liquidity adjusted order size is zero"}

        slippage_mult = (1.0 - self.slippage_tolerance_pct / 100.0) if is_ask else (1.0 + self.slippage_tolerance_pct / 100.0)
        exec_price = current_market_price * slippage_mult

        from trade_exits import dynamic_kelly_margin
        effective_margin = margin_utilization_pct if margin_utilization_pct is not None else (
            dynamic_kelly_margin(conviction) if conviction is not None else self.max_margin_utilization_pct
        )
        logger.info(
            f"🚀 [MAX-SIZE EXECUTION] {side_str} {order_size} {asset} (@ ~${current_market_price:.2f} | VWAP: ${vwap_price:.2f}) | "
            f"Margin: {effective_margin:.1f}% (${collateral_usd * (effective_margin / 100.0):.2f}) | Reason: {reason}"
        )

        pos_id = f"pos_{int(time.time() * 1000)}"
        position = ActivePosition(
            position_id=pos_id,
            asset=asset,
            market_index=market_index,
            side=side_str,
            entry_price=current_market_price,
            size_eth=order_size,
            notional_usd=order_size * current_market_price,
            tp_pct=policy.tp_pct,
            sl_pct=policy.sl_pct,
            highest_price=current_market_price,
            lowest_price=current_market_price,
            ordered_size=order_size,
            original_size=order_size,
            max_hold_seconds=policy.max_hold_seconds,
            trail_arm_pct=policy.trail_arm_pct,
            trail_gap_pct=policy.trail_gap_pct,
        )

        if not self.is_live:
            self.ensure_exit_prices(position)
            position.entry_time = self.clock.remember(asset, position.entry_time)
            self.active_positions[pos_id] = position
            from trade_exits import tp_ladder_prices
            return {
                "success": True,
                "mode": "PAPER_SIMULATION",
                "asset": asset,
                "position_id": pos_id,
                "side": side_str,
                "size_eth": order_size,
                "entry_price": current_market_price,
                "vwap_price": vwap_price,
                "expected_slippage_bps": expected_slippage_bps,
                "depth_exhausted": depth_exhausted,
                "notional_usd": order_size * current_market_price,
                "tp_target_price": position.tp_price,
                "sl_price": position.sl_price,
                "tp_pct": policy.tp_pct,
                "sl_pct": policy.sl_pct,
                "tp_ladder": list(tp_ladder_prices(side_str, current_market_price, policy)),
                "max_hold_seconds": policy.max_hold_seconds,
                "protect": "paper local TP1-4 scale-out",
                "reason": reason,
            }

        tx_hash, err = await self._submit_live_order(asset, market_index, order_size, exec_price, is_ask)
        if err:
            logger.error("❌ [EXEC] Order rejected: %s", err)
            return {"success": False, "error": str(err)}

        filled = await self.wait_for_exchange_position(asset, market_index, timeout=20.0)
        if not filled:
            logger.error("❌ [EXEC] Order sent but no exchange position for %s", asset)
            return {"success": False, "error": "order submitted but fill not confirmed on exchange", "tx_hash": str(tx_hash)}

        filled_size = float(filled.get("size") or 0.0)
        if filled_size <= 0:
            filled_size = order_size
        if filled_size + 1e-12 < order_size:
            logger.warning("Partial fill %s: filled=%s ordered=%s — TP/SL sized to fill", asset, filled_size, order_size)
        position.size_eth = filled_size
        position.ordered_size = order_size
        position.original_size = filled_size
        position.entry_price = float(filled.get("entry_price") or current_market_price)
        position.notional_usd = position.size_eth * position.entry_price
        self.ensure_exit_prices(position)
        tp_price, sl_price = position.tp_price, position.sl_price
        protect = await self.place_protective_exits(position, tp_price, sl_price)
        position.entry_time = self.clock.remember(asset, position.entry_time)
        from trade_exits import already_through_exit, infer_tp_hits, scaled_out_qty, tp_ladder_prices
        mark = current_market_price
        itm = already_through_exit(side_str, mark, tp_price, sl_price)
        hits = infer_tp_hits(side_str, position.entry_price, mark, policy)
        self.active_positions[pos_id] = position
        if itm == "STOP_LOSS":
            logger.info("Fill already through STOP_LOSS on %s @ %s — closing now", asset, mark)
            await self.cancel_open_orders(position.market_index, [position.tp_order_index, position.sl_order_index])
            closed = await self.close_position(position, mark)
            position.is_active = bool(not closed)
            protect["detail"] = "closed immediately (STOP_LOSS)"
            protect["itm"] = itm
        elif hits >= 1:
            qty = scaled_out_qty(position.original_size, position.size_eth, hits)
            logger.info("Fill already through TP%s on %s @ %s — scaling out qty=%s", hits, asset, mark, qty)
            await self.cancel_open_orders(position.market_index, [position.tp_order_index, position.sl_order_index])
            closed = await self.close_position(position, mark, qty=(None if hits >= 4 else qty))
            if hits >= 4:
                position.is_active = bool(not closed)
                protect["detail"] = "closed immediately (TP4)"
            elif closed:
                position.tp_hits = hits
                position.sl_price = position.entry_price
                self.ensure_exit_prices(position)
                protect = await self.place_protective_exits(position, position.tp_price, position.sl_price)
                protect["detail"] = f"scaled out TP1-{hits}; runner SL@BE"
            protect["itm"] = f"PARTIAL_TP_{hits}"
        logger.info("✅ [LIVE FILL CONFIRMED] TxHash: %s size=%s ordered=%s entry=%s protect=%s", tx_hash, position.size_eth, order_size, position.entry_price, protect.get("detail"))
        return {
            "success": True,
            "mode": "LIVE_MAINNET",
            "asset": asset,
            "tx_hash": str(tx_hash),
            "position_id": pos_id,
            "side": side_str,
            "size_eth": position.size_eth,
            "ordered_size": order_size,
            "entry_price": position.entry_price,
            "notional_usd": position.notional_usd,
            "tp_target_price": position.tp_price,
            "sl_price": position.sl_price,
            "tp_pct": policy.tp_pct,
            "sl_pct": policy.sl_pct,
            "tp_ladder": list(tp_ladder_prices(side_str, position.entry_price, policy)),
            "max_hold_seconds": policy.max_hold_seconds,
            "protect": protect.get("detail"),
            "exchange_tp": protect.get("tp"),
            "exchange_sl": protect.get("sl"),
            "on_book": protect.get("on_book"),
            "reason": reason,
        }

    async def cancel_open_orders(self, market_index: int, extra_indexes: Optional[List[int]] = None) -> int:
        """Cancel leftover TP/SL (and any other) working orders for one market."""
        if self.is_live and self.signer_client is None:
            await self._ensure_signer()
        if not self.is_live or not self.signer_client or market_index in (None, -1):
            return 0
        cancelled = 0
        async with self._order_lock:
            try:
                if hasattr(self.signer_client, "cancel_all_orders"):
                    tif = getattr(self.signer_client, "CANCEL_ALL_TIF_IMMEDIATE", 0)
                    result = await self.signer_client.cancel_all_orders(
                        time_in_force=tif,
                        timestamp_ms=0,
                        cancel_all_market_index=int(market_index),
                    )
                    _, err = unpack_signer_result(result)
                    if err:
                        logger.warning("cancel_all_orders market %s failed: %s", market_index, err)
                    else:
                        cancelled += 1
                for idx in extra_indexes or []:
                    if not idx or not hasattr(self.signer_client, "cancel_order"):
                        continue
                    result = await self.signer_client.cancel_order(int(market_index), int(idx))
                    _, err = unpack_signer_result(result)
                    if err is None:
                        cancelled += 1
                    elif err:
                        logger.debug("cancel_order %s/%s: %s", market_index, idx, err)
            except Exception as e:
                logger.warning("cancel open orders error: %s", e)
        return cancelled

    async def cancel_one_order(self, market_index: int, order_index: int) -> bool:
        if self.is_live and self.signer_client is None:
            await self._ensure_signer()
        if not self.is_live or not self.signer_client or not order_index:
            return False
        async with self._order_lock:
            try:
                result = await self.signer_client.cancel_order(int(market_index), int(order_index))
                _, err = unpack_signer_result(result)
                if err:
                    logger.warning("cancel_order %s/%s failed: %s", market_index, order_index, err)
                    if "invalid nonce" in str(err).lower():
                        await self._refresh_nonce()
                    return False
                return True
            except Exception as e:
                logger.warning("cancel_one_order error: %s", e)
                return False

    async def sync_position_orders(self, pos: ActivePosition, open_count: int) -> Dict[str, Any]:
        """Every live position gets local TP/SL plus exactly one exchange TP and one SL when the book allows."""
        if self.is_live and self.signer_client is None:
            await self._ensure_signer()
        self.ensure_exit_prices(pos)
        if open_count == 2:
            pos.exchange_tp = pos.exchange_sl = True
            return {"tp": True, "sl": True, "detail": "book already has TP+SL", "pruned": 0}
        if open_count > 0:
            logger.info("Rebuilding TP/SL on %s (had %s working orders)", pos.asset, open_count)
            await self.cancel_open_orders(pos.market_index, [pos.tp_order_index, pos.sl_order_index])
            pos.exchange_tp = pos.exchange_sl = False
            pos.tp_client_index = pos.sl_client_index = 0
            pos.tp_order_index = pos.sl_order_index = 0
            await asyncio.sleep(0.35)
        else:
            logger.info("Attaching missing TP/SL on %s (0 working orders)", pos.asset)
        protect = await self.place_protective_exits(pos, pos.tp_price, pos.sl_price)
        protect["pruned"] = open_count if open_count > 2 else 0
        return protect

    async def care_open_orders(self, prices: Optional[Dict[str, float]] = None) -> Dict[str, int]:
        """Always own the book: cancel orphans/stale/unfilled extras, one TP/SL per live position."""
        from trade_exits import classify_working_order, policy_for, tp_sl_prices

        summary = {"cancelled": 0, "orphans": 0, "stale": 0, "attached": 0, "flattened": 0, "pruned": 0}
        if not self.is_live:
            return summary
        if self.signer_client is None:
            await self._ensure_signer()
        now = time.time()
        if now - self._last_care_ts < 15:
            return summary
        self._last_care_ts = now
        prices = prices or {}
        live_positions = await self.fetch_account_positions()
        live_markets = {int(item["market_index"]) for item in live_positions if float(item.get("size") or 0) > 0 and item.get("market_index") is not None}
        live_symbols = {str(item.get("symbol") or "").upper() for item in live_positions if float(item.get("size") or 0) > 0 and item.get("symbol")}
        orders = await self.fetch_open_orders()
        flatten_assets: Dict[str, int] = {}
        for item in orders:
            market = self._order_int(item, "market_id", "market_index")
            symbol = str(item.get("symbol") or item.get("market_symbol") or "").upper()
            local = self.existing_position(symbol, market if market not in (0, -1) else None)
            max_age = local.max_hold_seconds if local else float(os.getenv("NEWS_ORDER_TTL_SECONDS", str(45 * 60)))
            entry_ttl = float(os.getenv("NEWS_ENTRY_TTL_SECONDS", "90"))
            kind = classify_working_order(item, live_markets, live_symbols, now, max_age, entry_ttl_seconds=entry_ttl)
            if kind == "keep":
                continue
            order_idx = self._order_int(item, "order_index", "order_id", "index")
            ok = False
            if order_idx:
                ok = await self.cancel_one_order(market, order_idx)
            if not ok and market not in (None, -1):
                await self.cancel_open_orders(market, [order_idx])
                ok = True
            if not ok:
                continue
            summary["cancelled"] += 1
            if kind == "orphan":
                summary["orphans"] += 1
                logger.info("Cancelled orphan/unfilled order market=%s idx=%s %s", market, order_idx, symbol)
            else:
                summary["stale"] += 1
                logger.info("Cancelled %s order market=%s idx=%s %s (news/entry expired)", kind, market, order_idx, symbol)
                if symbol:
                    flatten_assets[symbol] = market
                elif local:
                    flatten_assets[local.asset] = local.market_index
        for symbol, market in flatten_assets.items():
            pos = self.existing_position(symbol, market if market not in (0, -1) else None)
            mark = prices.get((pos.asset if pos else symbol).upper()) or (pos.entry_price if pos else 0.0)
            if pos and pos.is_active:
                if await self.close_position(pos, mark or pos.entry_price):
                    pos.is_active = False
                    summary["flattened"] += 1
            elif symbol in live_symbols:
                live = self.match_exchange_position(live_positions, symbol, market)
                if live:
                    dummy = ActivePosition(
                        position_id=f"stale_{symbol}",
                        asset=symbol,
                        market_index=int(live.get("market_index") or market),
                        side=live.get("side") or "BUY/LONG",
                        entry_price=float(live.get("entry_price") or mark or 0),
                        size_eth=float(live.get("size") or 0),
                        notional_usd=0.0,
                    )
                    if await self.close_position(dummy, mark or dummy.entry_price):
                        summary["flattened"] += 1
        live_positions = await self.fetch_account_positions()
        for live in live_positions:
            symbol = str(live.get("symbol") or "").upper()
            mkt_idx = int(live.get("market_index") or 0)
            size = float(live.get("size") or 0)
            if not symbol or size <= 0:
                continue
            pos = self.existing_position(symbol, mkt_idx)
            if pos is None:
                policy = policy_for(symbol)
                entry = float(live.get("entry_price") or 0)
                side = live.get("side") or "BUY/LONG"
                pos = ActivePosition(
                    position_id=f"adopted_{symbol}_{mkt_idx}",
                    asset=symbol,
                    market_index=mkt_idx,
                    side=side,
                    entry_price=entry,
                    size_eth=size,
                    notional_usd=size * entry,
                    tp_pct=policy.tp_pct or self.default_tp_pct or 2.5,
                    sl_pct=policy.sl_pct or 1.5,
                    is_active=True,
                )
                self.ensure_exit_prices(pos)
                self.active_positions[pos.position_id] = pos
                logger.info("🛡️ [CARE AUTO-ADOPT] %s %s Size=%s @ $%.4f", symbol, side, size, entry)
            self.ensure_exit_prices(pos)
            open_n = int(live.get("open_order_count") or live.get("position_tied_order_count") or 0)
            if open_n == 2:
                pos.exchange_tp = pos.exchange_sl = True
                continue
            if now - pos.last_protect_attempt < 20:
                continue
            pos.last_protect_attempt = now
            result = await self.sync_position_orders(pos, open_n)
            pruned = int(result.get("pruned") or 0)
            if pruned:
                summary["pruned"] += pruned
                summary["cancelled"] += max(0, pruned - 2)
            if result.get("tp") or result.get("sl"):
                summary["attached"] += 1
        return summary

    async def close_position(self, pos: ActivePosition, current_market_price: float, qty: Optional[float] = None) -> bool:
        """Close full size, or `qty` for a scale-out. Partials keep the clock and remaining TP/SL."""
        if self.is_live and self.signer_client is None:
            await self._ensure_signer()
        partial = qty is not None and qty > 0
        if not self.is_live or not self.signer_client:
            if partial:
                pos.size_eth = max(0.0, pos.size_eth - float(qty))
                if pos.size_eth <= 0:
                    self.clock.forget(pos.asset)
                return True
            self.clock.forget(pos.asset)
            return True
        books = await self.fetch_account_positions()
        live = self.match_exchange_position(books, pos.asset, pos.market_index)
        if live is None or float(live.get("size") or 0) <= 0:
            if not books:
                logger.error("Close aborted %s: account book unavailable (not assuming flat)", pos.asset)
                return False
            await self.cancel_open_orders(pos.market_index, [pos.tp_order_index, pos.sl_order_index])
            self.clock.forget(pos.asset)
            return True
        live_size = float(live.get("size") or pos.size_eth)
        size = min(float(qty), live_size) if partial else live_size
        if size <= 0:
            return True
        market_index = int(live.get("market_index") or pos.market_index)
        target_left = max(0.0, live_size - size) if partial else 0.0
        for attempt in range(3):
            _, err = await self._submit_live_order(
                pos.asset,
                market_index,
                size,
                current_market_price,
                is_ask=(pos.side == "BUY/LONG"),
                reduce_only=True,
            )
            if err:
                logger.error("❌ [EXEC] Close error for %s: %s", pos.position_id, err)
                await asyncio.sleep(0.6)
                continue
            deadline = time.time() + 12.0
            while time.time() < deadline:
                still = await self.wait_for_exchange_position(pos.asset, pos.market_index, timeout=1.5)
                left = float(still.get("size") or 0) if still else 0.0
                if left <= target_left + 1e-9:
                    await self.cancel_open_orders(market_index, [pos.tp_order_index, pos.sl_order_index])
                    if left <= 1e-12:
                        self.clock.forget(pos.asset)
                    else:
                        pos.size_eth = left
                    return True
                await asyncio.sleep(0.5)
        logger.error("Close not confirmed flat for %s", pos.position_id)
        return False

    async def execute_catalyst_snipe(self, signal: CatalystSignal, current_market_price: float) -> Dict[str, Any]:
        """Wrapper for news catalysts with Dynamic Kelly Sizing."""
        is_ask = (signal.sentiment == "BEARISH")
        return await self.execute_trade(
            asset=signal.target_asset,
            market_index=signal.market_index,
            is_ask=is_ask,
            current_market_price=current_market_price,
            reason=f"NEWS: {signal.headline[:40]}",
            conviction=signal.conviction_score,
        )

    async def close_all_positions(self, current_market_price: Any = 2650.0) -> int:
        """Emergency flatten using each asset's own mark. Confirms flat on live."""
        closed_count = 0
        price_map: Dict[str, float] = current_market_price if isinstance(current_market_price, dict) else {}
        fallback = 0.0 if isinstance(current_market_price, dict) else float(current_market_price or 0)
        for pos in list(self.active_positions.values()):
            if not pos.is_active:
                continue
            mark = price_map.get(pos.asset.upper()) or (fallback if fallback > 0 else pos.entry_price)
            ok = await self.close_position(pos, mark)
            if ok:
                pos.is_active = False
                closed_count += 1
                await self.cancel_open_orders(pos.market_index, [pos.tp_order_index, pos.sl_order_index])
            else:
                logger.error("Flatten not confirmed for %s", pos.position_id)
        return closed_count

    async def harvest_exchange_exits(self, prices: Dict[str, float]) -> List[Dict[str, Any]]:
        """If Lighter TP/SL already flattened a position, book the exit and cancel leftover orders."""
        closed: List[Dict[str, Any]] = []
        if not self.is_live:
            return closed
        live_positions = await self.fetch_account_positions()
        for pos in list(self.active_positions.values()):
            if not pos.is_active:
                continue
            still = self.match_exchange_position(live_positions, pos.asset, pos.market_index)
            if still is not None and float(still.get("size") or 0) > 0:
                live_size = float(still.get("size") or 0)
                if live_size + 1e-9 < (pos.size_eth or 0) * 0.98:
                    dropped = max(0.0, pos.size_eth - live_size)
                    mark = prices.get(pos.asset.upper()) or pos.entry_price
                    if pos.side == "BUY/LONG":
                        pnl_pct = ((mark - pos.entry_price) / pos.entry_price) * 100.0 if pos.entry_price else 0.0
                        pnl_usd = (mark - pos.entry_price) * dropped
                    else:
                        pnl_pct = ((pos.entry_price - mark) / pos.entry_price) * 100.0 if pos.entry_price else 0.0
                        pnl_usd = (pos.entry_price - mark) * dropped
                    pos.original_size = pos.original_size or (pos.size_eth or live_size)
                    pos.size_eth = live_size
                    pos.tp_hits = min(4, int(pos.tp_hits or 0) + 1)
                    if pos.tp_hits >= 1:
                        pos.sl_price = pos.entry_price
                    self.ensure_exit_prices(pos)
                    pos.exchange_tp = pos.exchange_sl = False
                    closed.append({
                        "type": "EXCHANGE_PARTIAL",
                        "pos_id": pos.position_id,
                        "asset": pos.asset,
                        "pnl_pct": pnl_pct,
                        "pnl_usd": pnl_usd,
                        "exit_price": mark,
                        "close_qty": dropped,
                        "full": False,
                        "already_done": True,
                        "tp_level": pos.tp_hits,
                    })
                    logger.info("Exchange scaled out %s leftover=%s tp_hits=%s", pos.asset, live_size, pos.tp_hits)
                continue
            mark = prices.get(pos.asset.upper()) or pos.entry_price
            if pos.side == "BUY/LONG":
                pnl_pct = ((mark - pos.entry_price) / pos.entry_price) * 100.0 if pos.entry_price else 0.0
                pnl_usd = (mark - pos.entry_price) * pos.size_eth
            else:
                pnl_pct = ((pos.entry_price - mark) / pos.entry_price) * 100.0 if pos.entry_price else 0.0
                pnl_usd = (pos.entry_price - mark) * pos.size_eth
            await self.cancel_open_orders(pos.market_index, [pos.tp_order_index, pos.sl_order_index])
            self.clock.forget(pos.asset)
            pos.is_active = False
            closed.append({
                "type": "EXCHANGE_EXIT",
                "pos_id": pos.position_id,
                "asset": pos.asset,
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
                "exit_price": mark,
            })
            logger.info("Exchange already flat %s %s pnl=$%.4f — leftover orders cancelled", pos.asset, pos.side, pnl_usd)
        return closed

    async def check_take_profit_and_stop_loss(self, prices: Dict[str, float]) -> List[Dict[str, Any]]:
        """Watchdog checking if active positions hit TP or Stop-Loss using per-asset prices."""
        closed_events = []
        now = time.time()
        for pos_id, pos in list(self.active_positions.items()):
            if not pos.is_active:
                continue
            self.ensure_exit_prices(pos)
            current_price = prices.get(pos.asset.upper())
            if current_price is None or current_price <= 0 or pos.entry_price <= 0:
                logger.debug("TP watchdog skip %s: no mark (entry=%s)", pos.asset, pos.entry_price)
                continue
            from trade_exits import already_through_exit
            itm = already_through_exit(pos.side, current_price, pos.tp_price, pos.sl_price)
            if itm == "STOP_LOSS":
                closed_events.append(self._exit_event(pos, "STOP_LOSS", current_price, pos.size_eth, full=True))
                continue
            if itm == "TAKE_PROFIT":
                closed_events.extend(self._scale_out_events(pos, current_price))
                continue
            if abs(current_price - pos.entry_price) / pos.entry_price > 0.20 and (now - pos.entry_time) < 15:
                logger.warning(
                    "Skipping TP/SL for %s: mark %s vs entry %s looks like a cross-asset price",
                    pos.asset, current_price, pos.entry_price,
                )
                continue

            pos.highest_price = max(pos.highest_price, current_price)
            pos.lowest_price = min(pos.lowest_price, current_price)

            # Dynamic Breakeven Acceleration: tighten to BE faster if volatility normalizes quickly
            engine = getattr(self, "volatility_engine", None)
            if engine and pos.tp_hits == 0 and pos.entry_price > 0:
                is_long = pos.side.startswith("BUY")
                pnl_now = ((current_price - pos.entry_price) / pos.entry_price * 100.0) if is_long else ((pos.entry_price - current_price) / pos.entry_price * 100.0)
                if engine.should_accelerate_breakeven(pos.asset, pos.side, pos.entry_price, current_price, pnl_now):
                    from trade_exits import breakeven_sl
                    be_price = breakeven_sl(pos.side, pos.entry_price, 0.1)
                    if is_long and (pos.sl_price <= 0 or be_price > pos.sl_price):
                        pos.sl_price = be_price
                        pos.pending_sl_amend = True
                        logger.info("⚡ [BE-ACCELERATION] Volatility normalized for %s: accelerated SL to BE @ %s", pos.asset, be_price)
                    elif not is_long and (pos.sl_price <= 0 or be_price < pos.sl_price):
                        pos.sl_price = be_price
                        pos.pending_sl_amend = True
                        logger.info("⚡ [BE-ACCELERATION] Volatility normalized for %s: accelerated SL to BE @ %s", pos.asset, be_price)

            from trade_exits import ExitPolicy, trail_stop
            policy = ExitPolicy(pos.tp_pct, pos.sl_pct, pos.trail_arm_pct, pos.trail_gap_pct, pos.max_hold_seconds, 80)
            prev_sl = pos.sl_price
            if pos.sl_price:
                pos.sl_price = trail_stop(pos.side, pos.entry_price, pos.highest_price, pos.lowest_price, pos.sl_price, policy)
            else:
                from trade_exits import tp_sl_prices
                _, pos.sl_price = tp_sl_prices(pos.side, pos.entry_price, policy)
            if pos.sl_price and prev_sl and abs(pos.sl_price - prev_sl) / max(pos.entry_price, 1e-9) >= 0.0008:
                pos.pending_sl_amend = True

            if pos.side == "BUY/LONG":
                pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100.0
                realized_usd = (current_price - pos.entry_price) * pos.size_eth
                sl_hit = current_price <= pos.sl_price
                tp_hit = pos.tp_price > 0 and current_price >= pos.tp_price
            else:
                pnl_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100.0
                realized_usd = (pos.entry_price - current_price) * pos.size_eth
                sl_hit = current_price >= pos.sl_price
                tp_hit = pos.tp_price > 0 and current_price <= pos.tp_price

            hit = None
            if tp_hit:
                closed_events.extend(self._scale_out_events(pos, current_price))
                continue
            if sl_hit or pnl_pct <= -pos.sl_pct:
                hit = "STOP_LOSS"
            elif now - pos.entry_time >= pos.max_hold_seconds:
                hit = "TIME_STOP"
            if not hit:
                continue
            closed_events.append(self._exit_event(pos, hit, current_price, pos.size_eth, full=True))

        return closed_events

    def _exit_event(self, pos: ActivePosition, kind: str, price: float, qty: float, full: bool) -> Dict[str, Any]:
        if pos.side == "BUY/LONG":
            pnl_pct = ((price - pos.entry_price) / pos.entry_price) * 100.0 if pos.entry_price else 0.0
            pnl_usd = (price - pos.entry_price) * qty
        else:
            pnl_pct = ((pos.entry_price - price) / pos.entry_price) * 100.0 if pos.entry_price else 0.0
            pnl_usd = (pos.entry_price - price) * qty
        return {
            "type": kind,
            "pos_id": pos.position_id,
            "asset": pos.asset,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "exit_price": price,
            "close_qty": qty,
            "full": full,
        }

    def _partial_tp_event(self, pos: ActivePosition, price: float) -> Dict[str, Any]:
        events = self._scale_out_events(pos, price)
        return events[0] if events else self._exit_event(pos, "PARTIAL_TP_1", price, pos.size_eth, full=True)

    def _scale_out_events(self, pos: ActivePosition, price: float) -> List[Dict[str, Any]]:
        """Emit every TP level the mark has already cleared (Level 1: 50% & SL->BE+0.1%, Level 2: 25%, Level 3: 25% runner)."""
        from trade_exits import already_through_exit, breakeven_sl, partial_qty, policy_for, scale_tp_price

        events: List[Dict[str, Any]] = []
        remaining = pos.size_eth
        hits = int(pos.tp_hits or 0)
        orig = pos.original_size or pos.size_eth
        mult = getattr(pos, "atr_multiplier", 1.0) or 1.0
        policy = policy_for(pos.asset, override_tp=pos.tp_pct or None, override_sl=pos.sl_pct or None, atr_multiplier=mult)
        while hits < 2 and remaining > 0:
            nxt = hits + 1
            tp_px = scale_tp_price(pos.side, pos.entry_price, policy, nxt, atr_multiplier=mult)
            if already_through_exit(pos.side, price, tp_px, 0.0) != "TAKE_PROFIT":
                break
            qty = partial_qty(orig, remaining, nxt)
            if qty <= 0:
                break
            full = False
            ev = self._exit_event(pos, f"PARTIAL_TP_{nxt}", price, qty, full=full)
            ev["tp_level"] = nxt
            events.append(ev)
            remaining -= qty
            hits = nxt
            if nxt == 1:
                pos.sl_price = breakeven_sl(pos.side, pos.entry_price, 0.1)
                pos.pending_sl_amend = True
            elif nxt == 2:
                pos.trail_gap_pct = policy.trail_gap_pct
                pos.pending_sl_amend = True
        return events


# =============================================================================
# NEWS INGESTION STREAMS
# =============================================================================

class NewsIngestionManager:
    """Polls breaking crypto feeds in background with sub-15ms TreeNews WebSocket streaming."""

    def __init__(self, on_news_callback: Callable[[NewsItem], Any], db_path: Optional[str] = None):
        self.on_news_callback = on_news_callback
        self.is_running = False
        self.registry = NewsSourceRegistry()
        self.pipeline = NewsPipeline(db_path=db_path)
        self.scheduler = NewsSourceScheduler(
            self.registry,
            self._handle_records,
            db_path=db_path,
            max_concurrency=int(os.getenv("NEWS_FETCH_CONCURRENCY", "16")),
        )
        self.treenews_ws = TreeNewsWebSocketClient(on_records=self._handle_records)
        try:
            from hyperliquid_whale_tracker import HyperliquidWhaleTracker
            self.whale_tracker = HyperliquidWhaleTracker(on_whale_signal=self._handle_whale_signal)
        except Exception:
            self.whale_tracker = None

    async def start(self):
        self.is_running = True
        enabled = [source.source_id for source in self.registry.enabled() if source.adapter != "webhook"]
        logger.info("📡 [NEWS] Multi-source news registry listening: %s", ", ".join(enabled))
        asyncio.create_task(self.scheduler.run_forever())
        if os.getenv("TREENEWS_WS_ENABLED", "true").lower() == "true":
            self.treenews_ws.start()
            logger.info("⚡ [NEWS] TreeNews Sub-15ms WebSocket Ingestion Client streaming active")
        if self.whale_tracker and os.getenv("HL_WHALES_ENABLED", "true").lower() == "true":
            await self.whale_tracker.start()
            logger.info("🐋 [NEWS] Hyperliquid Smart Money & Whale Scanner active (Tape + Leaderboard)")

    async def stop(self):
        self.is_running = False
        await self.scheduler.stop()
        await self.treenews_ws.stop()
        if self.whale_tracker:
            await self.whale_tracker.stop()

    async def _handle_whale_signal(self, sig: Dict[str, Any]):
        """Whale radar auto-execution when smart-money fills large orders."""
        asset = str(sig.get("asset", "")).upper()
        side = str(sig.get("side", "")).upper()
        notional = float(sig.get("notional_usd", 0.0))
        price = float(sig.get("entry_price", 0.0))
        trader = str(sig.get("trader", "Whale Leader"))

        logger.info(
            "🐋 [WHALE RADAR] %s | %s %s ($%s USD) @ $%s",
            asset,
            side,
            trader,
            f"{notional:,.0f}",
            f"{price:,.2f}",
        )

        # Trigger auto-entry when smart money notional >= $50,000 USD
        if notional >= float(os.getenv("WHALE_MIN_TRADE_USD", "50000")) and asset in ["BTC", "ETH", "SOL", "HYPE", "TRUMP", "DOGE", "AVAX"]:
            direction = "BULLISH" if side in ["BUY", "LONG"] else "BEARISH"
            whale_event = NormalizedNewsEvent(
                event_id=f"whale_{asset}_{int(time.time()*1000)}",
                source_id="hyperliquid_whale",
                publisher="HyperliquidTape",
                headline=f"🐋 Hyperliquid Mega Whale {side} ${notional:,.0f} {asset}",
                body=f"Smart money position entry on Hyperliquid tape at ${price:,.2f}",
                event_type="whale",
                direction=direction,
                confidence=0.88,
                entities=[asset],
                url="",
                cluster_id=f"whale_{asset}_{direction}",
            )
            item = NewsItem(
                source="HyperliquidWhale",
                headline=whale_event.headline,
                body=whale_event.body,
                timestamp=time.time(),
                url="",
            )
            await self.on_news_callback(item, whale_event)

    async def _handle_records(self, records):
        for event in self.pipeline.process(records):
            published_at = event.published_at.timestamp() if event.published_at else event.ingested_at.timestamp()
            item = NewsItem(
                source=event.publisher,
                headline=event.headline,
                body=event.body,
                timestamp=published_at,
                url=event.url,
            )
            await self.on_news_callback(item, event)

    async def _poll_crypto_rss(self):
        await self.scheduler.run_forever()


# =============================================================================
# ORCHESTRATOR & CLI ENTRYPOINT
# =============================================================================

class LighterNewsSniperBot:
    """Master Orchestrator for Catalyst and Manual Trading."""

    def __init__(self, is_live: bool = False, max_margin_pct: float = 85.0):
        self.is_live = live_execution_allowed(is_live)
        self.classifier = CatalystClassifier()
        self.executor = MaxSizeExecutionEngine(
            is_live=self.is_live,
            max_margin_utilization_pct=max_margin_pct,
            default_tp_pct=2.5,
            default_sl_pct=1.5,
        )
        self.db_path = os.getenv("NEWS_DB_PATH", str(Path(__file__).with_name("lighter_news.db")))
        self.news_manager = NewsIngestionManager(self._handle_news_event, db_path=self.db_path)
        self.markets = MarketRegistry()
        self.tickers = TickerCache()
        self.intent_queue = TradeIntentQueue(self.db_path)
        self.positions = PositionBook(self.db_path)
        self.paper_fills = PaperFillSimulator()
        self.metrics = NewsMetrics()
        self.audit = AuditLog(str(Path(__file__).with_name("news_audit.jsonl")))
        self.current_market_price = float(os.getenv("LIGHTER_ETH_PRICE", "2650.0"))
        self.current_market_timestamp = time.time()
        self.story_fingerprint_window_sec = float(os.getenv("NEWS_STORY_FINGERPRINT_WINDOW_SEC", "900.0"))
        self._story_fingerprints: List[Tuple[str, str, str, set, float]] = []
        self.news_risk_gate = LighterNewsRiskGate(live=self.is_live)
        self.momentum_filter = CrossExchangeMomentumFilter()
        self.news_risk_gate.momentum_filter = self.momentum_filter
        self._latest_news_events: Dict[str, NormalizedNewsEvent] = {}
        self.shadow = os.getenv("NEWS_PROMOTION_MODE", "").strip().lower() == "shadow"
        self.kill_file = Path(__file__).with_name("NEWS_KILL_SWITCH")
        self.reconciled = False
        from news_scoreboard import ShadowScoreboard
        self.scoreboard = ShadowScoreboard(self.db_path)
        self.news_manager.pipeline.on_correction = self._on_correction

        try:
            from lighter_telegram import LighterTelegramBot
            from lighter_db import LighterDBManager
            self.db = LighterDBManager()
            self.tg_bot = LighterTelegramBot(
                bot_context={
                    "is_paper_mode": (not self.is_live) or self.shadow,
                    "market_index": 0,
                    "db": self.db,
                    "executor": self.executor,
                    "news_manager": self.news_manager,
                    "bot_instance": self,
                    "intent_queue": self.intent_queue,
                    "positions": self.positions,
                    "metrics": self.metrics,
                    "markets": self.markets,
                }
            )
            self.tg_bot.start_polling_in_background()
            logger.info("📱 [TG] High-Speed Interactive Telegram Bot active.")
        except Exception as tge:
            logger.warning(f"Telegram listener init warning: {tge}")

    def _prune_story_fingerprints(self, now: float) -> None:
        cutoff = now - self.story_fingerprint_window_sec
        self._story_fingerprints = [item for item in self._story_fingerprints if item[4] >= cutoff]

    def _extract_story_tokens(self, text: str) -> set:
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        stopwords = {
            "a", "an", "the", "in", "on", "at", "to", "for", "of", "with", "by", "from",
            "and", "or", "as", "is", "are", "was", "were", "it", "this", "that", "be", "has", "have"
        }
        return words - stopwords

    def _is_duplicate_story_fingerprint(self, asset: str, side: str, event: NormalizedNewsEvent, now: float) -> bool:
        sym = asset.upper()
        cluster_id = getattr(event, "cluster_id", "")
        tokens = self._extract_story_tokens(f"{event.headline} {event.body}")
        for cached_asset, cached_side, cached_cluster, cached_tokens, ts in self._story_fingerprints:
            if cached_asset == sym:
                if cluster_id and cached_cluster and cluster_id == cached_cluster:
                    return True
                if cached_side == side:
                    union = tokens | cached_tokens
                    jaccard = len(tokens & cached_tokens) / len(union) if union else 1.0
                    if jaccard >= 0.35 or len(tokens & cached_tokens) >= 3:
                        return True
        return False

    def _record_story_fingerprint(self, asset: str, side: str, event: NormalizedNewsEvent, now: float) -> None:
        sym = asset.upper()
        cluster_id = getattr(event, "cluster_id", "")
        tokens = self._extract_story_tokens(f"{event.headline} {event.body}")
        self._story_fingerprints.append((sym, side, cluster_id, tokens, now))

    def _on_correction(self, event: NormalizedNewsEvent) -> None:
        self.metrics.inc("corrections")
        self.audit.emit("correction", event.event_id, cluster_id=event.cluster_id, headline=event.headline)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.intent_queue.invalidate_cluster(event.cluster_id))
        except RuntimeError:
            pass

    def _authorized(self) -> bool:
        chat_id = (os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        return bool(chat_id) and chat_id != "0"

    def kill_engaged(self) -> bool:
        return self.kill_file.exists() or os.getenv("NEWS_KILL_SWITCH", "").lower() in {"1", "true", "yes"}

    def set_kill(self, on: bool) -> None:
        if on:
            self.kill_file.write_text("1", encoding="utf-8")
        elif self.kill_file.exists():
            self.kill_file.unlink()

    def mode_label(self) -> str:
        if self.kill_engaged():
            return "KILLED"
        if self.shadow:
            return "SHADOW"
        if self.is_live:
            return "LIVE"
        return "PAPER"

    async def _handle_news_event(self, news: NewsItem, event: Optional[NormalizedNewsEvent] = None):
        if event is None:
            return
        self.metrics.inc("ingested")
        self.audit.emit("observed", event.event_id, source=event.source_id, type=event.event_type, headline=event.headline)
        from news_quality import quality_veto

        ok, veto_reason = quality_veto(event)
        if not ok:
            self.metrics.inc("quality_veto")
            logger.info("Quality veto: %s | %s", veto_reason, event.headline[:120])
            return
        confirmed = self.news_manager.pipeline.confirmed(event)
        from news_direction import macro_routes
        market = None
        side = "BUY/LONG"
        for symbol, route_side in macro_routes(event):
            found = self.markets.get(symbol)
            if found:
                market = found
                side = route_side
                break
        if market is None:
            market, reason = self.markets.resolve(event)
            side = "SELL/SHORT" if event.direction == "BEARISH" else "BUY/LONG"
        if market is None:
            self.metrics.inc("unresolved_asset")
            return
        if not confirmed:
            self.metrics.inc("unconfirmed")
            return

        # Check active position lockout
        if self.executor.existing_position(market.symbol, market.market_index):
            self.metrics.inc("active_position_lockout")
            logger.info("[DEDUP] Dropped duplicate news: Active position already exists for %s", market.symbol)
            return

        # Story Fingerprint / Cluster Lockout: Track traded clusters and story fingerprints with a 15-minute expiration
        now = time.time()
        self._prune_story_fingerprints(now)
        if self._is_duplicate_story_fingerprint(market.symbol, side, event, now):
            self.metrics.inc("duplicate_catalyst_lockout")
            logger.info("[DEDUP] Dropped duplicate news: Story fingerprint/cluster already processed for %s (%s)", market.symbol, event.headline[:80])
            return

        snapshot = self.tickers.get(market.symbol)
        if snapshot is None or not snapshot.fresh:
            fetched = await self.executor.fetch_market_snapshot(market.symbol, market.market_index)
            if fetched:
                snapshot = fetched
                self.tickers.update(fetched)
                if market.symbol == "ETH":
                    self.current_market_price = fetched.price
                    self.current_market_timestamp = fetched.timestamp
        if snapshot is None or (self.is_live and not snapshot.fresh):
            if self.is_live:
                self.metrics.inc("stale_price_veto")
                logger.warning("News signal vetoed: live market price is missing or stale for %s", market.symbol)
                return
            snapshot = self.tickers.snapshot_or_env(market)
        spread = await self.executor.fetch_spread_bps(int(snapshot.market_index or market.market_index))
        if spread > 0:
            snapshot.spread_bps = spread

        if side not in market.enabled_sides:
            self.metrics.inc("side_disabled")
            return
        requested_usd = min(self.news_risk_gate.max_trade_usd, float(os.getenv("NEWS_REQUESTED_USD", "50")))
        authorized = self._authorized()
        collateral = await self.executor.fetch_available_collateral_usd()
        momentum_confirmed = None
        if self.momentum_filter and event.confidence >= self.momentum_filter.high_conviction_threshold:
            sentiment = "BULLISH" if side.startswith("BUY") else "BEARISH"
            m_conf = await self.momentum_filter.verify_spike(market.symbol, sentiment, conviction_score=event.confidence)
            momentum_confirmed = m_conf.confirmed
            if m_conf.confirmed:
                self.metrics.inc("momentum_confirmed")
                logger.info("⚡ Cross-Exchange Momentum confirmed on Binance/Bybit: %s", m_conf.summary)
            else:
                self.metrics.inc("momentum_unconfirmed")
                logger.warning("⚠️ Cross-Exchange Momentum unconfirmed: %s", m_conf.summary)

        decision = await self.news_risk_gate.approve(
            event,
            snapshot,
            requested_usd,
            confirmed,
            authorized,
            asset=market.symbol,
            side=side,
            collateral_usd=collateral,
            stop_distance_pct=market.sl_pct,
            momentum_confirmed=momentum_confirmed,
            active_positions=self.executor.active_positions,
        )
        if not decision.approved:
            self.metrics.inc("vetoed")
            self.audit.emit("vetoed", event.event_id, reasons=decision.reasons)
            logger.warning("News signal vetoed: %s", "; ".join(decision.reasons))
            logger.info("🚫 [1st-News Guard] Duplicate/cooldown signal dropped silently: %s", "; ".join(decision.reasons))
            return

        if self.kill_engaged():
            await self.news_risk_gate.release(decision.reservation_id, market.symbol, side)
            self.metrics.inc("killed")
            return
        if self.shadow:
            self.metrics.inc("shadow_signal")
            self.audit.emit("shadow", event.event_id, asset=market.symbol, side=side, usd=decision.sized_usd)
            from news_scoreboard import ShadowBet
            self.scoreboard.record(ShadowBet(
                bet_id=event.event_id, asset=market.symbol, side=side, event_type=event.event_type,
                headline=event.headline, entry_price=snapshot.price, created_at=time.time(),
                cluster_id=event.cluster_id,
            ))
            try:
                from lighter_telegram import tg_send
                tg_send(
                    f"👁 <b>SHADOW — would have traded</b>\n"
                    f"📰 {event.headline}\n"
                    f"🎯 {market.symbol} {side} ${decision.sized_usd:.2f}"
                )
            except Exception:
                pass
            await self.news_risk_gate.release(decision.reservation_id, market.symbol, side)
            return

        intent = await self.intent_queue.enqueue(event, market, side, decision.sized_usd or requested_usd)
        if intent.status != "intent" or intent.event_id != event.event_id:
            self.metrics.inc("duplicate_intent")
            await self.news_risk_gate.release(decision.reservation_id, market.symbol, side)
            return
        await self.intent_queue.mark(intent.intent_id, "reserved", reservation_id=decision.reservation_id)
        self._record_story_fingerprint(market.symbol, side, event, now)

        try:
            result = await self.executor.execute_trade(
                asset=market.symbol,
                market_index=int(snapshot.market_index or market.market_index),
                is_ask=side.startswith("SELL"),
                current_market_price=snapshot.price,
                reason=f"NEWS: {event.headline[:40]}",
                notional_usd=decision.sized_usd or requested_usd,
            )
            if not result.get("success"):
                await self.intent_queue.mark(intent.intent_id, "rejected", reasons=(str(result.get("error", "execution failed")),))
                self.metrics.inc("rejected")
                return
            intent.fill_price = float(result.get("entry_price") or snapshot.price)
            intent.fill_size = float(result.get("size_eth") or 0.0)
            await self.intent_queue.mark(intent.intent_id, "filled", reservation_id=decision.reservation_id)
            self.positions.activate_from_fill(intent)
            self.news_risk_gate.record_fill(market.symbol)
            self.metrics.inc("filled_live" if self.is_live else "filled_paper")
            from news_scoreboard import ShadowBet
            self.scoreboard.record(ShadowBet(
                bet_id=event.event_id + "_live", asset=market.symbol, side=side, event_type=event.event_type,
                headline=event.headline, entry_price=float(result.get("entry_price") or snapshot.price),
                created_at=time.time(), cluster_id=event.cluster_id,
            ))
            self.audit.emit("filled", event.event_id, mode=result.get("mode"), asset=market.symbol, side=side, notional=result.get("notional_usd"))
            try:
                from lighter_telegram import format_fill_card, tg_send
                tg_send(format_fill_card(result, headline=event.headline))
            except Exception as tge:
                logger.warning("Telegram alert error: %s", tge)
        finally:
            await self.news_risk_gate.release(decision.reservation_id, market.symbol, side)

    async def _price_loop(self):
        while True:
            try:
                books = await self.executor.fetch_order_catalog()
                self.markets.ingest_catalog(books)
                wanted = [(market.symbol, market.market_index) for market in self.markets.enabled()]
                for asset, snapshot in self.executor.snapshots_from_catalog(books, wanted).items():
                    self.tickers.update(snapshot)
                    if hasattr(self.executor, "volatility_engine") and self.executor.volatility_engine:
                        self.executor.volatility_engine.on_tick(snapshot.asset, snapshot.price, timestamp=snapshot.timestamp)
                    if asset == "ETH":
                        self.current_market_price = snapshot.price
                        self.current_market_timestamp = snapshot.timestamp
            except Exception as e:
                logger.debug("[Price loop] %s", e)
            await asyncio.sleep(2.0)

    async def _honor_close_requests(self, prices: Dict[str, float]) -> None:
        """Flatten symbols listed in CLOSE_SYMBOLS. Keep the file until exchange is flat."""
        path = Path(__file__).with_name("CLOSE_SYMBOLS")
        wanted: set[str] = {item.strip().upper() for item in os.getenv("NEWS_CLOSE_SYMBOLS", "").split(",") if item.strip()}
        if path.exists():
            try:
                wanted |= {
                    line.strip().upper()
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#")
                }
            except OSError as exc:
                logger.warning("CLOSE_SYMBOLS read failed: %s", exc)
        if not wanted:
            return
        leftover = set(wanted)
        live = await self.executor.fetch_account_positions() if self.executor.is_live else []
        live_by_sym = {str(item.get("symbol") or "").upper(): item for item in live}
        for symbol in list(wanted):
            pos = self.executor.existing_position(symbol)
            item = live_by_sym.get(symbol)
            if pos is None and item and float(item.get("size") or 0) > 0:
                pos = ActivePosition(
                    position_id=f"close_{symbol}",
                    asset=symbol,
                    market_index=int(item.get("market_index") or 0),
                    side=item.get("side") or "BUY/LONG",
                    entry_price=float(item.get("entry_price") or 0),
                    size_eth=float(item.get("size") or 0),
                    notional_usd=0.0,
                )
            if pos is None or not pos.is_active:
                if item is None or float(item.get("size") or 0) <= 0:
                    leftover.discard(symbol)
                continue
            mark = prices.get(symbol) or pos.entry_price
            logger.warning("Operator close request for %s @ %s", symbol, mark)
            await self.executor.cancel_open_orders(pos.market_index, [pos.tp_order_index, pos.sl_order_index])
            closed = await self.executor.close_position(pos, mark)
            pos.is_active = not closed
            if closed:
                leftover.discard(symbol)
            try:
                from lighter_telegram import tg_send
                tg_send(
                    f"🧹 <b>Closed {symbol}</b> (false news match / operator request)\n"
                    f"{'flat confirmed' if closed else 'NOT FLAT — retrying'}"
                )
            except Exception:
                pass
        try:
            if leftover:
                path.write_text("\n".join(sorted(leftover)) + "\n", encoding="utf-8")
            elif path.exists():
                path.unlink()
        except OSError:
            pass

    async def _tp_watchdog_loop(self):
        """Monitors active positions every second for Take-Profit and Stop-Loss."""
        while True:
            try:
                # 1. Automatically adopt & sync all on-chain exchange positions with TP/SL
                prices: Dict[str, float] = await self.executor.sync_and_adopt_all_live_positions()
                for pos in self.executor.active_positions.values():
                    snap = self.tickers.get(pos.asset)
                    if snap and snap.price > 0:
                        prices[pos.asset.upper()] = snap.price
                        if hasattr(self.executor, "volatility_engine") and self.executor.volatility_engine:
                            self.executor.volatility_engine.on_tick(pos.asset, snap.price, timestamp=snap.timestamp)
                harvested = await self.executor.harvest_exchange_exits(prices)
                try:
                    await self._honor_close_requests(prices)
                except Exception as close_err:
                    logger.warning("Close-request file: %s", close_err)
                try:
                    care = await self.executor.care_open_orders(prices)
                    if care.get("cancelled") or care.get("flattened") or care.get("attached"):
                        logger.info("Order care %s", care)
                except Exception as care_err:
                    logger.debug("Order care skipped: %s", care_err)
                events = harvested + await self.executor.check_take_profit_and_stop_loss(prices)
                for pos in list(self.executor.active_positions.values()):
                    if pos.is_active and pos.pending_sl_amend:
                        try:
                            await self.executor.amend_trailing_sl(pos)
                        except Exception as trail_err:
                            logger.debug("Trail SL amend skipped: %s", trail_err)
                for ev in events:
                    pos = self.executor.active_positions.get(ev["pos_id"])
                    if not pos:
                        continue
                    if pos.is_active:
                        already_done = bool(ev.get("already_done"))
                        qty = ev.get("close_qty")
                        kind = str(ev.get("type") or "")
                        partial = (kind.startswith("PARTIAL") or kind == "EXCHANGE_PARTIAL") and not ev.get("full")
                        if not already_done:
                            await self.executor.cancel_open_orders(pos.market_index, [pos.tp_order_index, pos.sl_order_index])
                            exit_submitted = await self.executor.close_position(
                                pos, ev["exit_price"], qty=(qty if partial else None)
                            )
                        else:
                            exit_submitted = True
                        if not exit_submitted:
                            logger.error("TP/SL close not confirmed for %s; leaving position open", pos.position_id)
                            continue
                        if partial and pos.size_eth > 1e-12:
                            pos.tp_hits = max(int(pos.tp_hits or 0), int(ev.get("tp_level") or 0))
                            pos.original_size = pos.original_size or pos.size_eth
                            if pos.tp_hits >= 1:
                                pos.sl_price = pos.entry_price
                                pos.max_hold_seconds = max(pos.max_hold_seconds, 3 * 3600)
                            if pos.tp_hits >= 2:
                                pos.trail_gap_pct = min(pos.trail_gap_pct, max(0.25, pos.trail_gap_pct * 0.65))
                            self.executor.ensure_exit_prices(pos)
                            pos.exchange_tp = pos.exchange_sl = False
                            try:
                                await self.executor.place_protective_exits(pos, pos.tp_price, pos.sl_price)
                            except Exception as prot_err:
                                logger.debug("Re-arm TP/SL after partial: %s", prot_err)
                            logger.info(
                                "Scale-out %s %s qty=%s remaining=%s hits=%s next_tp=%s sl@%s",
                                ev.get("type"), pos.asset, qty, pos.size_eth, pos.tp_hits, pos.tp_price, pos.sl_price,
                            )
                        else:
                            pos.is_active = False
                    else:
                        exit_submitted = True
                    book_pos = next((item for item in self.positions.active() if item.asset == ev["asset"]), None)
                    if book_pos:
                        self.positions.mark_exit(book_pos.position_id, ev["type"], ev["exit_price"], True)
                    self.news_risk_gate.record_pnl(float(ev.get("pnl_usd") or 0.0))
                    from lighter_telegram import tg_send
                    from lighter_telegram import format_exit_card
                    tg_send(format_exit_card(ev, flat=exit_submitted))
                for retry in self.positions.due_retries():
                    pos = self.positions.positions.get(retry.position_id)
                    if not pos:
                        continue
                    if self.executor.existing_position(pos.asset, pos.market_index):
                        self.positions.mark_exit(pos.position_id, "adopted_managed", pos.entry_price, True)
                        continue
                    dummy = ActivePosition(
                        position_id=pos.position_id,
                        asset=pos.asset,
                        market_index=pos.market_index,
                        side=pos.side,
                        entry_price=pos.entry_price,
                        size_eth=pos.size,
                        notional_usd=pos.notional_usd,
                    )
                    mark = prices.get(pos.asset.upper()) or pos.exit_price or pos.entry_price
                    submitted = await self.executor.close_position(dummy, mark)
                    self.positions.mark_exit(pos.position_id, retry.reason, mark, submitted)
            except Exception as e:
                logger.warning("[TP Watchdog Error]: %s", e)
            await asyncio.sleep(1.0)

    async def reconcile_exchange(self) -> None:
        if not self.executor.is_live:
            self.reconciled = True
            return
        found = await self.executor.fetch_account_positions()
        logger.info("Startup reconcile found %s exchange position(s)", len(found))
        flatten = os.getenv("NEWS_STARTUP_FLATTEN", "false").lower() in {"1", "true", "yes"}
        wanted = [(str(it.get("symbol") or ""), int(it.get("market_index") or 0)) for it in found if it.get("symbol")]
        try:
            books = await self.executor.fetch_order_catalog()
            for asset, snapshot in self.executor.snapshots_from_catalog(books, wanted).items():
                self.tickers.update(snapshot)
        except Exception:
            pass
        for item in found:
            symbol = item["symbol"] or "UNK"
            snap = self.tickers.get(symbol)
            price = item["entry_price"] or (snap.price if snap else 0.0)
            if flatten:
                dummy = ActivePosition(
                    position_id=f"recon_{symbol}",
                    asset=symbol,
                    market_index=item["market_index"],
                    side=item["side"],
                    entry_price=item["entry_price"],
                    size_eth=item["size"],
                    notional_usd=item["size"] * (item["entry_price"] or 1),
                )
                await self.executor.close_position(dummy, price or item["entry_price"])
                logger.warning("Startup flatten submitted for %s", symbol)
                continue
            from trade_exits import already_through_exit, infer_tp_hits, policy_for, scaled_out_qty
            policy = policy_for(symbol)
            entry = item["entry_price"] or price or 0.0
            pos = ActivePosition(
                position_id=f"recon_{symbol}_{int(time.time())}",
                asset=symbol,
                market_index=item["market_index"],
                side=item["side"],
                entry_price=entry,
                size_eth=item["size"],
                notional_usd=item["size"] * (entry or 1),
                highest_price=entry,
                lowest_price=entry,
                original_size=item["size"],
                tp_pct=policy.tp_pct,
                sl_pct=policy.sl_pct,
                max_hold_seconds=policy.max_hold_seconds,
                trail_arm_pct=policy.trail_arm_pct,
                trail_gap_pct=policy.trail_gap_pct,
            )
            self.executor.ensure_exit_prices(pos)
            saved = self.executor.clock.recall(symbol)
            pos.entry_time = self.executor.clock.remember(symbol, saved or pos.entry_time)
            if saved:
                logger.info("Restored hold clock for %s first_seen=%.0fs ago", symbol, time.time() - pos.entry_time)
            self.executor.active_positions[pos.position_id] = pos
            snap_mark = snap.price if snap and snap.price > 0 else entry
            itm = already_through_exit(item["side"], snap_mark, pos.tp_price, pos.sl_price) if snap_mark else None
            hits = infer_tp_hits(item["side"], entry, snap_mark, policy) if snap_mark else 0
            if itm == "STOP_LOSS" and snap_mark:
                logger.info("Adopted %s already through STOP_LOSS @ %s — closing now", symbol, snap_mark)
                await self.executor.cancel_open_orders(pos.market_index, [pos.tp_order_index, pos.sl_order_index])
                closed = await self.executor.close_position(pos, snap_mark)
                pos.is_active = not closed
                continue
            if hits >= 1 and snap_mark:
                qty = scaled_out_qty(pos.original_size, pos.size_eth, hits)
                logger.info("Adopted %s already through TP%s @ %s — catch-up scale qty=%s", symbol, hits, snap_mark, qty)
                await self.executor.cancel_open_orders(pos.market_index, [pos.tp_order_index, pos.sl_order_index])
                if hits >= 4:
                    closed = await self.executor.close_position(pos, snap_mark)
                    pos.is_active = not closed
                    continue
                closed = await self.executor.close_position(pos, snap_mark, qty=qty)
                if closed:
                    pos.tp_hits = hits
                    pos.sl_price = pos.entry_price
                    pos.max_hold_seconds = max(pos.max_hold_seconds, 3 * 3600)
                    self.executor.ensure_exit_prices(pos)
            open_n = int(item.get("open_order_count") or 0)
            if pos.tp_price and pos.sl_price:
                try:
                    protect = await self.executor.sync_position_orders(pos, open_n)
                    logger.info("Adopted %s %s size=%s orders=%s protect=%s", symbol, item["side"], item["size"], open_n, protect.get("detail"))
                except Exception as prot_err:
                    logger.warning("Adopted %s but TP/SL sync failed: %s", symbol, prot_err)
            else:
                logger.info("Adopted exchange position %s %s size=%s (no entry for TP/SL)", symbol, item["side"], item["size"])
        self.reconciled = True
        exch_syms = {str(item.get("symbol") or "").upper() for item in found}
        for book in list(self.positions.active()):
            reason = "adopted_live" if book.asset.upper() in exch_syms else "already_flat"
            self.positions.mark_exit(book.position_id, reason, book.entry_price, True)
        try:
            from lighter_telegram import tg_send
            tg_send(f"🔁 <b>Reconcile</b> found {len(found)} exchange position(s). flatten={flatten}")
        except Exception:
            pass

    async def sync_lighter_universe(self, notify: bool = True) -> Dict[str, Any]:
        """Refresh Lighter markets, alias new tickers, attach ticker Google News feeds."""
        from news_sources import register_ticker_sources
        from news_universe import ASSET_ALIASES, sync_catalog
        from trade_exits import COMMODITY, CRYPTO, FX, INDEX

        books = await self.executor.fetch_order_catalog()
        if books:
            self.markets.ingest_catalog(books)
        symbols = [market.symbol for market in self.markets.enabled()]
        listed = set(symbols)
        named = FX | INDEX | COMMODITY | CRYPTO | set(ASSET_ALIASES.values())
        new, first = sync_catalog(symbols)
        seed = sorted(named & listed)
        open_syms = [p.asset.upper() for p in self.executor.active_positions.values() if p.is_active]
        # First boot: named assets only. Later: also attach feeds for pairs Lighter just listed.
        want = list(dict.fromkeys(open_syms + seed + (new if not first else [])))
        added = register_ticker_sources(self.news_manager.registry, want, limit=80)
        logger.info(
            "Universe sync markets=%s new=%s first=%s ticker_feeds=%s",
            len(symbols), new[:12], first, added,
        )
        if notify and new and not first:
            try:
                from lighter_telegram import tg_send
                shown = ", ".join(new[:30]) + ("…" if len(new) > 30 else "")
                tg_send(f"🆕 <b>New Lighter pairs</b>\n{shown}\n📡 ticker news sources attached")
            except Exception:
                pass
        return {"markets": len(symbols), "new": new, "ticker_feeds": added, "first": first}

    async def _universe_loop(self) -> None:
        interval = float(os.getenv("NEWS_UNIVERSE_SECONDS", str(12 * 3600)))
        while True:
            try:
                await self.sync_lighter_universe(notify=True)
            except Exception as e:
                logger.warning("universe sync: %s", e)
            await asyncio.sleep(max(300.0, interval))

    async def _heartbeat_loop(self) -> None:
        interval = float(os.getenv("NEWS_HEARTBEAT_SECONDS", "300"))
        ping_tg = os.getenv("NEWS_HEARTBEAT_TELEGRAM", "false").lower() in {"1", "true", "yes"}
        while True:
            await asyncio.sleep(interval)
            try:
                for bet in self.scoreboard.due():
                    snap = self.tickers.get(bet.asset)
                    if snap and snap.price > 0:
                        self.scoreboard.settle(bet.bet_id, snap.price)
                board = self.scoreboard.summary()
                collat = await self.executor.fetch_available_collateral_usd()
                open_n = len([p for p in self.executor.active_positions.values() if p.is_active])
                logger.info(
                    "heartbeat mode=%s open=%s collat=%s daily_loss=%s scoreboard %s/%s",
                    self.mode_label(),
                    open_n,
                    collat,
                    self.news_risk_gate._daily_loss_usd,
                    board.get("hits", 0),
                    board.get("closed", 0),
                )
                if ping_tg:
                    from lighter_telegram import tg_send
                    tg_send(
                        f"❤️ <b>Heartbeat</b> mode={self.mode_label()}\n"
                        f"open={open_n} collat=${collat if collat is not None else 0:.2f}\n"
                        f"daily_loss=${self.news_risk_gate._daily_loss_usd:.2f}\n"
                        f"scoreboard {board.get('hits',0)}/{board.get('closed',0)} hit-rate={board.get('hit_rate',0):.0%}"
                    )
            except Exception as e:
                logger.debug("heartbeat: %s", e)

    async def run(self):
        mode = self.mode_label()
        logger.info("=" * 60)
        logger.info(f"  LIGHTER NEWS & MANUAL SNIPER BOT ({mode})")
        logger.info("=" * 60)
        ready, reasons = self.news_risk_gate.readiness(
            self._authorized(),
            MarketSnapshot("ETH", self.current_market_price, timestamp=time.time()),
            bool(self.markets.enabled()),
        )
        if self.is_live and not ready:
            logger.error("Live readiness failed: %s", "; ".join(reasons))
        books = await self.executor.fetch_order_catalog()
        if books:
            self.markets.ingest_catalog(books)
            logger.info("Loaded %s Lighter markets (crypto/equities/FX/commodities)", len(self.markets.enabled()))
        if self.is_live:
            await self.executor._ensure_signer()
            if self.executor.signer_client is None:
                logger.error("Live mode requested but SignerClient is unavailable — no orders will send")
            else:
                logger.info("Live signer ready for account #%s", self.executor.account_index)
            await self.reconcile_exchange()
        await self.news_manager.start()
        asyncio.create_task(self._price_loop())
        asyncio.create_task(self._tp_watchdog_loop())
        asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._universe_loop())

        # Launch Integrated Fast Telegram Poller for instant interactive command responses
        try:
            from lighter_telegram import LighterTelegramBot
            tg_ctx = {
                "executor": self.executor,
                "bot": self,
                "markets": self.markets,
                "db": getattr(self, "db", None),
            }
            self.tg_bot = LighterTelegramBot(tg_ctx)
            asyncio.create_task(self.tg_bot.run_fast_polling())
            logger.info("⚡ [Telegram] Integrated Ultra-Fast Zero-Lag Poller active (/status, /balance, /positions, /help)")
        except Exception as e:
            logger.error("Failed to start integrated Telegram poller: %s", e)

        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            logger.info("Bot shutting down...")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Lighter News & Manual Catalyst Sniper Bot")
    parser.add_argument("--live", action="store_true", help="Enable live order execution with real funds")
    parser.add_argument("--margin-pct", type=float, default=85.0, help="Max collateral margin utilization percentage")
    args = parser.parse_args()

    bot = LighterNewsSniperBot(is_live=args.live, max_margin_pct=args.margin_pct)
    asyncio.run(bot.run())
