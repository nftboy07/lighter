#!/usr/bin/env python3
"""
Whale Liquidity Wall Shadowing & Structural Protection (whale_orderbook_shadow.py)
==================================================================================
Scans orderbook depth for institutional bid/ask liquidity walls (>= $50k..$250k),
filters out fleeting spoof quotes, and generates asymmetric front-run entries
with tight structural Stop-Loss cushion behind the wall.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("WhaleShadow")


class WallSide(str, Enum):
    BID = "BID"
    ASK = "ASK"


@dataclass
class LiquidityWall:
    """Detected institutional orderbook wall."""
    symbol: str
    side: WallSide
    price: float
    size: float
    notional_usd: float
    first_seen_at: float
    last_seen_at: float
    updates_count: int = 1
    is_confirmed: bool = False  # Confirmed if wall persists > min_duration_sec

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.first_seen_at)


@dataclass(frozen=True)
class WhaleShadowSetup:
    """Actionable trade setup front-running a confirmed whale wall."""
    symbol: str
    action: str  # "BUY/LONG" (front-running bid wall) or "SELL/SHORT" (front-running ask wall)
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    risk_pct: float
    reward_pct: float
    risk_reward_ratio: float
    wall_notional_usd: float
    wall_price: float
    timestamp: float = field(default_factory=time.time)


class WhaleOrderBookShadowEngine:
    """
    Scans L2/L3 orderbooks, identifies structural whale support/resistance walls,
    and calculates front-run entries with tight stops.
    """

    def __init__(
        self,
        min_wall_usd: float = 25000.0,
        min_wall_duration_sec: float = 2.0,
        front_run_ticks: int = 1,
        stop_cushion_pct: float = 0.20,
        target_tp_pct: float = 1.50,
    ):
        self.min_wall_usd = min_wall_usd
        self.min_wall_duration_sec = min_wall_duration_sec
        self.front_run_ticks = front_run_ticks
        self.stop_cushion_pct = stop_cushion_pct
        self.target_tp_pct = target_tp_pct

        # Active tracked walls: (symbol, side, price) -> LiquidityWall
        self.tracked_walls: Dict[Tuple[str, str, float], LiquidityWall] = {}

    def scan_orderbook(
        self,
        symbol: str,
        bids: List[Tuple[float, float]],  # List of (price, size)
        asks: List[Tuple[float, float]],  # List of (price, size)
        tick_size: float = 0.01,
        now: Optional[float] = None,
    ) -> List[WhaleShadowSetup]:
        """
        Scans depth levels for whale walls and returns actionable setups.
        """
        ts = now if now is not None else time.time()
        sym = symbol.upper()
        current_wall_keys = set()
        setups: List[WhaleShadowSetup] = []

        # 1. Scan Bids
        for price, size in bids:
            notional = price * size
            if notional >= self.min_wall_usd:
                key = (sym, WallSide.BID.value, price)
                current_wall_keys.add(key)
                if key in self.tracked_walls:
                    wall = self.tracked_walls[key]
                    wall.last_seen_at = ts
                    wall.size = size
                    wall.notional_usd = notional
                    wall.updates_count += 1
                    if not wall.is_confirmed and (ts - wall.first_seen_at) >= self.min_wall_duration_sec:
                        wall.is_confirmed = True
                else:
                    self.tracked_walls[key] = LiquidityWall(
                        symbol=sym,
                        side=WallSide.BID,
                        price=price,
                        size=size,
                        notional_usd=notional,
                        first_seen_at=ts,
                        last_seen_at=ts,
                    )

                wall = self.tracked_walls[key]
                if wall.is_confirmed:
                    # Front-run BUY setup
                    entry = price + (self.front_run_ticks * tick_size)
                    sl = price * (1.0 - self.stop_cushion_pct / 100.0)
                    tp = entry * (1.0 + self.target_tp_pct / 100.0)
                    risk = (entry - sl) / entry * 100.0
                    reward = (tp - entry) / entry * 100.0
                    rr = reward / max(0.01, risk)
                    setups.append(WhaleShadowSetup(
                        symbol=sym,
                        action="BUY/LONG",
                        entry_price=round(entry, 4),
                        stop_loss_price=round(sl, 4),
                        take_profit_price=round(tp, 4),
                        risk_pct=round(risk, 3),
                        reward_pct=round(reward, 3),
                        risk_reward_ratio=round(rr, 2),
                        wall_notional_usd=round(notional, 2),
                        wall_price=price,
                    ))

        # 2. Scan Asks
        for price, size in asks:
            notional = price * size
            if notional >= self.min_wall_usd:
                key = (sym, WallSide.ASK.value, price)
                current_wall_keys.add(key)
                if key in self.tracked_walls:
                    wall = self.tracked_walls[key]
                    wall.last_seen_at = ts
                    wall.size = size
                    wall.notional_usd = notional
                    wall.updates_count += 1
                    if not wall.is_confirmed and (ts - wall.first_seen_at) >= self.min_wall_duration_sec:
                        wall.is_confirmed = True
                else:
                    self.tracked_walls[key] = LiquidityWall(
                        symbol=sym,
                        side=WallSide.ASK,
                        price=price,
                        size=size,
                        notional_usd=notional,
                        first_seen_at=ts,
                        last_seen_at=ts,
                    )

                wall = self.tracked_walls[key]
                if wall.is_confirmed:
                    # Front-run SELL/SHORT setup
                    entry = price - (self.front_run_ticks * tick_size)
                    sl = price * (1.0 + self.stop_cushion_pct / 100.0)
                    tp = entry * (1.0 - self.target_tp_pct / 100.0)
                    risk = (sl - entry) / entry * 100.0
                    reward = (entry - tp) / entry * 100.0
                    rr = reward / max(0.01, risk)
                    setups.append(WhaleShadowSetup(
                        symbol=sym,
                        action="SELL/SHORT",
                        entry_price=round(entry, 4),
                        stop_loss_price=round(sl, 4),
                        take_profit_price=round(tp, 4),
                        risk_pct=round(risk, 3),
                        reward_pct=round(reward, 3),
                        risk_reward_ratio=round(rr, 2),
                        wall_notional_usd=round(notional, 2),
                        wall_price=price,
                    ))

        # 3. Clean up pulled or stale walls
        for k in list(self.tracked_walls.keys()):
            if k[0] == sym and k not in current_wall_keys:
                del self.tracked_walls[k]

        return setups
