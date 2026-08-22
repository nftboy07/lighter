import os
import sys
import queue
import time
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from poke_notifier import (
    strip_html_tags,
    poke_send,
    poke_send_trade_alert,
    poke_send_tp_sl_alert,
    PokeNotifierWorker,
    _POKE_QUEUE,
)


def test_strip_html_tags():
    raw = "<b>⚡ ETH LONG</b> with <code>$2,650.00</code> &amp; target"
    cleaned = strip_html_tags(raw)
    assert "<b>" not in cleaned
    assert "<code>" not in cleaned
    assert "ETH LONG" in cleaned
    assert "&" in cleaned


def test_poke_send_enqueues_message():
    with patch("poke_notifier._ensure_worker_started"):
        # Drain queue first
        while not _POKE_QUEUE.empty():
            try:
                _POKE_QUEUE.get_nowait()
            except queue.Empty:
                break

        res = poke_send("Test alert for Poke AI")
        assert res is True
        assert not _POKE_QUEUE.empty()
        item = _POKE_QUEUE.get_nowait()
        assert item == "Test alert for Poke AI"


def test_poke_send_trade_alert():
    with patch("poke_notifier.poke_send") as mock_send:
        mock_send.return_value = True
        res = poke_send_trade_alert(
            asset="ETH",
            side="BUY/LONG",
            size=0.05,
            price=2650.0,
            tp_price=2716.25,
            sl_price=2610.25,
            reason="BREAKING_ETF_NEWS",
            notional_usd=5.52,
        )
        assert res is True
        assert mock_send.called
        args, _ = mock_send.call_args
        assert "ETH (BUY/LONG)" in args[0]
        assert "$2,650.00" in args[0]
        assert "BREAKING_ETF_NEWS" in args[0]


def test_poke_send_tp_sl_alert():
    with patch("poke_notifier.poke_send") as mock_send:
        mock_send.return_value = True
        res = poke_send_tp_sl_alert(
            asset="SOL",
            exit_type="TAKE_PROFIT_LADDER_1",
            pnl_usd=0.25,
            pnl_pct=2.0,
            exit_price=148.5,
        )
        assert res is True
        assert mock_send.called
        args, _ = mock_send.call_args
        assert "SOL" in args[0]
        assert "+2.00%" in args[0]
        assert "$+0.25 USD" in args[0]


def test_poke_notifier_worker_dispatch():
    worker = PokeNotifierWorker(api_key="test_key", api_url="https://poke.com/test")
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        worker._dispatch("<b>Test Execution</b>")
        assert mock_urlopen.called
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer test_key"
        assert req.get_header("Content-type") == "application/json"
