#!/usr/bin/env python3
"""
Multi-Market Simultaneous 0-Fee Grid Quoter (multi_market_grid_quoter.py)
========================================================================
Deploys and manages dynamic 0-fee geometric quoting grids across the top 5
zkLighter Perpetual markets simultaneously (ETH, BTC, SOL, TRUMP, HYPE),
maximizing maker volume and Robinhood points under unified capital limits.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from dynamic_grid_mm import DynamicGridMMEngine, GridState

logger = logging.getLogger("MultiMarketGridQuoter")


@dataclass
class MarketQuotingSession:
    """Individual active quoting session for a specific perp market."""
    symbol: str
    market_index: int
    allocated_collateral_usd: float
    current_inventory_usd: float = 0.0
    total_volume_farmed_usd: float = 0.0
    active_grid: Optional[GridState] = None
    is_paused: bool = False
    last_update: float = field(default_factory=time.time)


class MultiMarketGridQuoterEngine:
    """
    Coordinates concurrent market making across multiple zkLighter books using Subaccount #281474976497685.
    """

    TOP_5_MARKETS = {
        "ETH": 0,
        "BTC": 1,
        "SOL": 2,
        "TRUMP": 3,
        "HYPE": 4,
    }

    def __init__(
        self,
        total_mm_collateral_usd: float = 250.0,
        per_market_allocation_pct: float = 20.0,  # 20% each across 5 markets = $50 each
        base_grid_layers: int = 5,
    ):
        self.total_mm_collateral_usd = total_mm_collateral_usd
        self.per_market_allocation_pct = per_market_allocation_pct
        self.base_grid_layers = base_grid_layers

        self.grid_engines: Dict[str, DynamicGridMMEngine] = {}
        self.sessions: Dict[str, MarketQuotingSession] = {}

        # Initialize quoting sessions for top 5 pairs
        per_market_cap = (total_mm_collateral_usd * per_market_allocation_pct) / 100.0
        for sym, m_idx in self.TOP_5_MARKETS.items():
            self.grid_engines[sym] = DynamicGridMMEngine(
                base_layer_size_usd=max(5.0, per_market_cap / 10.0),
                num_layers=base_grid_layers,
            )
            self.sessions[sym] = MarketQuotingSession(
                symbol=sym,
                market_index=m_idx,
                allocated_collateral_usd=per_market_cap,
            )

    def update_market_quote(
        self,
        symbol: str,
        mid_price: float,
        atr_multiplier: float = 1.0,
        current_inventory_usd: float = 0.0,
    ) -> Optional[GridState]:
        """
        Updates dynamic grid for a specific market.
        """
        sym = symbol.upper()
        session = self.sessions.get(sym)
        engine = self.grid_engines.get(sym)

        if not session or not engine or session.is_paused or mid_price <= 0:
            return None

        session.current_inventory_usd = current_inventory_usd
        grid = engine.generate_grid(
            symbol=sym,
            mid_price=mid_price,
            atr_multiplier=atr_multiplier,
            current_inventory_usd=current_inventory_usd,
            max_position_usd=session.allocated_collateral_usd,
        )
        session.active_grid = grid
        session.last_update = time.time()
        return grid

    def record_fill_volume(self, symbol: str, filled_notional_usd: float) -> None:
        """Records farmed maker volume for points accounting."""
        sym = symbol.upper()
        session = self.sessions.get(sym)
        if session:
            session.total_volume_farmed_usd += filled_notional_usd

    def get_multi_market_summary(self) -> Dict[str, Any]:
        """Returns consolidated multi-market quoting metrics."""
        total_vol = sum(s.total_volume_farmed_usd for s in self.sessions.values())
        active_markets = [s.symbol for s in self.sessions.values() if s.active_grid is not None and not s.is_paused]

        return {
            "total_markets_tracked": len(self.sessions),
            "active_quoting_markets": active_markets,
            "total_farmed_volume_usd": round(total_vol, 2),
            "sessions": {
                sym: {
                    "market_index": s.market_index,
                    "allocated_collateral_usd": round(s.allocated_collateral_usd, 2),
                    "current_inventory_usd": round(s.current_inventory_usd, 2),
                    "total_volume_usd": round(s.total_volume_farmed_usd, 2),
                    "is_active": s.active_grid is not None and not s.is_paused,
                }
                for sym, s in self.sessions.items()
            },
        }
