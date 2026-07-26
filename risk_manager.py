#!/usr/bin/env python3
"""
Risk Management Module for B20 Bot
==================================
Advanced portfolio and position management:
- Dynamic position sizing
- Take-profit ladders
- Trailing stop losses
- Concurrent position limits
- Daily loss limits with auto-pause
- Kelly criterion sizing
- Circuit breaker on losses
"""

from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
from datetime import datetime, timezone
import json


@dataclass
class Position:
    """Represents an open position."""
    position_id: str
    token_address: str
    entry_price: float
    quantity: float
    entry_time: datetime
    stop_loss_price: float
    take_profit_targets: List[Tuple[float, float]]  # [(price, percent_to_sell), ...]
    created_at: datetime


class RiskManager:
    """Manage positions, sizing, and portfolio risk."""

    def __init__(
        self,
        max_position_eth: float = 0.1,
        max_positions: int = 5,
        daily_loss_limit_eth: float = 0.5,
        max_consecutive_losses: int = 3,
    ):
        self.max_position_eth = max_position_eth
        self.max_positions = max_positions
        self.daily_loss_limit_eth = daily_loss_limit_eth
        self.max_consecutive_losses = max_consecutive_losses
        
        # Portfolio state
        self.positions: Dict[str, Position] = {}
        self.daily_loss_eth = 0.0
        self.consecutive_losses = 0
        self.daily_loss_reset_time = datetime.now(timezone.utc)
        self.is_paused = False
        self.blacklisted_tokens = set()

    # =========== POSITION SIZING ===========
    def calculate_position_size(
        self,
        wallet_balance_eth: float,
        meme_score: float = 0.5,
        liquidity_eth: float = 10.0,
    ) -> float:
        """
        Calculate position size using multiple factors.
        Returns: amount in ETH to buy
        """
        # Base sizing: fraction of wallet
        base_size = wallet_balance_eth * 0.05  # 5% per trade max
        
        # Limit by max position
        size = min(base_size, self.max_position_eth)
        
        # Adjust by liquidity (don't buy too large for pool)
        if liquidity_eth > 0:
            max_liquidity_fraction = min(0.1, 10 / liquidity_eth)  # Max 10% of pool
            size = min(size, liquidity_eth * max_liquidity_fraction)
        
        # Adjust by meme score (low score = smaller position)
        # meme_score: 0-1 (1 = high confidence)
        size *= (0.5 + meme_score * 0.5)  # Range: 25-75% of base
        
        return size

    def calculate_kelly_criterion_size(
        self,
        wallet_balance_eth: float,
        win_rate: float,  # 0-1
        avg_win_percent: float,  # e.g., 2.5 for 2.5x profit
        avg_loss_percent: float,  # e.g., 0.8 for -20% loss
    ) -> float:
        """
        Calculate position size using Kelly Criterion.
        Formula: f = (bp - q) / b
        where:
        - f = fraction of bankroll to risk
        - b = odds ratio (win/loss)
        - p = win probability
        - q = loss probability (1-p)
        
        Returns: amount in ETH
        """
        if win_rate <= 0 or win_rate >= 1 or avg_win_percent <= 0 or avg_loss_percent <= 0:
            # Default to conservative sizing
            return self.max_position_eth * 0.5
        
        # Simplified Kelly
        p = win_rate
        q = 1 - p
        b = avg_win_percent / avg_loss_percent
        
        kelly_f = (b * p - q) / b
        
        # Apply safety multiplier (full Kelly can be too aggressive)
        # Use 25% Kelly for safety
        kelly_f = max(0, min(kelly_f, 1.0)) * 0.25
        
        position_size = wallet_balance_eth * kelly_f
        return min(position_size, self.max_position_eth)

    def calculate_kelly_from_db(
        self,
        db_path: str,
        wallet_balance_eth: float,
        fallback_eth: float = 0.001
    ) -> float:
        """
        Calculate dynamic Kelly sizing using historical trade results from SQLite database.
        Returns: ETH position size
        """
        import sqlite3
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT action, eth_amount FROM trades WHERE status='success'")
            rows = cursor.fetchall()
            conn.close()

            if not rows or len(rows) < 5:
                return fallback_eth

            wins = [r for r in rows if r[0] in ('sell', 'TP_SELL', 'EXIT_WIN') and r[1] > 0]
            losses = [r for r in rows if r[0] in ('RUG_ZERO_LIQ', 'STOP_LOSS', 'EXIT_LOSS')]

            total = len(wins) + len(losses)
            if total == 0:
                return fallback_eth

            win_rate = len(wins) / total
            avg_win = (sum(w[1] for w in wins) / len(wins)) if wins else 0.002
            avg_loss = (sum(l[1] for l in losses) / len(losses)) if losses else 0.001

            return self.calculate_kelly_criterion_size(
                wallet_balance_eth=wallet_balance_eth,
                win_rate=win_rate,
                avg_win_percent=avg_win,
                avg_loss_percent=avg_loss if avg_loss > 0 else 0.001
            )
        except Exception:
            return fallback_eth

    # =========== POSITION LIMITS ===========
    def can_open_position(self, token_address: str) -> Tuple[bool, str]:
        """Check if we can open a new position."""
        if self.is_paused:
            return (False, "Risk manager paused")
        
        if token_address in self.blacklisted_tokens:
            return (False, f"Token blacklisted")
        
        if len(self.positions) >= self.max_positions:
            return (False, f"Max positions ({self.max_positions}) reached")
        
        if self.daily_loss_eth >= self.daily_loss_limit_eth:
            return (False, f"Daily loss limit reached ({self.daily_loss_eth:.4f} ETH)")
        
        if self.consecutive_losses >= self.max_consecutive_losses:
            return (False, f"Max consecutive losses ({self.max_consecutive_losses}) reached")
        
        return (True, "OK")

    # =========== TAKE PROFIT LADDER ===========
    def create_take_profit_ladder(
        self, entry_price: float, quantity: float
    ) -> List[Tuple[float, float]]:
        """
        Create a take-profit ladder.
        Standard: 25% at 2x, 25% at 5x, 25% at 10x, hold 25% for moonshot
        Returns: [(price, quantity_to_sell), ...]
        """
        targets = []
        qty_per_tier = quantity / 4
        
        # 2x: sell 25%
        targets.append((entry_price * 2.0, qty_per_tier))
        
        # 5x: sell 25%
        targets.append((entry_price * 5.0, qty_per_tier))
        
        # 10x: sell 25%
        targets.append((entry_price * 10.0, qty_per_tier))
        
        # Hold rest for potential higher returns
        # (remaining 25% handled by trailing stop or manual exit)
        
        return targets

    # =========== STOP LOSS ===========
    def calculate_stop_loss(
        self, entry_price: float, max_loss_percent: float = 20.0
    ) -> float:
        """Calculate stop loss price."""
        return entry_price * (1.0 - max_loss_percent / 100.0)

    def calculate_trailing_stop(
        self, current_price: float, highest_price: float, trail_percent: float = 10.0
    ) -> float:
        """Calculate trailing stop loss price."""
        return highest_price * (1.0 - trail_percent / 100.0)

    def should_sell(
        self, position_id: str, current_price: float
    ) -> Tuple[bool, str, float]:
        """
        Check if position should be sold (take profit or stop loss).
        Returns: (should_sell, reason, quantity_to_sell)
        """
        if position_id not in self.positions:
            return (False, "Position not found", 0.0)
        
        pos = self.positions[position_id]
        
        # Check stop loss
        if current_price <= pos.stop_loss_price:
            return (True, "Stop loss hit", pos.quantity)
        
        # Check take profit targets
        for target_price, qty in pos.take_profit_targets:
            if current_price >= target_price:
                return (True, f"Take profit target {target_price} hit", qty)
        
        return (False, "Hold", 0.0)

    # =========== LOSS TRACKING ===========
    def record_loss(self, loss_eth: float) -> None:
        """Record a realized loss."""
        # Reset daily loss if 24h passed
        now = datetime.now(timezone.utc)
        if (now - self.daily_loss_reset_time).total_seconds() > 86400:
            self.daily_loss_eth = 0.0
            self.consecutive_losses = 0
            self.daily_loss_reset_time = now
        
        self.daily_loss_eth += loss_eth
        self.consecutive_losses += 1
        
        # Auto-pause if daily limit hit
        if self.daily_loss_eth >= self.daily_loss_limit_eth:
            self.pause()

    def record_win(self) -> None:
        """Record a realized win."""
        self.consecutive_losses = 0

    # =========== PAUSE / RESUME ===========
    def pause(self) -> None:
        """Pause all trading."""
        self.is_paused = True
        print("Risk manager paused - no new positions")

    def resume(self) -> None:
        """Resume trading."""
        self.is_paused = False
        print("Risk manager resumed")

    # =========== BLACKLISTING ===========
    def blacklist_token(self, token_address: str) -> None:
        """Blacklist a token (don't buy again)."""
        self.blacklisted_tokens.add(token_address)

    def get_blacklisted_tokens(self) -> List[str]:
        """Get list of blacklisted tokens."""
        return list(self.blacklisted_tokens)

    # =========== POSITION MANAGEMENT ===========
    def open_position(self, position: Position) -> None:
        """Track a new position."""
        self.positions[position.position_id] = position

    def close_position(self, position_id: str) -> Optional[Position]:
        """Close and remove a position."""
        if position_id in self.positions:
            return self.positions.pop(position_id)
        return None

    def get_positions(self) -> List[Position]:
        """Get all open positions."""
        return list(self.positions.values())

    def get_position_count(self) -> int:
        """Get number of open positions."""
        return len(self.positions)

    # =========== PORTFOLIO ANALYTICS ===========
    def calculate_portfolio_value(self, prices: Dict[str, float]) -> float:
        """Calculate total portfolio value in ETH."""
        total = 0.0
        for pos in self.positions.values():
            current_price = prices.get(pos.token_address, pos.entry_price)
            value = pos.quantity * current_price
            total += value
        return total

    def calculate_portfolio_pnl(self, prices: Dict[str, float]) -> Tuple[float, float]:
        """
        Calculate portfolio PnL.
        Returns: (unrealized_pnl_eth, unrealized_pnl_percent)
        """
        total_entry_value = 0.0
        total_current_value = 0.0
        
        for pos in self.positions.values():
            entry_value = pos.quantity * pos.entry_price
            current_price = prices.get(pos.token_address, pos.entry_price)
            current_value = pos.quantity * current_price
            
            total_entry_value += entry_value
            total_current_value += current_value
        
        pnl_eth = total_current_value - total_entry_value
        pnl_percent = (pnl_eth / total_entry_value * 100) if total_entry_value > 0 else 0.0
        
        return (pnl_eth, pnl_percent)

    def get_max_drawdown(self, prices: Dict[str, float]) -> float:
        """Calculate maximum drawdown percentage."""
        prices_list = list(prices.values())
        if not prices_list:
            return 0.0
        
        max_price = max(prices_list)
        min_price = min(prices_list)
        
        return ((max_price - min_price) / max_price * 100) if max_price > 0 else 0.0

    # =========== EMERGENCY FUNCTIONS ===========
    def dump_all(self) -> List[str]:
        """Get all position IDs for emergency liquidation."""
        return list(self.positions.keys())

    def get_stats(self) -> Dict[str, any]:
        """Get risk manager statistics."""
        return {
            "open_positions": len(self.positions),
            "max_positions": self.max_positions,
            "daily_loss_eth": self.daily_loss_eth,
            "daily_loss_limit_eth": self.daily_loss_limit_eth,
            "consecutive_losses": self.consecutive_losses,
            "is_paused": self.is_paused,
            "blacklisted_count": len(self.blacklisted_tokens),
        }
