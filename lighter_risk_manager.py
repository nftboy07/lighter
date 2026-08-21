#!/usr/bin/env python3
"""
Risk Management & Circuit Breakers for Lighter MM Bot
=====================================================
Enforces:
- Hard & Soft Inventory Delta Limits
- Pre-Trade Price Band Validation (+/- 1.5% max deviation from mid-price)
- Volatility Spike Circuit Breaker (halts/widens quotes on sudden volatility)
- Daily Drawdown & Loss Limit with Auto-Pause
- WebSocket Liveness / Deadman's Switch (cancels all quotes if data is stale)
- File-based Kill Switch (lighter_kill_switch.flag)
- Emergency Liquidate / Dump Inventory functions
"""

import os
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from lighter_strategy import OrderSide, TargetQuote

logger = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    max_inventory: float = 1.0           # Maximum base units long or short
    soft_inventory_limit: float = 0.5    # Soft threshold where quoting is aggressively skewed
    max_price_deviation_pct: float = 0.015 # Max +/- 1.5% deviation from mid-price
    max_daily_loss_usd: float = 100.0    # Stop trading if daily realized loss exceeds this
    max_volatility_multiplier: float = 3.0 # Circuit breaker if vol > 3x rolling base
    heartbeat_timeout_sec: float = 3.0   # Deadman's switch trigger if no WS message
    kill_switch_file: str = "lighter_kill_switch.flag"


class LighterRiskManager:
    """
    Real-time risk evaluation engine ensuring delta neutrality, capital safety,
    and automatic quote pulling during adverse market conditions.
    """

    def __init__(self, limits: Optional[RiskLimits] = None):
        self.limits = limits or RiskLimits()
        self.inventory: float = 0.0
        self.daily_realized_pnl: float = 0.0
        self.daily_volume_usd: float = 0.0
        self.last_heartbeat_time: float = time.time()
        self.is_paused: bool = False
        self.pause_reason: str = ""
        self.circuit_broken: bool = False
        self.baseline_volatility: float = 0.01

    def record_heartbeat(self):
        """Updates the timestamp of the latest received WebSocket message."""
        self.last_heartbeat_time = time.time()

    def check_kill_switch_file(self) -> bool:
        """Checks if a manual kill switch file exists on disk."""
        if os.path.exists(self.limits.kill_switch_file):
            self.is_paused = True
            self.pause_reason = f"Kill switch file detected ({self.limits.kill_switch_file})"
            return True
        return False

    def update_pnl(self, realized_pnl_delta: float, fill_volume_usd: float):
        """Updates cumulative daily realized PnL and traded volume."""
        self.daily_realized_pnl += realized_pnl_delta
        self.daily_volume_usd += fill_volume_usd

        # Check daily drawdown
        if self.daily_realized_pnl < -abs(self.limits.max_daily_loss_usd):
            self.is_paused = True
            self.pause_reason = f"Daily loss limit breached (-${abs(self.daily_realized_pnl):.2f} > -${self.limits.max_daily_loss_usd:.2f})"
            logger.critical(f"[RISK] {self.pause_reason}")

    def update_inventory(self, delta_qty: float):
        """Updates the tracked base asset inventory."""
        self.inventory += delta_qty

    def set_inventory(self, qty: float):
        """Explicitly sets the tracked inventory from exchange balance."""
        self.inventory = qty

    def check_liveness(self) -> bool:
        """Deadman's switch: returns True if WebSocket connection is live, False if timed out."""
        elapsed = time.time() - self.last_heartbeat_time
        if elapsed > self.limits.heartbeat_timeout_sec:
            self.circuit_broken = True
            self.pause_reason = f"WebSocket heartbeat timeout ({elapsed:.1f}s > {self.limits.heartbeat_timeout_sec}s)"
            logger.warning(f"[RISK] {self.pause_reason}")
            return False
        return True

    def check_volatility_spike(self, current_volatility: float) -> bool:
        """Triggers circuit breaker if current volatility surges far above baseline."""
        if self.baseline_volatility <= 0:
            self.baseline_volatility = max(0.001, current_volatility)
            return False

        ratio = current_volatility / self.baseline_volatility
        if ratio > self.limits.max_volatility_multiplier:
            self.circuit_broken = True
            self.pause_reason = f"Volatility spike detected ({ratio:.1f}x baseline)"
            logger.warning(f"[RISK] {self.pause_reason}")
            return True

        # Slowly decay baseline towards current
        self.baseline_volatility = 0.98 * self.baseline_volatility + 0.02 * current_volatility
        return False

    def validate_quotes(
        self,
        target_quotes: Dict[OrderSide, List[TargetQuote]],
        mid_price: float,
        current_volatility: float = 0.015,
    ) -> Dict[OrderSide, List[TargetQuote]]:
        """
        Filters and bounds target quotes according to pre-trade risk and inventory delta rules.
        """
        # 1. Check Kill Switch & Pause State
        if self.check_kill_switch_file() or self.is_paused:
            return {OrderSide.BUY: [], OrderSide.SELL: []}

        # 2. Check Liveness
        if not self.check_liveness():
            return {OrderSide.BUY: [], OrderSide.SELL: []}

        # 3. Check Volatility Spike
        if self.check_volatility_spike(current_volatility):
            return {OrderSide.BUY: [], OrderSide.SELL: []}

        if mid_price <= 0:
            return {OrderSide.BUY: [], OrderSide.SELL: []}

        validated_buys: List[TargetQuote] = []
        validated_sells: List[TargetQuote] = []

        # 4. Inventory Delta Constraints
        # Can we place more BUY orders?
        can_buy = self.inventory < self.limits.max_inventory
        # Can we place more SELL orders?
        can_sell = self.inventory > -self.limits.max_inventory

        # 5. Price Band Filter (+/- max_price_deviation_pct)
        min_allowed_price = mid_price * (1.0 - self.limits.max_price_deviation_pct)
        max_allowed_price = mid_price * (1.0 + self.limits.max_price_deviation_pct)

        if can_buy:
            for q in target_quotes.get(OrderSide.BUY, []):
                if min_allowed_price <= q.price < mid_price:
                    # Scale down size if near hard limit
                    if self.inventory > self.limits.soft_inventory_limit:
                        q.size = max(0.001, q.size * 0.5)
                    validated_buys.append(q)

        if can_sell:
            for q in target_quotes.get(OrderSide.SELL, []):
                if mid_price < q.price <= max_allowed_price:
                    # Scale down size if short near hard limit
                    if self.inventory < -self.limits.soft_inventory_limit:
                        q.size = max(0.001, q.size * 0.5)
                    validated_sells.append(q)

        return {
            OrderSide.BUY: validated_buys,
            OrderSide.SELL: validated_sells,
        }

    def reset_circuit_breaker(self):
        """Manually or programmatically resets circuit breaker."""
        self.circuit_broken = False
        self.is_paused = False
        self.pause_reason = ""
        self.last_heartbeat_time = time.time()
        logger.info("[RISK] Circuit breaker reset. Quoting resumed.")

    def get_status(self) -> Dict[str, any]:
        """Returns snapshot of current risk state and metrics."""
        return {
            "inventory": round(self.inventory, 4),
            "max_inventory": self.limits.max_inventory,
            "soft_inventory": self.limits.soft_inventory_limit,
            "daily_realized_pnl": round(self.daily_realized_pnl, 2),
            "daily_volume_usd": round(self.daily_volume_usd, 2),
            "is_paused": self.is_paused,
            "pause_reason": self.pause_reason,
            "circuit_broken": self.circuit_broken,
            "seconds_since_heartbeat": round(time.time() - self.last_heartbeat_time, 2),
        }
