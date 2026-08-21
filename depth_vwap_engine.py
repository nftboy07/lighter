#!/usr/bin/env python3
"""
WebSocket L2/L3 Live Depth & Microstructure VWAP Sizing Engine
=============================================================
Institutional-grade microsecond orderbook management and liquidity sizing:
- In-memory high-performance sorted bid/ask orderbook with microsecond delta updates
- Dynamic Volume-Weighted Average Price (VWAP) calculation across arbitrary depth levels
- Liquidity-adjusted sizing to strictly enforce slippage caps before order submission
- Top-of-book fallback for thin or sparse market depth
- Microstructure alpha signals (Order Book Imbalance, Micro-price, Depth Liquidity Density)
"""

import math
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union


class OrderBookSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    BID = "BUY"
    ASK = "SELL"


def normalize_side(side: Union[str, OrderBookSide, Any]) -> OrderBookSide:
    """Normalizes side representation to OrderBookSide.BUY or OrderBookSide.SELL."""
    if isinstance(side, OrderBookSide):
        return OrderBookSide.BUY if side in (OrderBookSide.BUY, OrderBookSide.BID) else OrderBookSide.SELL
    if hasattr(side, "value"):
        side_str = str(side.value).upper()
    else:
        side_str = str(side).upper()

    if side_str in ("BUY", "BID", "LONG", "BUY/LONG"):
        return OrderBookSide.BUY
    return OrderBookSide.SELL


@dataclass
class DepthLevel:
    price: float
    size: float  # Base asset quantity
    orders_count: int = 1
    timestamp_us: int = 0

    @property
    def notional_usd(self) -> float:
        return self.price * self.size


@dataclass
class VWAPQuote:
    vwap_price: float
    total_filled_usd: float
    total_filled_qty: float
    avg_slippage_bps: float
    depth_exhausted: bool
    levels_traversed: int
    marginal_price: float


class MicrostructureDepthBook:
    """
    Ultra-low latency in-memory L2/L3 Orderbook with microsecond timestamping
    and delta update handling.
    
    Bids are maintained sorted descending by price (highest bid first).
    Asks are maintained sorted ascending by price (lowest ask first).
    """

    def __init__(self, market_index: int = 0, symbol: str = "ETH"):
        self.market_index = market_index
        self.symbol = symbol.upper()
        # Fast lookup by price: float -> DepthLevel
        self.bids_map: Dict[float, DepthLevel] = {}
        self.asks_map: Dict[float, DepthLevel] = {}
        # Sorted price lists
        self.sorted_bid_prices: List[float] = []  # descending
        self.sorted_ask_prices: List[float] = []  # ascending
        self.last_update_us: int = int(time.time() * 1_000_000)
        self.nonce: int = 0
        self.last_delta_latency_us: float = 0.0

    @property
    def best_bid(self) -> float:
        return self.sorted_bid_prices[0] if self.sorted_bid_prices else 0.0

    @property
    def best_bid_size(self) -> float:
        if not self.sorted_bid_prices:
            return 0.0
        return self.bids_map[self.sorted_bid_prices[0]].size

    @property
    def best_ask(self) -> float:
        return self.sorted_ask_prices[0] if self.sorted_ask_prices else float("inf")

    @property
    def best_ask_size(self) -> float:
        if not self.sorted_ask_prices:
            return 0.0
        return self.asks_map[self.sorted_ask_prices[0]].size

    @property
    def mid_price(self) -> float:
        bb = self.best_bid
        ba = self.best_ask
        if bb > 0 and ba < float("inf"):
            return (bb + ba) / 2.0
        if bb > 0:
            return bb
        if ba < float("inf"):
            return ba
        return 0.0

    @property
    def spread(self) -> float:
        bb = self.best_bid
        ba = self.best_ask
        if bb > 0 and ba < float("inf"):
            return max(0.0, ba - bb)
        return 0.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        if mid > 0:
            return (self.spread / mid) * 10_000.0
        return 0.0

    def load_snapshot(
        self,
        bids: List[Union[Tuple[float, float], Dict[str, Any], Any]],
        asks: List[Union[Tuple[float, float], Dict[str, Any], Any]],
        nonce: int = 0,
        timestamp_us: Optional[int] = None,
    ) -> None:
        """Loads a full orderbook snapshot and resets delta tracking."""
        now_us = timestamp_us if timestamp_us is not None else int(time.time() * 1_000_000)
        self.bids_map.clear()
        self.asks_map.clear()

        # Parse bids
        for item in bids:
            p, s = self._parse_level(item)
            if p > 0 and s > 0:
                self.bids_map[p] = DepthLevel(price=p, size=s, timestamp_us=now_us)

        # Parse asks
        for item in asks:
            p, s = self._parse_level(item)
            if p > 0 and s > 0:
                self.asks_map[p] = DepthLevel(price=p, size=s, timestamp_us=now_us)

        self.sorted_bid_prices = sorted(self.bids_map.keys(), reverse=True)
        self.sorted_ask_prices = sorted(self.asks_map.keys(), reverse=False)
        self.nonce = nonce
        self.last_update_us = now_us

    def apply_delta(
        self,
        side: Union[str, OrderBookSide, Any],
        price: float,
        size: float,
        orders_count: int = 1,
        timestamp_us: Optional[int] = None,
    ) -> None:
        """
        Applies a microsecond delta update to the orderbook.
        If size <= 0: removes the price level.
        If size > 0: inserts or updates the price level while preserving sort order.
        """
        now_us = timestamp_us if timestamp_us is not None else int(time.time() * 1_000_000)
        self.last_delta_latency_us = max(0.0, (now_us - self.last_update_us) if timestamp_us else 0.0)
        self.last_update_us = now_us

        norm_side = normalize_side(side)
        price = float(price)
        size = float(size)

        if norm_side == OrderBookSide.BUY:
            prices_list = self.sorted_bid_prices
            levels_map = self.bids_map
            descending = True
        else:
            prices_list = self.sorted_ask_prices
            levels_map = self.asks_map
            descending = False

        if size <= 1e-12:
            # Removal
            if price in levels_map:
                del levels_map[price]
                # Remove from sorted list
                self._remove_price(prices_list, price, descending)
        else:
            # Insert / Update
            if price in levels_map:
                level = levels_map[price]
                level.size = size
                level.orders_count = orders_count
                level.timestamp_us = now_us
            else:
                levels_map[price] = DepthLevel(
                    price=price,
                    size=size,
                    orders_count=orders_count,
                    timestamp_us=now_us,
                )
                self._insert_price(prices_list, price, descending)

    def _insert_price(self, prices_list: List[float], price: float, descending: bool) -> None:
        """Binary search insert into sorted price list."""
        if descending:
            # List is sorted descending
            lo = 0
            hi = len(prices_list)
            while lo < hi:
                mid = (lo + hi) // 2
                if prices_list[mid] < price:
                    hi = mid
                else:
                    lo = mid + 1
            prices_list.insert(lo, price)
        else:
            # List is sorted ascending
            idx = bisect_left(prices_list, price)
            prices_list.insert(idx, price)

    def _remove_price(self, prices_list: List[float], price: float, descending: bool) -> None:
        """Binary search removal from sorted price list."""
        if not prices_list:
            return
        if descending:
            lo = 0
            hi = len(prices_list)
            while lo < hi:
                mid = (lo + hi) // 2
                if prices_list[mid] > price:
                    lo = mid + 1
                elif prices_list[mid] < price:
                    hi = mid
                else:
                    del prices_list[mid]
                    return
        else:
            idx = bisect_left(prices_list, price)
            if idx < len(prices_list) and prices_list[idx] == price:
                del prices_list[idx]

    def _parse_level(self, item: Any) -> Tuple[float, float]:
        """Extracts (price, size) from diverse orderbook level formats."""
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            return float(item[0]), float(item[1])
        if isinstance(item, dict):
            return float(item.get("price", 0.0)), float(item.get("size", item.get("amount", 0.0)))
        if hasattr(item, "price") and hasattr(item, "size"):
            return float(item.price), float(item.size)
        return 0.0, 0.0

    def get_bids(self, depth: Optional[int] = None) -> List[DepthLevel]:
        """Returns top N bid levels (sorted descending)."""
        limit = len(self.sorted_bid_prices) if depth is None else min(depth, len(self.sorted_bid_prices))
        return [self.bids_map[p] for p in self.sorted_bid_prices[:limit]]

    def get_asks(self, depth: Optional[int] = None) -> List[DepthLevel]:
        """Returns top N ask levels (sorted ascending)."""
        limit = len(self.sorted_ask_prices) if depth is None else min(depth, len(self.sorted_ask_prices))
        return [self.asks_map[p] for p in self.sorted_ask_prices[:limit]]

    def get_total_depth_usd(self, side: Union[str, OrderBookSide, Any], max_depth_levels: int = 50) -> float:
        """Calculates total available USD liquidity up to N levels on a given side."""
        norm_side = normalize_side(side)
        levels = self.get_bids(max_depth_levels) if norm_side == OrderBookSide.BUY else self.get_asks(max_depth_levels)
        return sum(lvl.notional_usd for lvl in levels)

    def calculate_micro_price(self) -> float:
        """
        Calculates volume-weighted micro-price:
        S_micro = (V_ask * P_bid + V_bid * P_ask) / (V_bid + V_ask)
        """
        bb = self.best_bid
        ba = self.best_ask
        if bb <= 0 or ba == float("inf"):
            return self.mid_price

        vb = self.best_bid_size
        va = self.best_ask_size
        total_v = vb + va
        if total_v <= 0:
            return (bb + ba) / 2.0
        return (vb * ba + va * bb) / total_v

    def calculate_order_book_imbalance(self, depth: int = 5) -> float:
        """
        Calculates Order Book Imbalance (OBI) metric:
        OBI = (Bid_Volume - Ask_Volume) / (Bid_Volume + Ask_Volume)
        Ranges from -1.0 (pure selling pressure) to +1.0 (pure buying pressure).
        """
        bids = self.get_bids(depth)
        asks = self.get_asks(depth)
        total_bid_v = sum(lvl.size for lvl in bids)
        total_ask_v = sum(lvl.size for lvl in asks)
        total_v = total_bid_v + total_ask_v
        if total_v <= 0:
            return 0.0
        return (total_bid_v - total_ask_v) / total_v


# =============================================================================
# VWAP & LIQUIDITY SIZING CORE FUNCTIONS
# =============================================================================

def calculate_vwap(
    orderbook: Union[MicrostructureDepthBook, Any],
    side: Union[str, OrderBookSide, Any],
    target_notional_usd: float,
    fallback_price: Optional[float] = None,
) -> Tuple[float, float, float, bool]:
    """
    Computes the Volume-Weighted Average Price (VWAP) for filling target_notional_usd.
    
    Parameters:
    - orderbook: MicrostructureDepthBook or any object with .asks / .bids (or .get_asks / .get_bids)
    - side: "BUY" (walks asks up) or "SELL" (walks bids down)
    - target_notional_usd: Desired fill size in USD
    - fallback_price: Optional top-of-book fallback price if depth is empty
    
    Returns:
    - (vwap_price, total_filled_usd, avg_slippage_bps, depth_exhausted)
    """
    norm_side = normalize_side(side)
    target_usd = float(target_notional_usd)

    if target_usd <= 0:
        return 0.0, 0.0, 0.0, False

    # Extract levels
    if isinstance(orderbook, MicrostructureDepthBook):
        levels = orderbook.get_asks() if norm_side == OrderBookSide.BUY else orderbook.get_bids()
    elif hasattr(orderbook, "get_asks") and hasattr(orderbook, "get_bids"):
        levels = orderbook.get_asks() if norm_side == OrderBookSide.BUY else orderbook.get_bids()
    else:
        # Fallback to duck-typing for L2OrderBook / custom dicts
        raw_levels = getattr(orderbook, "asks" if norm_side == OrderBookSide.BUY else "bids", [])
        levels = []
        for item in raw_levels:
            if hasattr(item, "price") and hasattr(item, "size"):
                levels.append(DepthLevel(price=float(item.price), size=float(item.size)))
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                levels.append(DepthLevel(price=float(item[0]), size=float(item[1])))
            elif isinstance(item, dict):
                levels.append(DepthLevel(price=float(item["price"]), size=float(item["size"])))

    # Empty depth handler with top-of-book fallback
    if not levels:
        fb_price = fallback_price or getattr(orderbook, "mid_price", 0.0) or 0.0
        if fb_price > 0:
            return fb_price, target_usd, 0.0, True
        return 0.0, 0.0, 0.0, True

    best_price = levels[0].price
    if best_price <= 0:
        return 0.0, 0.0, 0.0, True

    remaining_usd = target_usd
    total_filled_usd = 0.0
    total_filled_qty = 0.0

    for lvl in levels:
        p = lvl.price
        s = lvl.size
        if p <= 0 or s <= 0:
            continue

        level_usd = p * s
        if remaining_usd <= level_usd:
            # Partial level fill to complete target
            fill_qty = remaining_usd / p
            total_filled_qty += fill_qty
            total_filled_usd += remaining_usd
            remaining_usd = 0.0
            break
        else:
            # Full level fill
            total_filled_qty += s
            total_filled_usd += level_usd
            remaining_usd -= level_usd

    if total_filled_qty <= 0 or total_filled_usd <= 0:
        return best_price, 0.0, 0.0, True

    vwap_price = total_filled_usd / total_filled_qty
    depth_exhausted = remaining_usd > 1e-6

    # Calculate slippage relative to best top-of-book price in basis points
    if norm_side == OrderBookSide.BUY:
        avg_slippage_bps = ((vwap_price - best_price) / best_price) * 10_000.0
    else:
        avg_slippage_bps = ((best_price - vwap_price) / best_price) * 10_000.0

    # Ensure non-negative numerical stability
    avg_slippage_bps = max(0.0, avg_slippage_bps)

    return vwap_price, total_filled_usd, avg_slippage_bps, depth_exhausted


def liquidity_adjusted_size(
    orderbook: Union[MicrostructureDepthBook, Any],
    side: Union[str, OrderBookSide, Any],
    requested_usd: float,
    max_slippage_bps: float = 50.0,
    fallback_price: Optional[float] = None,
) -> float:
    """
    Calculates the maximum executable USD order size that strictly does NOT exceed
    the max_slippage_bps cap across available depth levels.
    
    If orderbook depth is thin or empty, falls back to top-of-book single-level capacity
    or conservative sizing.
    
    Parameters:
    - orderbook: MicrostructureDepthBook or L2OrderBook
    - side: "BUY" or "SELL"
    - requested_usd: Desired trade size in USD
    - max_slippage_bps: Maximum allowable slippage in basis points (e.g. 50 bps = 0.50%)
    - fallback_price: Optional fallback price
    
    Returns:
    - adjusted_usd_size: Safe executable USD notional <= requested_usd
    """
    req_usd = float(requested_usd)
    max_slip = max(0.0, float(max_slippage_bps))
    if req_usd <= 0:
        return 0.0

    norm_side = normalize_side(side)

    # Extract levels
    if isinstance(orderbook, MicrostructureDepthBook):
        levels = orderbook.get_asks() if norm_side == OrderBookSide.BUY else orderbook.get_bids()
    elif hasattr(orderbook, "get_asks") and hasattr(orderbook, "get_bids"):
        levels = orderbook.get_asks() if norm_side == OrderBookSide.BUY else orderbook.get_bids()
    else:
        raw_levels = getattr(orderbook, "asks" if norm_side == OrderBookSide.BUY else "bids", [])
        levels = []
        for item in raw_levels:
            if hasattr(item, "price") and hasattr(item, "size"):
                levels.append(DepthLevel(price=float(item.price), size=float(item.size)))
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                levels.append(DepthLevel(price=float(item[0]), size=float(item[1])))
            elif isinstance(item, dict):
                levels.append(DepthLevel(price=float(item["price"]), size=float(item["size"])))

    # Empty depth fallback
    if not levels:
        return req_usd

    best_price = levels[0].price
    if best_price <= 0:
        return 0.0

    # Theoretical limit price for maximum allowable VWAP
    if norm_side == OrderBookSide.BUY:
        target_max_vwap = best_price * (1.0 + (max_slip / 10_000.0))
    else:
        target_max_vwap = best_price * (1.0 - (max_slip / 10_000.0))

    accum_usd = 0.0
    accum_qty = 0.0

    for lvl in levels:
        p = lvl.price
        s = lvl.size
        if p <= 0 or s <= 0:
            continue

        level_usd = p * s
        test_usd = accum_usd + level_usd
        test_qty = accum_qty + s
        test_vwap = test_usd / test_qty

        # Check if full level can be absorbed
        within_slippage = (test_vwap <= target_max_vwap) if norm_side == OrderBookSide.BUY else (test_vwap >= target_max_vwap)

        if within_slippage:
            if test_usd >= req_usd:
                # Requested size completely satisfied within slippage limits!
                return req_usd
            accum_usd = test_usd
            accum_qty = test_qty
        else:
            # Slippage boundary crossed at this level.
            # Solve for exact partial quantity delta_q such that VWAP == target_max_vwap:
            # (accum_usd + p * dq) / (accum_qty + dq) = target_max_vwap
            # accum_usd + p * dq = target_max_vwap * accum_qty + target_max_vwap * dq
            # dq * (p - target_max_vwap) = target_max_vwap * accum_qty - accum_usd
            # dq = (target_max_vwap * accum_qty - accum_usd) / (p - target_max_vwap)
            denominator = (p - target_max_vwap) if norm_side == OrderBookSide.BUY else (target_max_vwap - p)
            if abs(denominator) > 1e-9:
                if norm_side == OrderBookSide.BUY:
                    dq = (target_max_vwap * accum_qty - accum_usd) / denominator
                else:
                    dq = (accum_usd - target_max_vwap * accum_qty) / denominator
                
                dq = max(0.0, min(s, dq))
                max_executable_usd = accum_usd + (dq * p)
                return min(req_usd, max(0.0, max_executable_usd))
            else:
                return min(req_usd, accum_usd)

    # Exhausted all depth levels without breaching slippage cap
    return min(req_usd, accum_usd)


# =============================================================================
# LIGHTER ADAPTER & REGISTRY
# =============================================================================

class DepthVWAPEngine:
    """
    Multi-market Depth and VWAP Management Engine.
    Coordinates live depth orderbooks across multiple DEX market indices.
    """

    def __init__(self):
        self.books: Dict[int, MicrostructureDepthBook] = {}

    def get_or_create_book(self, market_index: int = 0, symbol: str = "ETH") -> MicrostructureDepthBook:
        if market_index not in self.books:
            self.books[market_index] = MicrostructureDepthBook(market_index=market_index, symbol=symbol)
        return self.books[market_index]

    def update_from_raw_l2(self, market_index: int, bids: List[Any], asks: List[Any], nonce: int = 0) -> MicrostructureDepthBook:
        """Updates book from Lighter REST/WS L2 book arrays."""
        book = self.get_or_create_book(market_index)
        book.load_snapshot(bids=bids, asks=asks, nonce=nonce)
        return book

    def calculate_sizing_and_vwap(
        self,
        market_index: int,
        side: Union[str, OrderBookSide, Any],
        requested_usd: float,
        max_slippage_bps: float = 50.0,
        fallback_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        One-stop calculation returning adjusted executable size, VWAP price,
        expected slippage bps, and depth exhaustion flag.
        """
        book = self.get_or_create_book(market_index)
        adj_size_usd = liquidity_adjusted_size(
            orderbook=book,
            side=side,
            requested_usd=requested_usd,
            max_slippage_bps=max_slippage_bps,
            fallback_price=fallback_price,
        )

        vwap_price, filled_usd, slippage_bps, depth_exhausted = calculate_vwap(
            orderbook=book,
            side=side,
            target_notional_usd=adj_size_usd,
            fallback_price=fallback_price,
        )

        best_price = book.best_ask if normalize_side(side) == OrderBookSide.BUY else book.best_bid
        if best_price <= 0 and fallback_price:
            best_price = fallback_price

        base_qty = filled_usd / vwap_price if vwap_price > 0 else 0.0

        return {
            "market_index": market_index,
            "side": normalize_side(side).value,
            "requested_usd": requested_usd,
            "executable_usd": adj_size_usd,
            "base_qty": base_qty,
            "vwap_price": vwap_price,
            "best_price": best_price,
            "expected_slippage_bps": slippage_bps,
            "max_slippage_bps": max_slippage_bps,
            "depth_exhausted": depth_exhausted,
            "spread_bps": book.spread_bps,
            "order_book_imbalance": book.calculate_order_book_imbalance(),
        }


# Global singleton instance for easy import across modules
global_depth_vwap_engine = DepthVWAPEngine()
