#!/usr/bin/env python3
"""
Prometheus & Real-Time REST Metrics Server (metrics_server.py)
==============================================================
Exposes live performance metrics, subaccount collateral, latency profiling,
and volume stats over Prometheus /metrics and REST /api/status endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("MetricsServer")


class PerformanceMetricsCollector:
    """
    Collects and formats quantitative metrics for Prometheus scrapers and Grafana.
    """

    def __init__(self, master_orchestrator: Optional[Any] = None):
        self.orchestrator = master_orchestrator

    def generate_prometheus_metrics(self) -> str:
        """
        Formats metrics in standard Prometheus text exposition format.
        """
        ts = time.time()
        portfolio_usd = 0.0
        volume_usd = 0.0
        pnl_usd = 0.0
        active_positions = 0

        if self.orchestrator:
            summary = self.orchestrator.get_summary_report()
            telemetry = summary.get("telemetry", {})
            portfolio_usd = telemetry.get("total_portfolio_usd", 0.0)
            volume_usd = telemetry.get("total_volume_usd", 0.0)
            pnl_usd = telemetry.get("total_realized_pnl_usd", 0.0)
            active_positions = telemetry.get("open_positions_count", 0)

        lines = [
            "# HELP lighter_portfolio_equity_usd Total collateral in USD across subaccounts",
            "# TYPE lighter_portfolio_equity_usd gauge",
            f"lighter_portfolio_equity_usd {portfolio_usd:.4f}",
            "",
            "# HELP lighter_total_volume_usd Total farmed trading volume in USD",
            "# TYPE lighter_total_volume_usd counter",
            f"lighter_total_volume_usd {volume_usd:.2f}",
            "",
            "# HELP lighter_realized_pnl_usd Cumulative realized profit and loss in USD",
            "# TYPE lighter_realized_pnl_usd gauge",
            f"lighter_realized_pnl_usd {pnl_usd:.4f}",
            "",
            "# HELP lighter_active_positions Number of currently open positions",
            "# TYPE lighter_active_positions gauge",
            f"lighter_active_positions {active_positions}",
            "",
            "# HELP lighter_bot_uptime_seconds Time since bot start",
            "# TYPE lighter_bot_uptime_seconds counter",
            f"lighter_bot_uptime_seconds {int(ts)}",
        ]
        return "\n".join(lines) + "\n"

    def get_json_telemetry(self) -> Dict[str, Any]:
        """
        Returns JSON format telemetry for web dashboards.
        """
        if self.orchestrator:
            return self.orchestrator.get_summary_report()
        return {
            "status": "ONLINE",
            "timestamp": time.time(),
        }
