import asyncio
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hyperliquid_whale_tracker import HyperliquidWhaleTracker, CURATED_WHALES


@pytest.mark.asyncio
async def test_whale_tracker_initialization():
    tracker = HyperliquidWhaleTracker(min_notional_usd=100000.0)
    assert len(tracker.watched_whales) >= 5
    assert not tracker.is_running
    assert tracker.min_notional_usd == 100000.0


@pytest.mark.asyncio
async def test_whale_position_detection_and_signal_generation():
    signals = []

    def on_sig(s):
        signals.append(s)

    tracker = HyperliquidWhaleTracker(on_whale_signal=on_sig, min_notional_usd=50000.0)

    # Mock fetch_user_state
    async def mock_fetch_user_state(user):
        return {
            "assetPositions": [
                {
                    "position": {
                        "coin": "HYPE",
                        "szi": "10000.0",
                        "entryPx": "25.50",
                    }
                }
            ]
        }

    tracker.fetch_user_state = mock_fetch_user_state
    detected = await tracker.scan_whale_positions()

    assert len(detected) > 0
    assert detected[0]["asset"] == "HYPE"
    assert detected[0]["side"] == "BUY"
    assert detected[0]["notional_usd"] == 255000.0
    assert detected[0]["conviction"] == 0.92
    assert "HYPE" in detected[0]["headline"]


@pytest.mark.asyncio
async def test_whale_tracker_start_stop():
    tracker = HyperliquidWhaleTracker()
    await tracker.start()
    assert tracker.is_running
    await tracker.stop()
    assert not tracker.is_running
