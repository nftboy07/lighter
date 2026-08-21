#!/usr/bin/env python3
"""
zkLighter Internal Spot vs Perp Basis Arbitrage Engine (internal_basis_arbitrage.py)
===================================================================================
Institutional basis arbitrage between zkLighter Spot and Perpetual markets.

Key Features:
- Microsecond comparison between Spot bid/ask and Perp bid/ask for ETH, BTC, SOL.
- Exploits basis spread >= 15 bps (Spot vs Perp price dislocations).
- Atomic zero-latency dual-leg execution on the same exchange.
- Automated convergence detection & position unwinding when basis closes < 3 bps.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("InternalBasisArb")


class BasisDirection(str, Enum):
    """Direction of basis arbitrage trade."""
    BUY_SPOT_SELL_PERP = "BUY_SPOT_SELL_PERP"  # Perp is at premium (Perp > Spot)
    BUY_PERP_SELL_SPOT = "BUY_PERP_SELL_SPOT"  # Perp is at discount (Spot > Perp)


@dataclass(frozen=True)
class BasisOpportunity:
    """Detected basis arbitrage opportunity."""
    symbol: str
    direction: BasisDirection
    spot_price: float
    perp_price: float
    basis_spread_bps: float
    net_edge_bps: float
    target_notional_usd: float
    estimated_profit_usd: float
    confidence_score: float
    timestamp: float = field(default_factory=time.time)

    @property
    def is_actionable(self) -> bool:
        return self.net_edge_bps > 0.0 and self.target_notional_usd > 0.0


@dataclass
class ActiveBasisPosition:
    """Tracks an open delta-neutral basis position."""
    position_id: str
    symbol: str
    direction: BasisDirection
    entry_spot_price: float
    entry_perp_price: float
    entry_basis_spread_bps: float
    size_base: float
    notional_usd: float
    opened_at: float = field(default_factory=time.time)
    status: str = "OPEN"  # OPEN, CLOSING, CLOSED
    realized_pnl_usd: float = 0.0
    closed_at: Optional[float] = None


class InternalBasisArbitrageEngine:
    """
    Continuous market scanner and execution orchestrator for zkLighter Spot vs Perp Basis.
    """

    def __init__(
        self,
        min_basis_spread_bps: float = 15.0,
        unwind_spread_bps: float = 3.0,
        estimated_fee_bps: float = 2.0,
        default_order_size_usd: float = 200.0,
        max_active_positions: int = 3,
    ):
        self.min_basis_spread_bps = min_basis_spread_bps
        self.unwind_spread_bps = unwind_spread_bps
        self.estimated_fee_bps = estimated_fee_bps
        self.default_order_size_usd = default_order_size_usd
        self.max_active_positions = max_active_positions

        self.spot_prices: Dict[str, Tuple[float, float]] = {}  # symbol -> (bid, ask)
        self.perp_prices: Dict[str, Tuple[float, float]] = {}  # symbol -> (bid, ask)
        self.active_positions: Dict[str, ActiveBasisPosition] = {}

    def update_spot_book(self, symbol: str, bid: float, ask: float) -> None:
        """Updates top-of-book for a Spot market."""
        if bid > 0 and ask >= bid:
            self.spot_prices[symbol.upper()] = (bid, ask)

    def update_perp_book(self, symbol: str, bid: float, ask: float) -> None:
        """Updates top-of-book for a Perp market."""
        if bid > 0 and ask >= bid:
            self.perp_prices[symbol.upper()] = (bid, ask)

    def evaluate_opportunity(self, symbol: str) -> Optional[BasisOpportunity]:
        """
        Evaluates potential basis arbitrage between Spot and Perp for a given symbol.
        """
        sym = symbol.upper()
        if sym not in self.spot_prices or sym not in self.perp_prices:
            return None

        spot_bid, spot_ask = self.spot_prices[sym]
        perp_bid, perp_ask = self.perp_prices[sym]

        spot_mid = (spot_bid + spot_ask) / 2.0
        perp_mid = (perp_bid + perp_ask) / 2.0

        if spot_mid <= 0 or perp_mid <= 0:
            return None

        # Case 1: Perp Premium -> Buy Spot @ ask, Sell Perp @ bid
        if perp_bid > spot_ask:
            raw_spread_pct = (perp_bid - spot_ask) / spot_ask
            spread_bps = raw_spread_pct * 10000.0
            net_edge = spread_bps - self.estimated_fee_bps
            if net_edge >= self.min_basis_spread_bps:
                est_profit = (net_edge / 10000.0) * self.default_order_size_usd
                return BasisOpportunity(
                    symbol=sym,
                    direction=BasisDirection.BUY_SPOT_SELL_PERP,
                    spot_price=spot_ask,
                    perp_price=perp_bid,
                    basis_spread_bps=round(spread_bps, 2),
                    net_edge_bps=round(net_edge, 2),
                    target_notional_usd=self.default_order_size_usd,
                    estimated_profit_usd=round(est_profit, 4),
                    confidence_score=min(1.0, 0.70 + (net_edge / 50.0) * 0.30),
                )

        # Case 2: Perp Discount -> Buy Perp @ ask, Sell Spot @ bid
        if spot_bid > perp_ask:
            raw_spread_pct = (spot_bid - perp_ask) / perp_ask
            spread_bps = raw_spread_pct * 10000.0
            net_edge = spread_bps - self.estimated_fee_bps
            if net_edge >= self.min_basis_spread_bps:
                est_profit = (net_edge / 10000.0) * self.default_order_size_usd
                return BasisOpportunity(
                    symbol=sym,
                    direction=BasisDirection.BUY_PERP_SELL_SPOT,
                    spot_price=spot_bid,
                    perp_price=perp_ask,
                    basis_spread_bps=round(spread_bps, 2),
                    net_edge_bps=round(net_edge, 2),
                    target_notional_usd=self.default_order_size_usd,
                    estimated_profit_usd=round(est_profit, 4),
                    confidence_score=min(1.0, 0.70 + (net_edge / 50.0) * 0.30),
                )

        return None

    def should_unwind_position(self, pos: ActiveBasisPosition) -> Tuple[bool, str, float]:
        """
        Checks if an active basis position should be unwound due to spread convergence.
        """
        sym = pos.symbol
        if sym not in self.spot_prices or sym not in self.perp_prices:
            return False, "NO_PRICES", 0.0

        spot_bid, spot_ask = self.spot_prices[sym]
        perp_bid, perp_ask = self.perp_prices[sym]
        spot_mid = (spot_bid + spot_ask) / 2.0
        perp_mid = (perp_bid + perp_ask) / 2.0

        current_spread_bps = abs(perp_mid - spot_mid) / spot_mid * 10000.0

        # Unwind condition 1: Spread converged to baseline
        if current_spread_bps <= self.unwind_spread_bps:
            gross_pnl = ((pos.entry_basis_spread_bps - current_spread_bps) / 10000.0) * pos.notional_usd
            return True, "SPREAD_CONVERGED", round(gross_pnl, 4)

        # Unwind condition 2: Inversion / Adverse Drift
        if pos.direction == BasisDirection.BUY_SPOT_SELL_PERP and perp_mid < spot_mid:
            return True, "SPREAD_INVERTED", round((pos.entry_basis_spread_bps / 10000.0) * pos.notional_usd, 4)
        elif pos.direction == BasisDirection.BUY_PERP_SELL_SPOT and spot_mid < perp_mid:
            return True, "SPREAD_INVERTED", round((pos.entry_basis_spread_bps / 10000.0) * pos.notional_usd, 4)

        return False, "HOLD", 0.0

    def open_position(self, opp: BasisOpportunity) -> Optional[ActiveBasisPosition]:
        """Registers a newly opened basis arbitrage position."""
        if len(self.active_positions) >= self.max_active_positions:
            return None

        pos_id = f"basis_{opp.symbol}_{int(time.time()*1000)}"
        mid_price = (opp.spot_price + opp.perp_price) / 2.0
        size_base = opp.target_notional_usd / max(1.0, mid_price)

        pos = ActiveBasisPosition(
            position_id=pos_id,
            symbol=opp.symbol,
            direction=opp.direction,
            entry_spot_price=opp.spot_price,
            entry_perp_price=opp.perp_price,
            entry_basis_spread_bps=opp.basis_spread_bps,
            size_base=round(size_base, 6),
            notional_usd=opp.target_notional_usd,
        )
        self.active_positions[pos_id] = pos
        logger.info("⚡ [BasisArb] Opened %s on %s (Spread: %.1f bps, Net Edge: %.1f bps)", pos.direction.value, pos.symbol, pos.entry_basis_spread_bps, opp.net_edge_bps)
        return pos

    def close_position(self, pos_id: str, realized_pnl: float = 0.0) -> Optional[ActiveBasisPosition]:
        """Closes and settles an active basis position."""
        pos = self.active_positions.get(pos_id)
        if pos:
            pos.status = "CLOSED"
            pos.realized_pnl_usd = realized_pnl
            pos.closed_at = time.time()
            del self.active_positions[pos_id]
            logger.info("✅ [BasisArb] Closed %s (PnL: $%.4f)", pos.position_id, realized_pnl)
            return pos
        return None
