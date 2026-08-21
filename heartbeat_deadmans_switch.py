#!/usr/bin/env python3
"""
Dead-Man's Switch & 30s Uptime Watchdog (heartbeat_deadmans_switch.py)
====================================================================
Monitors continuous process execution and internet connectivity.
Transmits heartbeat pings every 30 seconds and triggers emergency alarms
and fail-safe risk reduction if pings lapse for > 60 seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("DeadMansSwitch")


@dataclass
class HeartbeatStatus:
    """Consolidated heartbeat health status."""
    last_ping_time: float
    elapsed_since_ping_sec: float
    is_healthy: bool
    consecutive_missed_pings: int
    emergency_action_triggered: bool
    timestamp: float = field(default_factory=time.time)


class DeadMansHeartbeatSwitch:
    """
    Continuous process watchdog and emergency dead-man's fail-safe switch.
    """

    def __init__(
        self,
        ping_interval_sec: float = 30.0,
        max_missed_threshold_sec: float = 60.0,
        emergency_callback: Optional[Callable[[], Any]] = None,
    ):
        self.ping_interval_sec = ping_interval_sec
        self.max_missed_threshold_sec = max_missed_threshold_sec
        self.emergency_callback = emergency_callback

        self._last_ping: float = time.time()
        self._is_active: bool = False
        self._emergency_triggered: bool = False

    def emit_heartbeat(self) -> float:
        """Called by the main trading loop on each iteration to prove vitality."""
        self._last_ping = time.time()
        self._emergency_triggered = False
        return self._last_ping

    def evaluate_health(self) -> HeartbeatStatus:
        """
        Evaluates elapsed time since the last heartbeat ping.
        """
        now = time.time()
        elapsed = now - self._last_ping
        is_healthy = elapsed <= self.max_missed_threshold_sec
        missed_count = int(elapsed // self.ping_interval_sec)

        emergency = False
        if not is_healthy and not self._emergency_triggered:
            self._emergency_triggered = True
            emergency = True
            logger.critical("🚨 [DEAD-MAN'S SWITCH TRIGGERED] Process stalled for %.1fs! Triggering emergency fail-safe!", elapsed)
            if self.emergency_callback:
                try:
                    self.emergency_callback()
                except Exception as e:
                    logger.error("Error invoking emergency callback: %s", e)

        return HeartbeatStatus(
            last_ping_time=self._last_ping,
            elapsed_since_ping_sec=round(elapsed, 1),
            is_healthy=is_healthy,
            consecutive_missed_pings=missed_count,
            emergency_action_triggered=emergency or self._emergency_triggered,
        )
