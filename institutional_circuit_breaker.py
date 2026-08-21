#!/usr/bin/env python3
"""
3-Tier Institutional Circuit Breaker & Liquidity Lockout (institutional_circuit_breaker.py)
==========================================================================================
Tier 1: Asset-Level Cooldown (15m pause on asset after anomalous loss)
Tier 2: Sector-Level Freeze (Freeze all Memes or DeFi on macro shock)
Tier 3: Global Flash Evacuation (Emergency shutdown across all 3 subaccounts)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("CircuitBreaker")


@dataclass
class CircuitBreakerStatus:
    """Current circuit breaker state across all tiers."""
    tier1_cooldown_assets: List[str]
    tier2_frozen_sectors: List[str]
    tier3_global_evacuate_engaged: bool
    is_trading_permitted: bool
    reason: str
    timestamp: float = field(default_factory=time.time)


class InstitutionalCircuitBreaker:
    """
    3-Tier Institutional Risk & Circuit Breaker Engine.
    """

    def __init__(
        self,
        tier1_cooldown_duration_sec: float = 900.0, # 15 min cooldown
        max_daily_loss_usd: float = 100.0,
        max_hourly_loss_usd: float = 40.0,
    ):
        self.tier1_cooldown_duration_sec = tier1_cooldown_duration_sec
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_hourly_loss_usd = max_hourly_loss_usd

        # Asset -> cooldown expiry timestamp
        self._asset_cooldowns: Dict[str, float] = {}
        # Sector -> freeze expiry timestamp
        self._sector_freezes: Dict[str, float] = {}
        self._global_kill: bool = False

    def trigger_tier1_asset_cooldown(self, symbol: str, reason: str = "Consecutive loss") -> None:
        """Pauses a single asset for 15 minutes."""
        sym = symbol.upper()
        self._asset_cooldowns[sym] = time.time() + self.tier1_cooldown_duration_sec
        logger.warning("🛑 [Tier-1 Breaker] %s placed on 15m cooldown: %s", sym, reason)

    def trigger_tier2_sector_freeze(self, sector_name: str, duration_sec: float = 1800.0) -> None:
        """Freezes an entire sector (e.g. 'MEMES' or 'DEFI') for 30 minutes."""
        sec = sector_name.upper()
        self._sector_freezes[sec] = time.time() + duration_sec
        logger.warning("🛑 [Tier-2 Breaker] Sector %s frozen for %.0fs", sec, duration_sec)

    def trigger_tier3_global_evacuation(self, reason: str = "Max loss breach") -> None:
        """Engages global emergency circuit breaker."""
        self._global_kill = True
        logger.critical("🚨 [Tier-3 Breaker] GLOBAL EVACUATION ENGAGED: %s", reason)

    def reset_global_kill(self) -> None:
        """Resets global kill switch."""
        self._global_kill = False

    def is_asset_tradeable(self, symbol: str, sector: Optional[str] = None) -> Tuple[bool, str]:
        """
        Evaluates whether trading is permitted for an asset across all 3 tiers.
        """
        sym = symbol.upper()
        now = time.time()

        if self._global_kill:
            return False, "Tier-3 Global Flash Evacuation is Engaged"

        if sector and self._sector_freezes.get(sector.upper(), 0.0) > now:
            return False, f"Tier-2 Sector {sector.upper()} is frozen"

        if self._asset_cooldowns.get(sym, 0.0) > now:
            rem = int(self._asset_cooldowns[sym] - now)
            return False, f"Tier-1 Asset {sym} is in cooldown ({rem}s remaining)"

        return True, "Trading Permitted"

    def get_status(self) -> CircuitBreakerStatus:
        """Returns consolidated circuit breaker report."""
        now = time.time()
        active_cooldowns = [k for k, v in self._asset_cooldowns.items() if v > now]
        active_freezes = [k for k, v in self._sector_freezes.items() if v > now]

        is_permitted = not self._global_kill

        return CircuitBreakerStatus(
            tier1_cooldown_assets=active_cooldowns,
            tier2_frozen_sectors=active_freezes,
            tier3_global_evacuate_engaged=self._global_kill,
            is_trading_permitted=is_permitted,
            reason="Normal Operation" if is_permitted else "Tier Breaker Engaged",
        )
