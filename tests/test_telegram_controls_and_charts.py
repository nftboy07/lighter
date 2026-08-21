#!/usr/bin/env python3
"""
Unit tests for Dynamic Visual Candlestick & Target Chart Generator
and Interactive One-Tap Position Controls in Lighter Telegram Bot.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from chart_generator import (
    calculate_target_levels,
    generate_position_chart,
    generate_position_chart_file,
    generate_synthetic_candles,
    tg_send_photo,
)
from lighter_news_sniper import ActivePosition
from lighter_telegram import LighterTelegramBot, tg_send_photo as bot_tg_send_photo


# =============================================================================
# CHART GENERATOR TESTS
# =============================================================================

def test_calculate_target_levels_long():
    entry = 2500.0
    levels = calculate_target_levels(entry_price=entry, side="BUY/LONG", tp_pct=2.0, tp2_pct=4.0, sl_pct=1.5)
    
    assert levels["entry"] == 2500.0
    assert pytest.approx(levels["tp1"], rel=1e-5) == 2550.0  # +2.0%
    assert pytest.approx(levels["tp2"], rel=1e-5) == 2600.0  # +4.0%
    assert pytest.approx(levels["sl"], rel=1e-5) == 2462.5   # -1.5%


def test_calculate_target_levels_short():
    entry = 2000.0
    levels = calculate_target_levels(entry_price=entry, side="SELL/SHORT", tp_pct=2.0, tp2_pct=4.0, sl_pct=1.5)
    
    assert levels["entry"] == 2000.0
    assert pytest.approx(levels["tp1"], rel=1e-5) == 1960.0  # -2.0%
    assert pytest.approx(levels["tp2"], rel=1e-5) == 1920.0  # -4.0%
    assert pytest.approx(levels["sl"], rel=1e-5) == 2030.0   # +1.5%


def test_generate_synthetic_candles():
    candles = generate_synthetic_candles(entry_price=100.0, current_price=102.5, side="BUY/LONG", n_candles=30)
    
    assert len(candles) == 30
    for c in candles:
        assert "open" in c and "high" in c and "low" in c and "close" in c and "volume" in c
        assert c["high"] >= c["low"]
        assert c["high"] >= min(c["open"], c["close"])
        assert c["low"] <= max(c["open"], c["close"])
        assert c["volume"] > 0
    # Final candle close lands on current_price
    assert pytest.approx(candles[-1]["close"], rel=1e-3) == 102.5


def test_generate_position_chart_png_bytes():
    chart_bytes = generate_position_chart(
        symbol="ETH",
        side="BUY/LONG",
        entry_price=2650.0,
        current_price=2710.0,
        size=1.5,
        tp_pct=2.0,
        tp2_pct=4.0,
        sl_pct=1.5,
    )
    
    assert isinstance(chart_bytes, bytes)
    assert len(chart_bytes) > 1000
    # PNG Magic bytes header
    assert chart_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    
    # Verify PIL can open and parse it
    img = Image.open(io.BytesIO(chart_bytes))
    assert img.format == "PNG"
    assert img.size[0] > 500 and img.size[1] > 300


def test_generate_position_chart_short_and_custom_candles():
    custom_candles = [
        {"open": 130.0, "high": 132.0, "low": 129.0, "close": 131.0, "volume": 500.0},
        {"open": 131.0, "high": 131.5, "low": 127.5, "close": 128.0, "volume": 800.0},
        {"open": 128.0, "high": 129.0, "low": 126.0, "close": 126.5, "volume": 650.0},
    ]
    chart_bytes = generate_position_chart(
        symbol="NVDA",
        side="SELL/SHORT",
        entry_price=130.0,
        current_price=126.5,
        size=25.0,
        tp_pct=3.0,
        tp2_pct=6.0,
        sl_pct=2.0,
        custom_tp_price=126.10,
        custom_sl_price=132.60,
        candles=custom_candles,
    )
    
    assert isinstance(chart_bytes, bytes)
    assert chart_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_generate_position_chart_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        out_file = os.path.join(tmpdir, "test_target_chart.png")
        saved_path = generate_position_chart_file(
            out_file,
            symbol="SOL",
            side="BUY/LONG",
            entry_price=145.0,
            current_price=148.0,
            size=10.0,
        )
        
        assert os.path.exists(saved_path)
        assert os.path.getsize(saved_path) > 1000
        with open(saved_path, "rb") as f:
            data = f.read()
            assert data.startswith(b"\x89PNG\r\n\x1a\n")


def test_tg_send_photo_mock():
    fake_png = b"\x89PNG\r\n\x1a\nfakecontent"
    
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        res = tg_send_photo(
            photo=fake_png,
            caption="Test Photo",
            chat_id="123456",
            token="123:ABC",
            reply_markup={"inline_keyboard": []},
        )
        assert res is True
        assert mock_post.called
        assert "api.telegram.org/bot123:ABC/sendPhoto" in mock_post.call_args[0][0]

    # Test failure when no token
    res_no_token = tg_send_photo(fake_png, chat_id="", token="")
    assert res_no_token is False


# =============================================================================
# LIGHTER TELEGRAM BOT ONE-TAP POSITION CONTROLS TESTS
# =============================================================================

@pytest.fixture
def mock_executor():
    executor = MagicMock()
    pos = ActivePosition(
        position_id="pos_eth_101",
        asset="ETH",
        market_index=0,
        side="BUY/LONG",
        entry_price=2600.0,
        size_eth=2.0,
        notional_usd=5200.0,
        tp_pct=2.5,
        sl_pct=1.5,
        tp_price=2665.0,
        sl_price=2561.0,
        is_active=True,
    )
    executor.active_positions = {"pos_eth_101": pos}
    executor.sync_and_adopt_all_live_positions = AsyncMock(return_value={"ETH": 2650.0})
    executor.close_position = AsyncMock(return_value=True)
    executor.amend_trailing_sl = AsyncMock(return_value=True)
    return executor


@pytest.fixture
def bot(mock_executor):
    ctx = {"executor": mock_executor, "is_paper_mode": True}
    return LighterTelegramBot(ctx)


def test_build_positions_keyboard_with_active_positions(bot, mock_executor):
    kb = bot.build_positions_keyboard(mock_executor)
    assert "inline_keyboard" in kb
    rows = kb["inline_keyboard"]
    
    # Check for position control buttons
    all_callbacks = [btn["callback_data"] for row in rows for btn in row]
    assert "pos_be_pos_eth_101" in all_callbacks
    assert "pos_close50_pos_eth_101" in all_callbacks
    assert "pos_tp2_pos_eth_101" in all_callbacks
    assert "pos_chart_pos_eth_101" in all_callbacks

    all_texts = [btn["text"] for row in rows for btn in row]
    assert any("Breakeven SL" in t for t in all_texts)
    assert any("Close 50%" in t for t in all_texts)
    assert any("+2% TP" in t for t in all_texts)
    assert any("Chart" in t for t in all_texts)


def test_build_positions_keyboard_empty(bot):
    empty_executor = MagicMock()
    empty_executor.active_positions = {}
    kb = bot.build_positions_keyboard(empty_executor)
    
    # Returns main keyboard when no active positions
    assert "inline_keyboard" in kb
    all_callbacks = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
    assert "menu_positions" in all_callbacks


@pytest.mark.asyncio
async def test_handle_user_action_positions_display(bot):
    msg, kb = await bot.handle_user_action("/positions", 12345)
    
    assert "ACTIVE POSITIONS" in msg
    assert "ETH" in msg
    assert "Entry: <code>$2,600.00</code>" in msg
    assert "Mark: <code>$2,650.00</code>" in msg
    assert "TP (+2.5%)" in msg
    assert "SL (-1.5%)" in msg
    
    # Inline keyboard contains one-tap control buttons
    all_callbacks = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
    assert "pos_be_pos_eth_101" in all_callbacks
    assert "pos_close50_pos_eth_101" in all_callbacks
    assert "pos_tp2_pos_eth_101" in all_callbacks
    assert "pos_chart_pos_eth_101" in all_callbacks


@pytest.mark.asyncio
async def test_handle_user_action_breakeven_sl(bot, mock_executor):
    pos = mock_executor.active_positions["pos_eth_101"]
    assert pos.sl_price == 2561.0

    msg, kb = await bot.handle_user_action("pos_be_pos_eth_101", 12345)
    
    assert "BREAKEVEN SL ACTIVATED" in msg
    assert "ETH" in msg
    # Long breakeven SL is Entry * 1.001 = 2600.0 * 1.001 = 2602.6
    assert pytest.approx(pos.sl_price, rel=1e-4) == 2602.6
    assert pos.sl_pct == 0.1


@pytest.mark.asyncio
async def test_handle_user_action_breakeven_sl_short(bot, mock_executor):
    short_pos = ActivePosition(
        position_id="pos_tsla_202",
        asset="TSLA",
        market_index=112,
        side="SELL/SHORT",
        entry_price=200.0,
        size_eth=5.0,
        notional_usd=1000.0,
        tp_pct=2.5,
        sl_pct=1.5,
        tp_price=195.0,
        sl_price=203.0,
        is_active=True,
    )
    mock_executor.active_positions["pos_tsla_202"] = short_pos
    
    msg, kb = await bot.handle_user_action("pos_be_pos_tsla_202", 12345)
    assert "BREAKEVEN SL ACTIVATED" in msg
    # Short breakeven SL is Entry * 0.999 = 200.0 * 0.999 = 199.8
    assert pytest.approx(short_pos.sl_price, rel=1e-4) == 199.8


@pytest.mark.asyncio
async def test_handle_user_action_close_50(bot, mock_executor):
    pos = mock_executor.active_positions["pos_eth_101"]
    assert pos.size_eth == 2.0

    msg, kb = await bot.handle_user_action("pos_close50_pos_eth_101", 12345)
    
    assert "PARTIAL CLOSE (50%) EXECUTED" in msg
    assert "Closed Size:</b> <code>1.0</code>" in msg
    assert "Remaining Size:</b> <code>1.0</code>" in msg
    mock_executor.close_position.assert_called_once()
    call_args = mock_executor.close_position.call_args
    assert call_args[0][0] == pos
    assert call_args[1]["qty"] == 1.0


@pytest.mark.asyncio
async def test_handle_user_action_tp_extension(bot, mock_executor):
    pos = mock_executor.active_positions["pos_eth_101"]
    orig_tp = pos.tp_pct  # 2.5%
    orig_tp_price = pos.tp_price

    msg, kb = await bot.handle_user_action("pos_tp2_pos_eth_101", 12345)
    
    assert "TAKE-PROFIT EXTENDED" in msg
    assert pos.tp_pct == orig_tp + 2.0  # 4.5%
    # Long TP = 2600.0 * (1 + 0.045) = 2717.0
    assert pytest.approx(pos.tp_price, rel=1e-4) == 2717.0
    assert pos.tp_price > orig_tp_price


@pytest.mark.asyncio
async def test_handle_user_action_chart_position(bot, mock_executor):
    with patch("lighter_telegram.tg_send_photo") as mock_photo:
        mock_photo.return_value = True
        msg, kb = await bot.handle_user_action("pos_chart_pos_eth_101", 12345)
        
        assert "Visual Chart Dispatched for ETH" in msg
        assert mock_photo.called
        photo_bytes = mock_photo.call_args[0][0]
        assert isinstance(photo_bytes, bytes)
        assert photo_bytes.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_handle_user_action_chart_standalone_ticker(bot):
    with patch("lighter_telegram.tg_send_photo") as mock_photo:
        mock_photo.return_value = True
        msg, kb = await bot.handle_user_action("/chart nvda", 12345)
        
        assert "Market Blueprint Chart Dispatched for NVDA" in msg
        assert mock_photo.called


@pytest.mark.asyncio
async def test_handle_user_action_position_not_found(bot, mock_executor):
    mock_executor.active_positions = {}  # No active positions
    msg_be, _ = await bot.handle_user_action("pos_be_unknown_id", 12345)
    assert "Position Not Found" in msg_be

    msg_c50, _ = await bot.handle_user_action("pos_close50_unknown_id", 12345)
    assert "Position Not Found" in msg_c50

    msg_tp, _ = await bot.handle_user_action("pos_tp2_unknown_id", 12345)
    assert "Position Not Found" in msg_tp
