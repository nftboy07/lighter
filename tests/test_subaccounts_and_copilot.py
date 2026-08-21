#!/usr/bin/env python3
"""
Unit tests for Multi-Subaccount Strategy Sharding and Telegram AI Copilot.
Covers:
1. SubaccountManager routing, collateral tracking, and rebalancing recommendations.
2. TelegramAICopilot natural language command parsing (sniping, risk management, analytics).
3. Integration with LighterTelegramBot.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from subaccount_manager import (
    RebalanceRecommendation,
    SubaccountManager,
    SubaccountProfile,
    SubaccountRole,
    SubaccountState,
)
from telegram_copilot import (
    CopilotIntentType,
    ParsedCommand,
    TelegramAICopilot,
)
from lighter_telegram import LighterTelegramBot
from lighter_news_sniper import ActivePosition


# =============================================================================
# SUBACCOUNT MANAGER TESTS
# =============================================================================

def test_subaccount_manager_default_initialization():
    mgr = SubaccountManager()
    assert len(mgr.profiles) == 3
    
    # Verify Sniper subaccount #737649
    sniper_prof = mgr.get_subaccount(SubaccountRole.SNIPER)
    assert sniper_prof is not None
    assert sniper_prof.account_index == 737649
    assert sniper_prof.target_allocation_pct == 40.0

    # Verify MM subaccount
    mm_prof = mgr.get_subaccount(SubaccountRole.MARKET_MAKER)
    assert mm_prof is not None
    assert mm_prof.account_index == 737650
    assert mm_prof.target_allocation_pct == 40.0

    # Verify Arb subaccount
    arb_prof = mgr.get_subaccount(SubaccountRole.ARBITRAGE)
    assert arb_prof is not None
    assert arb_prof.account_index == 737651
    assert arb_prof.target_allocation_pct == 20.0


def test_subaccount_manager_routing():
    mgr = SubaccountManager()
    
    # Sniper routing
    assert mgr.route_strategy("sniper").account_index == 737649
    assert mgr.route_strategy("news_catalyst").account_index == 737649
    assert mgr.route_strategy("manual_quick_trade").account_index == 737649
    assert mgr.route_strategy(SubaccountRole.SNIPER).account_index == 737649
    
    # MM routing
    assert mgr.route_strategy("market_maker").account_index == 737650
    assert mgr.route_strategy("mm").account_index == 737650
    assert mgr.route_strategy("points_farming").account_index == 737650
    assert mgr.route_strategy(SubaccountRole.MARKET_MAKER).account_index == 737650

    # Arb routing
    assert mgr.route_strategy("arbitrage").account_index == 737651
    assert mgr.route_strategy("cross_dex").account_index == 737651
    assert mgr.route_strategy("funding_harvester").account_index == 737651
    assert mgr.route_strategy(SubaccountRole.ARBITRAGE).account_index == 737651


def test_subaccount_manager_state_updates():
    mgr = SubaccountManager()
    st = mgr.update_state(
        account_index=737649,
        collateral_usd=10.0,
        available_margin_usd=8.0,
        active_positions_count=1,
        total_volume_usd=5000.0,
        unrealized_pnl_usd=12.50,
    )
    
    assert st.collateral_usd == 10.0
    assert st.available_margin_usd == 8.0
    assert st.allocated_margin_usd == 2.0
    assert st.margin_utilization_pct == 20.0
    assert st.active_positions_count == 1
    assert st.total_volume_usd == 5000.0
    assert st.unrealized_pnl_usd == 12.50


def test_subaccount_manager_rebalance_recommendations():
    mgr = SubaccountManager()
    
    # Set unbalanced distribution:
    # Total pool = $100
    # Sniper has $90 (target 40% = $40 -> +$50 surplus)
    # MM has $5 (target 40% = $40 -> -$35 deficit)
    # Arb has $5 (target 20% = $20 -> -$15 deficit)
    mgr.update_state(737649, collateral_usd=90.0, available_margin_usd=90.0)
    mgr.update_state(737650, collateral_usd=5.0, available_margin_usd=5.0)
    mgr.update_state(737651, collateral_usd=5.0, available_margin_usd=5.0)

    recs = mgr.calculate_rebalancing(drift_threshold_pct=0.15, min_transfer_usd=1.0)
    assert len(recs) >= 1
    
    # Verify recommendations shift funds from #737649 to deficit accounts
    from_indices = [r.from_account_index for r in recs]
    to_indices = [r.to_account_index for r in recs]
    assert 737649 in from_indices
    assert 737650 in to_indices or 737651 in to_indices


@pytest.mark.asyncio
async def test_subaccount_manager_transfer_collateral():
    mgr = SubaccountManager()
    mgr.update_state(737649, collateral_usd=10.0, available_margin_usd=10.0)
    mgr.update_state(737650, collateral_usd=2.0, available_margin_usd=2.0)

    res = await mgr.transfer_collateral(737649, 737650, amount_usd=4.0, is_paper=True)
    assert res["success"] is True
    assert res["amount_usd"] == 4.0

    st_sniper = mgr.get_state(737649)
    st_mm = mgr.get_state(737650)
    assert st_sniper.collateral_usd == 6.0
    assert st_mm.collateral_usd == 6.0


def test_subaccount_manager_portfolio_summary_and_html():
    mgr = SubaccountManager()
    mgr.update_state(737649, collateral_usd=5.5208, available_margin_usd=5.5208)
    mgr.update_state(737650, collateral_usd=5.0, available_margin_usd=5.0)
    mgr.update_state(737651, collateral_usd=2.5, available_margin_usd=2.5)

    summary = mgr.get_portfolio_summary()
    assert pytest.approx(summary["total_collateral_usd"], rel=1e-3) == 13.0208
    assert summary["subaccounts_count"] == 3

    html_report = mgr.format_subaccounts_report_html()
    assert "MULTI-SUBACCOUNT STRATEGY SHARDING" in html_report
    assert "Sniper Shard" in html_report
    assert "Market Maker Shard" in html_report
    assert "Arbitrage Shard" in html_report


# =============================================================================
# TELEGRAM AI COPILOT NLP PARSING TESTS
# =============================================================================

@pytest.fixture
def copilot():
    return TelegramAICopilot()


def test_copilot_parse_snipe_commands(copilot):
    # Test "snipe $200 long SOL"
    cmd1 = copilot.parse_command("snipe $200 long SOL")
    assert cmd1.intent == CopilotIntentType.SNIPE_TRADE
    assert cmd1.asset == "SOL"
    assert cmd1.is_short is False
    assert cmd1.amount_usd == 200.0

    # Test "buy 50 USD ETH"
    cmd2 = copilot.parse_command("buy 50 USD ETH")
    assert cmd2.intent == CopilotIntentType.SNIPE_TRADE
    assert cmd2.asset == "ETH"
    assert cmd2.is_short is False
    assert cmd2.amount_usd == 50.0

    # Test "short $100 NVDA"
    cmd3 = copilot.parse_command("short $100 NVDA")
    assert cmd3.intent == CopilotIntentType.SNIPE_TRADE
    assert cmd3.asset == "NVDA"
    assert cmd3.is_short is True
    assert cmd3.amount_usd == 100.0

    # Test "go long sol 25 usd"
    cmd4 = copilot.parse_command("go long sol 25 usd")
    assert cmd4.intent == CopilotIntentType.SNIPE_TRADE
    assert cmd4.asset == "SOL"
    assert cmd4.is_short is False
    assert cmd4.amount_usd == 25.0


def test_copilot_parse_risk_management_commands(copilot):
    # Test "breakeven TRUMP"
    cmd1 = copilot.parse_command("breakeven TRUMP")
    assert cmd1.intent == CopilotIntentType.BREAKEVEN
    assert cmd1.asset == "TRUMP"

    # Test "be sol"
    cmd2 = copilot.parse_command("be sol")
    assert cmd2.intent == CopilotIntentType.BREAKEVEN
    assert cmd2.asset == "SOL"

    # Test "close 50% RIVER"
    cmd3 = copilot.parse_command("close 50% RIVER")
    assert cmd3.intent == CopilotIntentType.PARTIAL_CLOSE
    assert cmd3.asset == "RIVER"
    assert cmd3.percentage == 50.0

    # Test "take 50% profit sol"
    cmd4 = copilot.parse_command("take 50% profit sol")
    assert cmd4.intent == CopilotIntentType.PARTIAL_CLOSE
    assert cmd4.asset == "SOL"
    assert cmd4.percentage == 50.0

    # Test "trim 25% eth"
    cmd5 = copilot.parse_command("trim 25% eth")
    assert cmd5.intent == CopilotIntentType.PARTIAL_CLOSE
    assert cmd5.asset == "ETH"
    assert cmd5.percentage == 25.0

    # Test "flatten all"
    cmd6 = copilot.parse_command("flatten all")
    assert cmd6.intent == CopilotIntentType.CLOSE_ALL

    # Test "emergency exit"
    cmd7 = copilot.parse_command("emergency exit")
    assert cmd7.intent == CopilotIntentType.CLOSE_ALL

    # Test "close sol"
    cmd8 = copilot.parse_command("close sol")
    assert cmd8.intent == CopilotIntentType.CLOSE_POSITION
    assert cmd8.asset == "SOL"

    # Test "tp 3.5 sol"
    cmd9 = copilot.parse_command("tp 3.5 sol")
    assert cmd9.intent == CopilotIntentType.SET_TP_SL
    assert cmd9.asset == "SOL"
    assert cmd9.tp_pct == 3.5


def test_copilot_parse_status_and_analytics(copilot):
    # Test "how much volume today?"
    cmd1 = copilot.parse_command("how much volume today?")
    assert cmd1.intent == CopilotIntentType.VOLUME_QUERY

    # Test "show funding opportunities"
    cmd2 = copilot.parse_command("show funding opportunities")
    assert cmd2.intent == CopilotIntentType.FUNDING_ARBITRAGE

    # Test "report"
    cmd3 = copilot.parse_command("report")
    assert cmd3.intent == CopilotIntentType.DAILY_REPORT

    # Test "what is my balance?"
    cmd4 = copilot.parse_command("what is my balance?")
    assert cmd4.intent == CopilotIntentType.BALANCE_QUERY

    # Test "collateral rebalance"
    cmd5 = copilot.parse_command("collateral rebalance")
    assert cmd5.intent == CopilotIntentType.COLLATERAL_REBALANCE

    # Test "whale radar"
    cmd6 = copilot.parse_command("whale radar")
    assert cmd6.intent == CopilotIntentType.WHALE_RADAR

    # Test "sources"
    cmd7 = copilot.parse_command("sources")
    assert cmd7.intent == CopilotIntentType.SOURCES_QUERY

    # Test "status"
    cmd8 = copilot.parse_command("status")
    assert cmd8.intent == CopilotIntentType.STATUS_QUERY

    # Test "help"
    cmd9 = copilot.parse_command("help")
    assert cmd9.intent == CopilotIntentType.HELP


# =============================================================================
# COPILOT EXECUTION & TELEGRAM BOT INTEGRATION TESTS
# =============================================================================

@pytest.fixture
def mock_executor():
    executor = MagicMock()
    pos = ActivePosition(
        position_id="pos_sol_101",
        asset="SOL",
        market_index=2,
        side="BUY/LONG",
        entry_price=145.0,
        size_eth=1.0,
        notional_usd=145.0,
        tp_pct=2.5,
        sl_pct=1.5,
        tp_price=148.625,
        sl_price=142.825,
        is_active=True,
    )
    executor.active_positions = {"pos_sol_101": pos}
    executor.sync_and_adopt_all_live_positions = AsyncMock(return_value={"SOL": 146.50, "ETH": 2650.0})
    executor.execute_trade = AsyncMock(return_value={"status": "FILLED", "order_id": 999})
    executor.close_position = AsyncMock(return_value=True)
    executor.close_all_positions = AsyncMock(return_value=1)
    executor.amend_trailing_sl = AsyncMock(return_value=True)
    return executor


@pytest.fixture
def tg_bot(mock_executor):
    ctx = {
        "executor": mock_executor,
        "is_paper_mode": True,
    }
    return LighterTelegramBot(ctx)


@pytest.mark.asyncio
async def test_tg_bot_natural_language_snipe(tg_bot, mock_executor):
    # Send natural language order: "snipe $200 long SOL"
    msg, kb = await tg_bot.handle_user_action("snipe $200 long SOL", user_id=12345)
    
    assert "COPILOT BUY/LONG EXECUTED" in msg
    assert "SOL" in msg
    assert "$200.00 USD" in msg
    assert "Subaccount Shard" in msg
    mock_executor.execute_trade.assert_called_once()


@pytest.mark.asyncio
async def test_tg_bot_natural_language_breakeven(tg_bot, mock_executor):
    pos = mock_executor.active_positions["pos_sol_101"]
    assert pos.sl_price == 142.825

    msg, kb = await tg_bot.handle_user_action("breakeven SOL", user_id=12345)
    
    assert "BREAKEVEN SL ACTIVATED" in msg
    assert "SOL" in msg
    assert pytest.approx(pos.sl_price, rel=1e-3) == 145.145  # 145 * 1.001
    assert pos.sl_pct == 0.1


@pytest.mark.asyncio
async def test_tg_bot_natural_language_partial_close(tg_bot, mock_executor):
    pos = mock_executor.active_positions["pos_sol_101"]
    assert pos.size_eth == 1.0

    msg, kb = await tg_bot.handle_user_action("close 50% SOL", user_id=12345)
    
    assert "PARTIAL CLOSE (50%) EXECUTED" in msg
    assert "SOL" in msg
    assert pytest.approx(pos.size_eth, rel=1e-4) == 0.5
    mock_executor.close_position.assert_called_once()


@pytest.mark.asyncio
async def test_tg_bot_natural_language_flatten_all(tg_bot, mock_executor):
    msg, kb = await tg_bot.handle_user_action("flatten all", user_id=12345)
    
    assert "ALL POSITIONS CLOSED AT MARKET" in msg
    mock_executor.close_all_positions.assert_called_once()


@pytest.mark.asyncio
async def test_tg_bot_subaccount_sharding_menu(tg_bot):
    msg, kb = await tg_bot.handle_user_action("menu_subaccounts", user_id=12345)
    
    assert "MULTI-SUBACCOUNT STRATEGY SHARDING" in msg
    assert "Sniper Shard" in msg
    assert "Market Maker Shard" in msg
    assert "Arbitrage Shard" in msg
    
    # Check callback buttons
    all_callbacks = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
    assert "menu_exec_rebalance" in all_callbacks
    assert "menu_funding" in all_callbacks


@pytest.mark.asyncio
async def test_tg_bot_funding_arbitrage_query(tg_bot):
    msg, kb = await tg_bot.handle_user_action("show funding opportunities", user_id=12345)
    
    assert "CROSS-DEX FUNDING RATE & ARB HARVEST" in msg
    assert "Subaccount Arb (#737651)" in msg
    assert "Spread" in msg


def test_copilot_voice_and_transcripts(copilot):
    # Test spoken words "two hundred dollars"
    cmd1 = copilot.parse_command("snipe two hundred dollars long sol")
    assert cmd1.intent == CopilotIntentType.SNIPE_TRADE
    assert cmd1.asset == "SOL"
    assert cmd1.amount_usd == 200.0

    # Test "scale out half"
    cmd2 = copilot.parse_command("scale out half of eth")
    assert cmd2.intent == CopilotIntentType.PARTIAL_CLOSE
    assert cmd2.asset == "ETH"
    assert cmd2.percentage == 50.0


@pytest.mark.asyncio
async def test_subaccount_fetch_balances_api():
    mgr = SubaccountManager()
    
    mock_payload = {
        "sub_accounts": [
            {"index": 737649, "collateral": "15.7500", "pending_order_count": 0, "status": 1},
            {"index": 737650, "collateral": "8.2500", "pending_order_count": 2, "status": 1},
        ]
    }
    
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=mock_payload)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    states = await mgr.fetch_subaccount_balances(mock_session)
    assert states[737649].collateral_usd == 15.75
    assert states[737650].collateral_usd == 8.25
    assert states[737650].pending_orders_count == 2


@pytest.mark.asyncio
async def test_subaccount_transfer_validation():
    mgr = SubaccountManager()
    mgr.update_state(737649, collateral_usd=2.0, available_margin_usd=2.0)
    mgr.update_state(737650, collateral_usd=1.0, available_margin_usd=1.0)

    # Insufficient funds error
    res_err = await mgr.transfer_collateral(737649, 737650, amount_usd=10.0)
    assert res_err["success"] is False
    assert "Insufficient available collateral" in res_err["error"]

    # Non-existent account error
    res_err2 = await mgr.transfer_collateral(999999, 737650, amount_usd=1.0)
    assert res_err2["success"] is False


@pytest.mark.asyncio
async def test_tg_bot_rebalance_execution_callbacks(tg_bot):
    # Set unbalanced state
    tg_bot.subaccount_mgr.update_state(737649, collateral_usd=50.0, available_margin_usd=50.0)
    tg_bot.subaccount_mgr.update_state(737650, collateral_usd=1.0, available_margin_usd=1.0)
    tg_bot.subaccount_mgr.update_state(737651, collateral_usd=1.0, available_margin_usd=1.0)

    # Check recommendations view
    msg_rec, kb_rec = await tg_bot.handle_user_action("menu_rebalance", user_id=12345)
    assert "COLLATERAL REBALANCE RECOMMENDATIONS" in msg_rec
    assert "Action #1" in msg_rec

    # Execute rebalance
    msg_exec, kb_exec = await tg_bot.handle_user_action("menu_exec_rebalance", user_id=12345)
    assert "SUBACCOUNT REBALANCE EXECUTED" in msg_exec
    assert "Transferred" in msg_exec


@pytest.mark.asyncio
async def test_tg_bot_copilot_analytics_and_controls(tg_bot):
    # Test volume query
    msg_vol, _ = await tg_bot.handle_user_action("how much volume today?", user_id=12345)
    assert "FARMED VOLUME" in msg_vol

    # Test bot pause & resume
    msg_pause, _ = await tg_bot.handle_user_action("pause bot", user_id=12345)
    assert "Bot Paused" in msg_pause

    msg_resume, _ = await tg_bot.handle_user_action("resume bot", user_id=12345)
    assert "Bot Resumed" in msg_resume

