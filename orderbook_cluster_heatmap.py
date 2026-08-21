#!/usr/bin/env python3
"""
Orderbook Liquidity Cluster & Magnet Target Engine (orderbook_cluster_heatmap.py)
================================================================================
Aggregates top 100 levels of L2 depth into institutional liquidity clusters
($50k-$250k) to identify magnetic take-profit targets and stop placement zones.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("LiquidityClusters")


@dataclass
class LiquidityCluster:
    """A high-density price zone with accumulated resting liquidity."""
    price_center: float
    price_min: float
    price_max: float
    side: str                          # "BID" or "ASK"
    total_notional_usd: float
    cluster_strength_score: float      # Score based on volume concentration
    is_major_wall: bool                # Notional >= $50,000 USD


@dataclass
class OrderbookClusterSummary:
    """Consolidated liquidity clusters and magnet targets."""
    symbol: str
    mid_price: float
    nearest_bid_magnet_price: float
    nearest_ask_magnet_price: float
    bid_clusters: List[LiquidityCluster]
    ask_clusters: List[LiquidityCluster]
    recommended_long_tp_price: float
    recommended_short_tp_price: float
    timestamp: float = field(default_factory=time.time)


class OrderbookClusterEngine:
    """
    Groups resting limit orders into discrete price density clusters.
    """

    def __init__(
        self,
        cluster_bin_pct: float = 0.25,         # Group orders within 0.25% price bands
        min_cluster_usd: float = 10000.0,      # Minimum $10k to form a cluster
        major_wall_usd: float = 50000.0,       # $50k+ = Major Institutional Wall
    ):
        self.cluster_bin_pct = cluster_bin_pct
        self.min_cluster_usd = min_cluster_usd
        self.major_wall_usd = major_wall_usd

    def cluster_orderbook(
        self,
        symbol: str,
        bids: List[Tuple[float, float]],       # List of (price, size)
        asks: List[Tuple[float, float]],       # List of (price, size)
        mid_price: float,
    ) -> OrderbookClusterSummary:
        """
        Groups bids and asks into liquidity clusters and calculates optimal TP magnets.
        """
        sym = symbol.upper()

        bid_clusters = self._cluster_side(bids, "BID", mid_price)
        ask_clusters = self._cluster_side(asks, "ASK", mid_price)

        # Identify nearest magnet prices
        nearest_ask = ask_clusters[0].price_center if ask_clusters else (mid_price * 1.02)
        nearest_bid = bid_clusters[0].price_center if bid_clusters else (mid_price * 0.98)

        # Optimal TP targets (placed slightly inside the magnet wall for 100% fill)
        long_tp = round(nearest_ask * 0.9995, 4)
        short_tp = round(nearest_bid * 1.0005, 4)

        return OrderbookClusterSummary(
            symbol=sym,
            mid_price=mid_price,
            nearest_bid_magnet_price=nearest_bid,
            nearest_ask_magnet_price=nearest_ask,
            bid_clusters=bid_clusters,
            ask_clusters=ask_clusters,
            recommended_long_tp_price=long_tp,
            recommended_short_tp_price=short_tp,
        )

    def _cluster_side(
        self,
        orders: List[Tuple[float, float]],
        side: str,
        mid_price: float,
    ) -> List[LiquidityCluster]:
        """Helper to cluster orders along one side of the orderbook."""
        if not orders:
            return []

        clusters: List[LiquidityCluster] = []
        bin_size = mid_price * (self.cluster_bin_pct / 100.0)

        # Sort orders appropriately
        sorted_orders = sorted(orders, key=lambda x: x[0], reverse=(side == "BID"))

        current_bin_orders: List[Tuple[float, float]] = []
        bin_anchor_px = sorted_orders[0][0] if sorted_orders else mid_price

        for px, qty in sorted_orders:
            if abs(px - bin_anchor_px) <= bin_size:
                current_bin_orders.append((px, qty))
            else:
                # Close current bin
                self._add_cluster_if_valid(clusters, current_bin_orders, side)
                current_bin_orders = [(px, qty)]
                bin_anchor_px = px

        if current_bin_orders:
            self._add_cluster_if_valid(clusters, current_bin_orders, side)

        # Sort clusters by notional size
        clusters.sort(key=lambda c: c.total_notional_usd, reverse=True)
        return clusters

    def _add_cluster_if_valid(
        self,
        clusters: List[LiquidityCluster],
        orders: List[Tuple[float, float]],
        side: str,
    ) -> None:
        if not orders:
            return
        total_usd = sum(p * q for p, q in orders)
        if total_usd >= self.min_cluster_usd:
            prices = [p for p, _ in orders]
            avg_px = sum(p * (p * q) for p, q in orders) / total_usd
            clusters.append(
                LiquidityCluster(
                    price_center=round(avg_px, 4),
                    price_min=min(prices),
                    price_max=max(prices),
                    side=side,
                    total_notional_usd=round(total_usd, 2),
                    cluster_strength_score=round(total_usd / self.major_wall_usd, 2),
                    is_major_wall=total_usd >= self.major_wall_usd,
                )
            )
