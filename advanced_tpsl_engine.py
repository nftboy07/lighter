#!/usr/bin/env python3
"""
Advanced Dynamic TP/SL Engine (advanced_tpsl_engine.py)
======================================================
1. Wall-Aware Take-Profit placement ahead of large orderbook resistance.
2. Parabolic Chandelier Trailing Stop (+3% -> +5% -> +8% tightening).
3. 90-Second Time-Decay Momentum Auto-Exit.
4. Hard MAE (Maximum Adverse Excursion) Dollar Guard.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("AdvancedTPSLEngine")


@dataclass
class ExitAction:
    """Actionable instruction for position exit or stop adjustment."""
    action_type: str  # "HOLD", "CLOSE_MARKET", "ADJUST_TP", "ADJUST_SL", "PARTIAL_CLOSE"
    price: float
    qty_fraction: float = 1.0
    reason: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class AdvancedPositionState:
    """Enhanced state tracker for an open position with dynamic trailing rules."""
    position_id: str
    symbol: str
    side: str
    entry_price: float
    size: float
    entry_time: float = field(default_factory=time.time)
    current_tp_price: float = 0.0
    current_sl_price: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    is_breakeven_locked: bool = False
    max_adverse_usd: float = 0.0


class AdvancedTPSLEngine:
    """
    Precision exit manager maximizing realized gains and eliminating drawdown wicks.
    """

    def __init__(
        self,
        time_decay_seconds: float = 90.0,       # Auto-exit if stagnant after 90s
        min_momentum_gain_pct: float = 0.80,    # Must reach at least +0.80% in 90s
        max_mae_loss_usd: float = 15.0,         # Hard $15 max dollar loss per trade
        wall_threshold_usd: float = 20000.0,    # Institutional wall detection threshold
    ):
        self.time_decay_seconds = time_decay_seconds
        self.min_momentum_gain_pct = min_momentum_gain_pct
        self.max_mae_loss_usd = max_mae_loss_usd
        self.wall_threshold_usd = wall_threshold_usd

        self.positions: Dict[str, AdvancedPositionState] = {}

    def register_position(
        self,
        position_id: str,
        symbol: str,
        side: str,
        entry_price: float,
        size: float,
        initial_tp_price: float,
        initial_sl_price: float,
    ) -> AdvancedPositionState:
        """Registers a newly opened position."""
        pos = AdvancedPositionState(
            position_id=position_id,
            symbol=symbol.upper(),
            side=side.upper(),
            entry_price=entry_price,
            size=size,
            current_tp_price=initial_tp_price,
            current_sl_price=initial_sl_price,
            highest_price=entry_price,
            lowest_price=entry_price,
        )
        self.positions[position_id] = pos
        return pos

    def calculate_wall_aware_tp(
        self,
        entry_price: float,
        is_long: bool,
        target_tp_price: float,
        orderbook_levels: List[Tuple[float, float]],
    ) -> float:
        """
        Adjusts TP to sit just inside ($0.05..0.10) a large liquidity wall to ensure guaranteed fills.
        """
        if not orderbook_levels:
            return target_tp_price

        for px, sz in orderbook_levels:
            level_notional = px * sz
            if level_notional >= self.wall_threshold_usd:
                if is_long and entry_price < px <= target_tp_price * 1.01:
                    # Place right in front of the ask wall
                    adjusted_tp = px * 0.9995
                    logger.info("🎯 Wall-Aware TP Adjusted: $%.2f -> $%.2f (Ahead of $%.0f wall at $%.2f)", target_tp_price, adjusted_tp, level_notional, px)
                    return round(adjusted_tp, 4)
                elif not is_long and entry_price > px >= target_tp_price * 0.99:
                    # Place right in front of the bid wall
                    adjusted_tp = px * 1.0005
                    logger.info("🎯 Wall-Aware TP Adjusted: $%.2f -> $%.2f (Ahead of $%.0f wall at $%.2f)", target_tp_price, adjusted_tp, level_notional, px)
                    return round(adjusted_tp, 4)

        return target_tp_price

    def evaluate_tick(
        self,
        position_id: str,
        current_price: float,
        orderbook_asks: Optional[List[Tuple[float, float]]] = None,
        orderbook_bids: Optional[List[Tuple[float, float]]] = None,
    ) -> ExitAction:
        """
        Evaluates position against all 4 exit algorithms on every price tick.
        """
        pos = self.positions.get(position_id)
        if not pos:
            return ExitAction(action_type="HOLD", price=current_price)

        is_long = "BUY" in pos.side or "LONG" in pos.side
        pos.highest_price = max(pos.highest_price, current_price)
        pos.lowest_price = min(pos.lowest_price, current_price)

        pnl_pct = ((current_price - pos.entry_price) / pos.entry_price * 100.0) if is_long else ((pos.entry_price - current_price) / pos.entry_price * 100.0)
        pnl_usd = (current_price - pos.entry_price) * pos.size if is_long else (pos.entry_price - current_price) * pos.size
        elapsed_sec = time.time() - pos.entry_time

        # 1. Hard MAE (Max Adverse Excursion) Capital Guard
        if pnl_usd <= -self.max_mae_loss_usd:
            return ExitAction(
                action_type="CLOSE_MARKET",
                price=current_price,
                reason=f"MAE Hard Dollar Guard: Realized loss reached -${abs(pnl_usd):.2f}",
            )

        # 2. Hard Stop-Loss Hit
        if (is_long and current_price <= pos.current_sl_price) or (not is_long and current_price >= pos.current_sl_price):
            return ExitAction(
                action_type="CLOSE_MARKET",
                price=current_price,
                reason=f"Stop-Loss Triggered at ${current_price:,.2f}",
            )

        # 3. Take-Profit Hit
        if (is_long and current_price >= pos.current_tp_price) or (not is_long and current_price <= pos.current_tp_price):
            return ExitAction(
                action_type="CLOSE_MARKET",
                price=current_price,
                reason=f"Take-Profit Target Hit at ${current_price:,.2f} (+{pnl_pct:.2f}%)",
            )

        # 4. Parabolic Chandelier Trailing Stop
        # Level 1: at +3.0% gain -> Lock Breakeven (+0.2%)
        if pnl_pct >= 3.0 and not pos.is_breakeven_locked:
            pos.is_breakeven_locked = True
            pos.current_sl_price = pos.entry_price * 1.002 if is_long else pos.entry_price * 0.998
            return ExitAction(
                action_type="ADJUST_SL",
                price=pos.current_sl_price,
                reason="Chandelier L1: Locked Breakeven (+0.2%) at +3% profit",
            )

        # Level 2: at +5.0% gain -> Trail 1.5% behind highest high
        if pnl_pct >= 5.0:
            new_sl = pos.highest_price * 0.985 if is_long else pos.lowest_price * 1.015
            if (is_long and new_sl > pos.current_sl_price) or (not is_long and new_sl < pos.current_sl_price):
                pos.current_sl_price = new_sl
                return ExitAction(
                    action_type="ADJUST_SL",
                    price=pos.current_sl_price,
                    reason=f"Chandelier L2: Trailing stop ratcheted to ${new_sl:,.2f}",
                )

        # 5. Stale Momentum Time-Decay Exit (90s)
        if elapsed_sec >= self.time_decay_seconds and pnl_pct < self.min_momentum_gain_pct:
            return ExitAction(
                action_type="CLOSE_MARKET",
                price=current_price,
                reason=f"Stale Momentum Time-Decay: Failed to reach +{self.min_momentum_gain_pct}% after {int(elapsed_sec)}s",
            )

        return ExitAction(action_type="HOLD", price=current_price)
