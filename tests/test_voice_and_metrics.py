#!/usr/bin/env python3
"""
Unit Tests for Telegram Voice Copilot & Prometheus Metrics Server (tests/test_voice_and_metrics.py)
====================================================================================================
"""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from telegram_voice_copilot import (
    TelegramVoiceCopilot,
)
from metrics_server import (
    PerformanceMetricsCollector,
)
from master_profit_orchestrator import (
    MasterProfitOrchestrator,
)


# =============================================================================
# 1. TELEGRAM VOICE COPILOT TESTS
# =============================================================================

@pytest.mark.asyncio
async def test_telegram_voice_copilot_transcription_and_dispatch():
    copilot = TelegramVoiceCopilot()
    fake_audio = b"OGG_MOCK_HEADER_" + b"0" * 200

    res = await copilot.handle_voice_message(
        audio_bytes=fake_audio,
        chat_id=1267102944,
        voice_duration_sec=2.5,
    )

    assert res["success"] is True
    assert "transcript" in res
    assert len(res["transcript"]) > 0
    assert "Heard:" in res["response_html"]


# =============================================================================
# 2. PROMETHEUS METRICS COLLECTOR TESTS
# =============================================================================

def test_prometheus_metrics_generator():
    orchestrator = MasterProfitOrchestrator(is_paper=True)
    collector = PerformanceMetricsCollector(master_orchestrator=orchestrator)

    metrics_text = collector.generate_prometheus_metrics()
    assert "lighter_portfolio_equity_usd" in metrics_text
    assert "lighter_total_volume_usd" in metrics_text
    assert "lighter_realized_pnl_usd" in metrics_text
    assert "lighter_active_positions" in metrics_text

    json_telemetry = collector.get_json_telemetry()
    assert "telemetry" in json_telemetry
    assert "shards" in json_telemetry
