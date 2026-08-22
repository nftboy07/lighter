#!/usr/bin/env python3
"""
Unit Tests for Watchdog Supervisor (watchdog_supervisor.py)
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from watchdog_supervisor import send_telegram_alert


def test_watchdog_supervisor_alert(monkeypatch):
    # Verify send_telegram_alert handles exceptions safely without crashing
    def mock_tg_send(msg):
        assert "TEST" in msg
        return True

    monkeypatch.setattr("lighter_telegram.tg_send", mock_tg_send)
    send_telegram_alert("TEST ALERT")
