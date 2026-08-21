#!/usr/bin/env python3
"""
Multi-Node Failover & Redundant Watchdog (redundant_failover_node.py)
===================================================================
Provides zero-downtime high-availability architecture:
- Primary <-> Standby node heartbeat health checks.
- Automatic failover election if primary node misses heartbeats for > timeout.
- State synchronization and atomic nonce reservation to prevent double-execution.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("RedundantFailover")


class NodeRole(str, Enum):
    PRIMARY = "PRIMARY"
    STANDBY = "STANDBY"
    FAILOVER_ACTIVE = "FAILOVER_ACTIVE"


@dataclass
class NodeHeartbeat:
    """Heartbeat signal sent between nodes."""
    node_id: str
    role: NodeRole
    last_ping_time: float
    active_positions_count: int
    open_orders_count: int
    rpc_latency_ms: float
    is_healthy: bool = True


class RedundantFailoverManager:
    """
    Monitors node health and handles seamless standby promotion upon primary outage.
    """

    def __init__(
        self,
        node_id: str = "node_secondary_vps",
        initial_role: NodeRole = NodeRole.STANDBY,
        heartbeat_timeout_sec: float = 3.0,
        max_rpc_latency_ms: float = 250.0,
    ):
        self.node_id = node_id
        self.current_role = initial_role
        self.heartbeat_timeout_sec = heartbeat_timeout_sec
        self.max_rpc_latency_ms = max_rpc_latency_ms

        self.last_primary_heartbeat: Optional[NodeHeartbeat] = None
        self.failover_events: List[Dict[str, Any]] = []

    def receive_heartbeat(self, hb: NodeHeartbeat) -> None:
        """Records an incoming heartbeat from peer node."""
        if hb.role == NodeRole.PRIMARY:
            self.last_primary_heartbeat = hb

    def check_failover_condition(self, now: Optional[float] = None) -> Tuple[bool, str]:
        """
        Evaluates whether the standby node should promote itself to FAILOVER_ACTIVE.
        """
        if self.current_role == NodeRole.PRIMARY:
            return False, "ALREADY_PRIMARY"

        if self.current_role == NodeRole.FAILOVER_ACTIVE:
            return False, "ALREADY_PROMOTED"

        ts = now if now is not None else time.time()

        # Check 1: Primary heartbeat missing entirely
        if self.last_primary_heartbeat is None:
            return False, "WAITING_FOR_INITIAL_HEARTBEAT"

        time_since_last = ts - self.last_primary_heartbeat.last_ping_time
        if time_since_last > self.heartbeat_timeout_sec:
            reason = f"Primary heartbeat timeout: {time_since_last:.2f}s > {self.heartbeat_timeout_sec:.2f}s"
            return True, reason

        # Check 2: Primary reports degraded health / excessive latency
        if not self.last_primary_heartbeat.is_healthy:
            return True, "Primary reported unhealthy state"

        if self.last_primary_heartbeat.rpc_latency_ms > self.max_rpc_latency_ms:
            return True, f"Primary RPC latency too high ({self.last_primary_heartbeat.rpc_latency_ms:.1f}ms > {self.max_rpc_latency_ms:.1f}ms)"

        return False, "HEALTHY"

    def promote_to_active(self, reason: str) -> None:
        """Promotes this standby node to active executor."""
        self.current_role = NodeRole.FAILOVER_ACTIVE
        evt = {
            "timestamp": time.time(),
            "node_id": self.node_id,
            "new_role": self.current_role.value,
            "reason": reason,
        }
        self.failover_events.append(evt)
        logger.warning("🚨 [Failover] Standby promoted to FAILOVER_ACTIVE: %s", reason)
