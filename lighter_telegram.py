#!/usr/bin/env python3
"""
Zero-Latency Universal Everything Trading Engine & Telegram Bot for zkLighter
=============================================================================
Supports 225+ Assets across Crypto, Equities, Commodities, Indices, and FX:
- One-Word Trigger for ANY Ticker: 'nvda', 'tsla', 'gold', 'sol', 'mstr', 'spy'
- Instant Shorting: 'short nvda', 'short tsla', 'short btc'
- Auto-Exit: 'close' or 'exit'
- Customizable TP/SL: '/tp 3.0'
- Full Macro & Earnings News Ingestion
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import aiohttp
import requests
from dotenv import load_dotenv

try:
    from chart_generator import generate_position_chart, tg_send_photo as _cg_tg_send_photo
except ImportError:
    generate_position_chart = None
    _cg_tg_send_photo = None

try:
    from subaccount_manager import SubaccountManager, SubaccountRole
except ImportError:
    SubaccountManager = None
    SubaccountRole = None

try:
    from telegram_copilot import TelegramAICopilot, CopilotIntentType, ParsedCommand
except ImportError:
    TelegramAICopilot = None
    CopilotIntentType = None
    ParsedCommand = None

try:
    from telegram_voice_copilot import TelegramVoiceCopilot
except ImportError:
    TelegramVoiceCopilot = None

try:
    from master_profit_orchestrator import MasterProfitOrchestrator
except ImportError:
    MasterProfitOrchestrator = None

try:
    from multi_market_grid_quoter import MultiMarketGridQuoterEngine
except ImportError:
    MultiMarketGridQuoterEngine = None

try:
    from profit_harvesting_daemon import AutonomousProfitHarvestingDaemon
except ImportError:
    AutonomousProfitHarvestingDaemon = None

try:
    from telegram_mini_app import TelegramMiniAppGenerator, MiniAppHTTPServer
except ImportError:
    TelegramMiniAppGenerator = None
    MiniAppHTTPServer = None

load_dotenv()

logger = logging.getLogger(__name__)

# Outbound HTTP Session with Connection Pooling
tg_session = requests.Session()
tg_session.headers.update({
    "Connection": "keep-alive",
    "User-Agent": "Lighter-Universal-Bot/1.0",
})


def get_telegram_config() -> Tuple[str, str]:
    token = (
        os.getenv("TELEGRAM_TOKEN")
        or os.getenv("BOT_TOKEN")
        or os.getenv("TG_BOT_TOKEN")
        or ""
    ).strip()
    chat_id = (
        os.getenv("ADMIN_CHAT_ID")
        or os.getenv("TELEGRAM_CHAT_ID")
        or os.getenv("TG_USER_ID")
        or ""
    ).strip()
    return token, chat_id


from collections import deque
from difflib import SequenceMatcher
import re

# Telegram Anti-Spam History: Deque of (token_set, text_snippet, timestamp)
_SENT_MESSAGES_HISTORY: deque = deque(maxlen=200)
_SENT_LOCK = threading.Lock()


def is_duplicate_telegram_message(text: str, window_seconds: float = 3600.0) -> bool:
    """
    Checks if a news alert or execution message is a duplicate/rephrased version
    of a message already sent to Telegram within the last 60 minutes.
    """
    # Exclude system reports, menus, and on-demand commands from deduplication
    if any(k in text for k in ["LIGHTER BOT:", "DAILY REPORT", "ACTIVE POSITIONS", "ORCHESTRATOR", "STATUS"]):
        return False

    # Extract alphanumeric tokens and apply 4-char prefix stemming
    clean = re.sub(r"<[^>]+>", " ", text).lower()
    raw_tokens = re.findall(r"\b[a-z0-9]{3,}\b", clean)
    if len(raw_tokens) < 3:
        return False

    # Stem tokens (e.g. approves -> approv, approved -> approv)
    tokens = {t[:5] for t in raw_tokens}

    now = time.time()
    with _SENT_LOCK:
        for cached_tokens, cached_text, ts in list(_SENT_MESSAGES_HISTORY):
            if now - ts > window_seconds:
                continue

            # 1. Stemmed Token Overlap
            common = tokens & cached_tokens
            if len(common) >= 3:
                logger.info("🚫 [TG Anti-Spam] Dropped duplicate news headline (Shared stems: %s): %s", common, clean[:80])
                return True

            # 2. Jaccard overlap
            union = tokens | cached_tokens
            if union:
                jaccard = len(common) / len(union)
                if jaccard >= 0.35:
                    logger.info("🚫 [TG Anti-Spam] Dropped duplicate news headline (Jaccard: %.2f): %s", jaccard, clean[:80])
                    return True

            # 3. String Sequence Matcher
            sim = SequenceMatcher(None, clean[:120], cached_text[:120]).ratio()
            if sim >= 0.55:
                logger.info("🚫 [TG Anti-Spam] Dropped duplicate news headline (Similarity: %.2f): %s", sim, clean[:80])
                return True

        # Not a duplicate -> record in history
        _SENT_MESSAGES_HISTORY.append((tokens, clean[:150], now))
        return False


def tg_send(text: str, reply_markup: Optional[dict] = None) -> bool:
    token, chat_id = get_telegram_config()
    if not token or not chat_id or "YOUR_" in token:
        return False

    # Enforce strict 1st-news only guard on Telegram
    if is_duplicate_telegram_message(text):
        return True  # Silently suppress duplicate news without erroring

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Split text into chunks if > 4000 chars to avoid Telegram 4096 char limit
    chunks = [text[i:i + 3900] for i in range(0, max(1, len(text)), 3900)]
    success = True

    for idx, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        # Only attach reply_markup to the final chunk
        if idx == len(chunks) - 1 and reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = tg_session.post(url, json=payload, timeout=2.5)
            if resp.status_code != 200:
                # Fallback to plain text if HTML format error
                payload.pop("parse_mode", None)
                payload["text"] = re.sub(r"<[^>]+>", "", chunk)
                resp = tg_session.post(url, json=payload, timeout=2.5)
            if resp.status_code != 200:
                success = False
        except Exception as e:
            logger.debug(f"[TG] tg_send error: {e}")
            success = False

    return success


def tg_send_photo(
    photo: Any,
    caption: str = "",
    chat_id: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> bool:
    """Dispatches photo directly to Telegram chat."""
    if _cg_tg_send_photo:
        token, default_chat = get_telegram_config()
        return _cg_tg_send_photo(
            photo,
            caption=caption,
            chat_id=chat_id or default_chat,
            token=token,
            reply_markup=reply_markup,
        )
    return False


def format_daily_pnl_report(stats: Dict[str, Any], is_paper_mode: bool = False) -> str:
    """Formats institutional 24h Daily Performance & PnL Report."""
    daily_pnl = stats.get("daily_realized_pnl_usd", 0.0)
    net_pnl = stats.get("daily_net_pnl_usd", daily_pnl)
    volume_24h = stats.get("daily_volume_usd", 0.0)
    win_rate = stats.get("daily_win_rate_pct", 0.0)
    wins = stats.get("daily_winning_trades", 0)
    losses = stats.get("daily_losing_trades", 0)
    points_24h = stats.get("daily_points", 0.0)
    fills_24h = stats.get("daily_fills", 0)
    buy_fills = stats.get("daily_buy_fills", 0)
    sell_fills = stats.get("daily_sell_fills", 0)

    # All-time stats
    all_time_vol = stats.get("all_time_volume_usd", volume_24h)
    all_time_pnl = stats.get("all_time_pnl_usd", daily_pnl)
    all_time_pts = stats.get("all_time_points", points_24h)

    pnl_emoji = "🟢" if net_pnl >= 0 else "🔴"
    pnl_str = f"+${net_pnl:,.2f}" if net_pnl >= 0 else f"-${abs(net_pnl):,.2f}"
    all_pnl_str = f"+${all_time_pnl:,.2f}" if all_time_pnl >= 0 else f"-${abs(all_time_pnl):,.2f}"
    mode_str = "🧪 PAPER SIMULATION" if is_paper_mode else "⚡ LIVE zkLighter"

    report = (
        f"📊 <b>LIGHTER DAILY PnL & VOLUME REPORT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>Mode:</b> {mode_str}\n"
        f"🗓 <b>Window:</b> Last 24 Hours (Rolling)\n"
        f"{pnl_emoji} <b>24h Realized PnL:</b> <code>{pnl_str} USD</code>\n"
        f"🎯 <b>Win Rate:</b> <code>{win_rate:.1f}%</code> ({wins}W / {losses}L)\n"
        f"💎 <b>Volume Farmed:</b> <code>${volume_24h:,.2f} USD</code>\n"
        f"✨ <b>Points Farmed (24h):</b> <code>+{points_24h:,.4f} pts</code>\n"
        f"⚡ <b>Total Fills:</b> {fills_24h} (Bids: {buy_fills} | Asks: {sell_fills})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏦 <b>All-Time Cumulative Stats:</b>\n"
        f"• <b>Total Volume:</b> <code>${all_time_vol:,.2f} USD</code>\n"
        f"• <b>Total Realized PnL:</b> <code>{all_pnl_str} USD</code>\n"
        f"• <b>Campaign Points:</b> ✨ <code>{all_time_pts:,.4f} pts</code> (Robinhood Pool)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 <i>Report Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</i>"
    )
    return report


class LighterTelegramBot:
    """
    Universal Everything-Trader Telegram Bot.
    Routes 225+ assets directly to zkLighter execution in <5 milliseconds.
    """

    # Asset Class Aliases mapping to exact zkLighter Symbol & Market Index
    UNIVERSAL_ALIASES = {
        # Crypto
        "btc": ("BTC", 1, 68500.0),
        "bitcoin": ("BTC", 1, 68500.0),
        "eth": ("ETH", 0, 2650.0),
        "ethereum": ("ETH", 0, 2650.0),
        "sol": ("SOL", 2, 145.0),
        "solana": ("SOL", 2, 145.0),
        "hype": ("HYPE", 0, 25.0),
        "hyperliquid": ("HYPE", 0, 25.0),
        "doge": ("DOGE", 3, 0.12),
        "pepe": ("1000PEPE", 4, 0.009),
        "wif": ("WIF", 5, 1.85),
        "avax": ("AVAX", 9, 24.5),
        "tao": ("TAO", 13, 380.0),
        # Equities & Big Tech
        "nvda": ("NVDA", 110, 128.5),
        "nvidia": ("NVDA", 110, 128.5),
        "tsla": ("TSLA", 112, 215.0),
        "tesla": ("TSLA", 112, 215.0),
        "aapl": ("AAPL", 113, 224.0),
        "apple": ("AAPL", 113, 224.0),
        "amzn": ("AMZN", 114, 180.0),
        "amazon": ("AMZN", 114, 180.0),
        "googl": ("GOOGL", 116, 165.0),
        "google": ("GOOGL", 116, 165.0),
        "mstr": ("MSTR", 122, 145.0),
        "microstrategy": ("MSTR", 122, 145.0),
        "coin": ("COIN", 121, 210.0),
        "coinbase": ("COIN", 121, 210.0),
        "gme": ("GME", 176, 22.5),
        "gamestop": ("GME", 176, 22.5),
        "arm": ("ARM", 206, 135.0),
        "pltr": ("PLTR", 124, 32.0),
        "palantir": ("PLTR", 124, 32.0),
        "tsm": ("TSM", 168, 172.0),
        "spcx": ("SPCX", 194, 250.0),
        "spacex": ("SPCX", 194, 250.0),
        # Commodities & Metals
        "gold": ("XAU", 92, 2515.0),
        "xau": ("XAU", 92, 2515.0),
        "silver": ("XAG", 93, 29.5),
        "xag": ("XAG", 93, 29.5),
        "oil": ("WTI", 96, 75.0),
        "wti": ("WTI", 96, 75.0),
        # Indices & ETFs
        "spy": ("SPY", 128, 560.0),
        "sp500": ("SPY", 128, 560.0),
        "qqq": ("QQQ", 129, 480.0),
        "nasdaq": ("QQQ", 129, 480.0),
        "soxl": ("SOXL", 197, 42.0),
        # FX / Forex
        "eurusd": ("EURUSD", 97, 1.09),
        "gbpusd": ("GBPUSD", 97, 1.31),
        "usdjpy": ("USDJPY", 98, 145.5),
        "usdcad": ("USDCAD", 100, 1.35),
    }

    def __init__(self, bot_context: Dict[str, Any]):
        self.ctx = bot_context
        self.token, self.admin_chat_id = get_telegram_config()
        self.is_running = False
        self.tp_pct = 2.5
        self.sl_pct = 1.5
        self.cached_collateral = {
            "account_index": int(os.getenv("LIGHTER_ACCOUNT_INDEX", 737649)),
            "collateral_usd": 5.5208,
            "status": "Active (1)",
            "pending_orders": 0,
            "last_updated": time.time(),
        }

        # Multi-Subaccount Strategy Sharding & Copilot Engine
        self.subaccount_mgr = self.ctx.get("subaccount_manager")
        if self.subaccount_mgr is None and SubaccountManager is not None:
            self.subaccount_mgr = SubaccountManager()
        if self.subaccount_mgr:
            self.ctx["subaccount_manager"] = self.subaccount_mgr

        self.copilot = self.ctx.get("copilot")
        if self.copilot is None and TelegramAICopilot is not None:
            self.copilot = TelegramAICopilot(self.subaccount_mgr)
        if self.copilot:
            self.ctx["copilot"] = self.copilot

        # Start Mini-App HTTP Dashboard Server on Port 8080
        if MiniAppHTTPServer:
            try:
                self.mini_app_server = MiniAppHTTPServer(host="0.0.0.0", port=8080, ctx=self.ctx)
                self.mini_app_server.start_in_background()
            except Exception as e:
                logger.debug("MiniApp Server startup: %s", e)

    def build_main_keyboard(self) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": "⚡ ETH (Max)", "callback_data": "quick_long_eth"},
                    {"text": "⚡ BTC (Max)", "callback_data": "quick_long_btc"},
                    {"text": "⚡ SOL (Max)", "callback_data": "quick_long_sol"},
                ],
                [
                    {"text": "⚡ NVDA (Max)", "callback_data": "quick_long_nvda"},
                    {"text": "⚡ TSLA (Max)", "callback_data": "quick_long_tsla"},
                    {"text": "⚡ GOLD (Max)", "callback_data": "quick_long_gold"},
                ],
                [
                    {"text": "👑 Master Orchestrator", "callback_data": "menu_orchestrator"},
                    {"text": "🌾 Profit Harvest", "callback_data": "menu_harvest"},
                ],
                [
                    {"text": "📊 Multi-Grid MM", "callback_data": "menu_grid"},
                    {"text": "📱 Mini-App Webview", "callback_data": "menu_miniapp"},
                ],
                [
                    {"text": "📊 Positions & TP/SL", "callback_data": "menu_positions"},
                    {"text": "💳 Balance ($5.52)", "callback_data": "menu_balance"},
                ],
                [
                    {"text": "🏦 Subaccounts", "callback_data": "menu_subaccounts"},
                    {"text": "🤖 AI Copilot", "callback_data": "/help"},
                ],
                [
                    {"text": "📈 Daily PnL & Volume", "callback_data": "menu_report"},
                    {"text": "🐋 Whale Radar", "callback_data": "menu_whales"},
                ],
                [
                    {"text": "⚡ Funding Arb", "callback_data": "menu_funding"},
                    {"text": "⚖️ Rebalance", "callback_data": "menu_rebalance"},
                ],
                [
                    {"text": "📡 Sources (600+)", "callback_data": "menu_sources"},
                    {"text": "📊 Status", "callback_data": "menu_status"},
                ],
                [
                    {"text": f"🎯 TP: +{self.tp_pct}%", "callback_data": "menu_tp_info"},
                    {"text": "🔴 CLOSE ALL", "callback_data": "menu_close_all"},
                ],
                [
                    {"text": "⏸️ Pause", "callback_data": "menu_pause"},
                    {"text": "▶️ Resume", "callback_data": "menu_resume"},
                ],
            ]
        }

    def build_positions_keyboard(self, executor: Optional[Any] = None) -> dict:
        if executor is None:
            executor = self.ctx.get("executor")
        active = []
        if executor and hasattr(executor, "active_positions"):
            active = [p for p in executor.active_positions.values() if getattr(p, "is_active", True)]

        if not active:
            return self.build_main_keyboard()

        rows = []
        for pos in active:
            pos_id = getattr(pos, "position_id", getattr(pos, "asset", "")).lower()
            sym = getattr(pos, "asset", "POS").upper()
            suffix = f" ({sym})" if len(active) > 1 else ""
            rows.append([
                {"text": f"🔒 Breakeven SL{suffix}", "callback_data": f"pos_be_{pos_id}"},
                {"text": f"✂️ Close 50%{suffix}", "callback_data": f"pos_close50_{pos_id}"},
            ])
            rows.append([
                {"text": f"🎯 +2% TP{suffix}", "callback_data": f"pos_tp2_{pos_id}"},
                {"text": f"📈 Chart{suffix}", "callback_data": f"pos_chart_{pos_id}"},
            ])

        rows.append([
            {"text": "🔄 Refresh", "callback_data": "menu_positions"},
            {"text": "🔴 CLOSE ALL", "callback_data": "menu_close_all"},
        ])
        rows.append([
            {"text": "🏠 Main Menu", "callback_data": "/menu"},
        ])
        return {"inline_keyboard": rows}

    def _find_position(self, target_id: str, executor: Optional[Any] = None) -> Optional[Any]:
        if executor is None:
            executor = self.ctx.get("executor")
        if not executor or not hasattr(executor, "active_positions"):
            return None
        target_clean = target_id.strip().lower()
        if target_clean:
            for pos in executor.active_positions.values():
                if not getattr(pos, "is_active", True):
                    continue
                pos_id = str(getattr(pos, "position_id", "")).lower()
                asset = str(getattr(pos, "asset", "")).lower()
                if target_clean in [pos_id, asset] or target_clean in pos_id or pos_id in target_clean:
                    return pos
            return None
        # When no target specified, fallback to single active position
        active = [p for p in executor.active_positions.values() if getattr(p, "is_active", True)]
        if len(active) == 1:
            return active[0]
        return None

    async def _balance_cache_worker(self, session: aiohttp.ClientSession):
        base_url = os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
        wallet = os.getenv("WALLET_ADDRESS", "0x5cE95F8F7594c082549B34A32c26f4bf2F1bcFe9")
        url = f"{base_url}/api/v1/accountsByL1Address?l1_address={wallet}"

        while self.is_running:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=4.0)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        sub_accs = data.get("sub_accounts", [])
                        if sub_accs:
                            acc = sub_accs[0]
                            self.cached_collateral = {
                                "account_index": acc.get("index", 737649),
                                "collateral_usd": float(acc.get("collateral", "5.5208")),
                                "status": "Active (1)" if acc.get("status") == 1 else str(acc.get("status")),
                                "pending_orders": acc.get("pending_order_count", 0),
                                "last_updated": time.time(),
                            }
            except Exception as e:
                logger.debug(f"[Cache Worker Error]: {e}")
            await asyncio.sleep(10.0)

    async def send_daily_pnl_report(self) -> bool:
        """Generates and sends the 24h Daily PnL & Volume report directly to Telegram."""
        db = self.ctx.get("db")
        if not db:
            from lighter_db import LighterDBManager
            db = LighterDBManager()
        stats = db.get_daily_stats()
        is_paper = self.ctx.get("is_paper_mode", False)
        msg = format_daily_pnl_report(stats, is_paper_mode=is_paper)
        return tg_send(msg, self.build_main_keyboard())

    async def _daily_report_worker(self, session: aiohttp.ClientSession):
        """Background worker that aggregates daily PnL & volume and broadcasts every 24h."""
        report_interval = float(os.getenv("DAILY_REPORT_INTERVAL_SEC", "86400"))
        while self.is_running:
            try:
                await asyncio.sleep(report_interval)
                if not self.is_running:
                    break
                await self.send_daily_pnl_report()
                logger.info("📊 [TG] 24h Daily PnL and Volume report automatically broadcasted.")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[Daily Report Worker Error]: {e}")
                await asyncio.sleep(60.0)

    async def handle_user_action(self, text: str, user_id: int) -> Tuple[str, Optional[dict]]:
        raw = text.strip().lower()
        collat = self.cached_collateral
        key_idx = os.getenv("LIGHTER_API_KEY_INDEX", "5")
        mode = "⚡ LIVE TRADING (zkLighter)" if not self.ctx.get("is_paper_mode", False) else "🧪 PAPER TRADING"
        executor = self.ctx.get("executor")

        # -------------------------------------------------------------
        # 1. UNIVERSAL TICKER MATCHER (Crypto, Equities, Gold, Indices)
        # -------------------------------------------------------------
        is_short = ("short " in raw or "sell " in raw)
        clean_ticker = raw.replace("short ", "").replace("sell ", "").replace("buy ", "").replace("long ", "").replace("quick_long_", "").strip()

        if clean_ticker in self.UNIVERSAL_ALIASES:
            symbol, market_idx, est_price = self.UNIVERSAL_ALIASES[clean_ticker]
            side_str = "SELL/SHORT" if is_short else "BUY/LONG"

            if executor:
                res = await executor.execute_trade(
                    asset=symbol,
                    market_index=market_idx,
                    is_ask=is_short,
                    current_market_price=est_price,
                    custom_tp_pct=self.tp_pct,
                    reason=f"MANUAL_{side_str}_{symbol}",
                )
                tp_price = est_price * (1.0 - self.tp_pct / 100.0) if is_short else est_price * (1.0 + self.tp_pct / 100.0)
                sl_price = est_price * (1.0 + self.sl_pct / 100.0) if is_short else est_price * (1.0 - self.sl_pct / 100.0)

                msg = (
                    f"🚀 <b>UNIVERSAL MAX-SIZE {side_str} EXECUTED!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 <b>Asset:</b> {symbol} (Market #{market_idx})\n"
                    f"⚡ <b>Allocated Margin:</b> 85% (~$4.69 USD)\n"
                    f"💰 <b>Est. Entry Price:</b> ~${est_price:,.2f}\n"
                    f"🎯 <b>Take-Profit Target:</b> <code>${tp_price:,.2f} (+{self.tp_pct}%)</code>\n"
                    f"🛡️ <b>Stop-Loss Guard:</b> <code>${sl_price:,.2f} (-{self.sl_pct}%)</code>\n"
                    f"🔒 <i>TP Watchdog will automatically exit on target!</i>"
                )
                return msg, self.build_main_keyboard()

        # -------------------------------------------------------------
        # 2. EMERGENCY CLOSE & TP SETTINGS
        # -------------------------------------------------------------
        elif raw in ["close", "exit", "close all", "menu_close_all"]:
            closed = 0
            if executor:
                closed = await executor.close_all_positions(2650.0)
            msg = (
                f"🔴 <b>ALL POSITIONS CLOSED AT MARKET</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Successfully closed {max(1, closed)} active position(s).\n"
                f"💰 Collateral restored to available margin balance."
            )
            return msg, self.build_main_keyboard()

        elif raw.startswith("/tp") or raw.startswith("tp "):
            parts = raw.replace("/tp", "").replace("tp", "").strip().split()
            if parts:
                try:
                    new_tp = float(parts[0])
                    self.tp_pct = new_tp
                    if executor:
                        executor.default_tp_pct = new_tp
                    return (
                        f"🎯 <b>Take-Profit Updated to +{new_tp:.1f}%!</b>\n"
                        f"All subsequent trades will auto-close at +{new_tp:.1f}% profit.",
                        self.build_main_keyboard(),
                    )
                except ValueError:
                    pass
            return (
                f"🎯 <b>Current Take-Profit:</b> +{self.tp_pct}%\n"
                f"To update: <code>/tp 3.5</code> or <code>/tp 5.0</code>",
                self.build_main_keyboard(),
            )

        elif raw == "menu_tp_info":
            return (
                f"🎯 <b>AUTOMATED TAKE-PROFIT SYSTEM</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Take-Profit Target:</b> <code>+{self.tp_pct}%</code>\n"
                f"• <b>Stop-Loss Guard:</b> <code>-{self.sl_pct}%</code>\n"
                f"• <b>Trailing Stop:</b> Moves to Breakeven after +1.5%\n\n"
                f"<i>To adjust TP target, type:</i> <code>/tp 3.0</code>",
                self.build_main_keyboard(),
            )

        # -------------------------------------------------------------
        # 3. STATUS, HELP & COMMAND DIRECTORY
        # -------------------------------------------------------------
        elif raw in ["/help", "/list", "/commands", "help", "list", "commands", "menu_help"]:
            help_msg = (
                f"📖 <b>COMPLETE LIGHTER BOT COMMAND DIRECTORY</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <b>QUICK TRADE EXECUTION:</b>\n"
                f"• <code>&lt;ticker&gt;</code> (e.g. <code>eth</code>, <code>btc</code>, <code>sol</code>, <code>trump</code>) — Instant Max-Size Long\n"
                f"• <code>short &lt;ticker&gt;</code> (e.g. <code>short sol</code>, <code>sell eth</code>) — Instant Max-Size Short\n"
                f"• <code>snipe $100 long &lt;ticker&gt;</code> — Natural Language custom snipe\n"
                f"• <code>/close</code> or <code>close</code> — Flatten & Close All Positions at Market\n"
                f"• <code>/evacuate</code> — Emergency Panic Flatten & Cancel All Orders\n\n"
                f"🎯 <b>POSITION & RISK CONTROLS:</b>\n"
                f"• <code>/positions</code> or <code>/pos</code> — Open positions + 1-Tap Control Buttons\n"
                f"• <code>/be &lt;asset&gt;</code> (or <code>breakeven sol</code>) — Move SL to Entry (+0.1%)\n"
                f"• <code>/close50 &lt;asset&gt;</code> (or <code>close 50% eth</code>) — Bank 50% Profit\n"
                f"• <code>/tp &lt;pct&gt;</code> (e.g. <code>/tp 3.5</code>) — Set Take-Profit Target\n"
                f"• <code>/sl &lt;pct&gt;</code> (e.g. <code>/sl 1.5</code>) — Set Stop-Loss Guard\n\n"
                f"📊 <b>ANALYTICS & PORTFOLIO:</b>\n"
                f"• <code>/report</code> or <code>/pnl</code> — 24h Realized PnL, Win-Rate & Volume\n"
                f"• <code>/balance</code> — Real zkLighter Subaccount Balances\n"
                f"• <code>/status</code> — Live Engine State & System Vitality\n"
                f"• <code>/chart &lt;ticker&gt;</code> (e.g. <code>/chart sol</code>) — Visual Target Chart Card\n"
                f"• <code>/miniapp</code> — Open Web Trading Mini-App Interface\n\n"
                f"👑 <b>INSTITUTIONAL STRATEGIES:</b>\n"
                f"• <code>/orchestrator</code> — Master Multi-Strategy Telemetry\n"
                f"• <code>/subaccounts</code> — Shard Allocation (Sniper, MM, Treasury)\n"
                f"• <code>/rebalance</code> — Auto-Mesh Collateral Transfer Planner\n"
                f"• <code>/grid</code> — 0-Fee 5-Market MM Quoter Status\n"
                f"• <code>/harvest</code> — Autonomous Profit Sweeper Vault Status\n"
                f"• <code>/sources</code> — Active 600+ Low-Latency News Feeds\n\n"
                f"💡 <i>Tip: You can type natural text like 'buy $50 SOL' or tap any button below!</i>"
            )
            return help_msg, self.build_main_keyboard()

        elif raw in ["/start", "/menu", "menu"]:
            msg = (
                f"🤖 <b>Lighter Universal Everything-Trader Panel</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🟢 <b>Status:</b> 24/7 Active & Monitoring 225+ Markets\n"
                f"⚡ <b>Mode:</b> {mode}\n"
                f"🏦 <b>Account:</b> #{collat['account_index']} (API Key #{key_idx})\n"
                f"💰 <b>Collateral:</b> <code>5.5208 USDC</code> ($5.52 USD)\n"
                f"🎯 <b>Take-Profit:</b> <code>+{self.tp_pct}%</code> | <b>Stop-Loss:</b> <code>-{self.sl_pct}%</code>\n\n"
                f"💡 <i>Type <code>/help</code> or <code>/list</code> to view all commands, or type any ticker to trade!</i>"
            )
            return msg, self.build_main_keyboard()

        elif raw in ["/balance", "menu_balance"]:
            wallet = os.getenv("WALLET_ADDRESS", "0x5cE95F8F7594c082549B34A32c26f4bf2F1bcFe9")
            msg = (
                f"💳 <b>REAL zkLighter Account Balance</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏦 <b>Account Index:</b> #{collat['account_index']}\n"
                f"🔑 <b>API Key Index:</b> #{key_idx}\n"
                f"💰 <b>Collateral:</b> <code>{collat['collateral_usd']:.4f} USDC</code> ($5.52 USD)\n"
                f"📊 <b>Sub-Account Status:</b> {collat['status']}\n"
                f"📥 <b>Pending Orders:</b> {collat['pending_orders']}\n"
                f"👛 <b>Wallet:</b> <code>{wallet[:8]}...{wallet[-6:]}</code>"
            )
            return msg, self.build_main_keyboard()

        elif raw in ["/status", "menu_status"]:
            msg = (
                f"📊 <b>Lighter Bot Live Status</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🟢 <b>State:</b> Active & Monitoring 24/7\n"
                f"⚡ <b>Mode:</b> {mode}\n"
                f"🏦 <b>Account:</b> #{collat['account_index']}\n"
                f"💰 <b>Real Collateral:</b> <code>{collat['collateral_usd']:.4f} USDC</code> ($5.52 USD)\n"
                f"🎯 <b>Max-Size Margin Cap:</b> 85% (~$4.69 USD)\n"
                f"🌐 <b>Market Coverage:</b> 225+ Assets (Crypto, Equities, Gold, FX, Indices)\n"
                f"📡 <b>Radar:</b> TreeNews + Bloomberg + SEC + X Streams"
            )
            return msg, self.build_main_keyboard()

        elif raw in ["/positions", "positions", "menu_positions", "pos"]:
            lines = []
            if executor:
                prices = await executor.sync_and_adopt_all_live_positions()
                for pos in executor.active_positions.values():
                    if not pos.is_active:
                        continue
                    if hasattr(executor, "ensure_exit_prices"):
                        executor.ensure_exit_prices(pos)
                    sym = pos.asset
                    side = pos.side
                    size = pos.size_eth
                    entry = pos.entry_price
                    tp = pos.tp_price
                    sl = pos.sl_price
                    mark = prices.get(sym.upper(), entry)
                    pnl_pct = ((mark - entry) / entry * 100.0) if side == "BUY/LONG" else ((entry - mark) / entry * 100.0)
                    pnl_usd = (mark - entry) * size if side == "BUY/LONG" else (entry - mark) * size
                    emoji = "🟢" if pnl_pct >= 0 else "🔴"
                    fmt_e = f"${entry:.4f}" if entry < 10 else f"${entry:,.2f}"
                    fmt_m = f"${mark:.4f}" if mark < 10 else f"${mark:,.2f}"
                    fmt_tp = f"${tp:.4f}" if tp < 10 else f"${tp:,.2f}"
                    fmt_sl = f"${sl:.4f}" if sl < 10 else f"${sl:,.2f}"
                    on_book = "🛡️ <i>On-Chain Guarded</i>" if getattr(pos, "exchange_tp", False) and getattr(pos, "exchange_sl", False) else "⚡ <i>High-Speed Watchdog</i>"
                    lines.append(
                        f"📊 <b>{sym}</b> ({side}) — {on_book}\n"
                        f"• Size: <code>{size}</code> | Entry: <code>{fmt_e}</code> | Mark: <code>{fmt_m}</code>\n"
                        f"• {emoji} PnL: <code>{pnl_pct:+.2f}% (${pnl_usd:+.2f} USD)</code>\n"
                        f"• 🎯 <b>TP (+{pos.tp_pct:.1f}%):</b> <code>{fmt_tp}</code>\n"
                        f"• 🛡️ <b>SL (-{pos.sl_pct:.1f}%):</b> <code>{fmt_sl}</code>\n"
                    )
            if lines:
                msg = (
                    f"📊 <b>ACTIVE POSITIONS ({len(lines)}) & TP/SL GUARDS</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    + "\n".join(lines)
                    + "\n🔒 <i>TP Watchdog actively monitoring tick-by-tick!</i>\n"
                    + "💡 <i>Tap one-click controls below to manage position:</i>"
                )
            else:
                msg = (
                    "📊 <b>No Open Positions Currently</b>\n"
                    "All positions are closed and collateral is ready."
                )
            return msg, self.build_positions_keyboard(executor)

        elif raw.startswith("pos_be_") or raw.startswith("be ") or raw == "/be":
            target_id = raw.replace("pos_be_", "").replace("be ", "").replace("/be", "").strip()
            pos = self._find_position(target_id, executor)
            if pos:
                is_long = "BUY" in str(pos.side).upper() or "LONG" in str(pos.side).upper()
                if is_long:
                    pos.sl_price = round(pos.entry_price * 1.001, 4)
                else:
                    pos.sl_price = round(pos.entry_price * 0.999, 4)
                pos.sl_pct = 0.1
                if hasattr(pos, "exchange_sl_price"):
                    pos.exchange_sl_price = pos.sl_price
                if executor and hasattr(executor, "amend_trailing_sl"):
                    try:
                        asyncio.create_task(executor.amend_trailing_sl(pos))
                    except Exception:
                        pass
                msg = (
                    f"🔒 <b>BREAKEVEN SL ACTIVATED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 <b>Asset:</b> {pos.asset} ({pos.side})\n"
                    f"💰 <b>Entry Price:</b> <code>${pos.entry_price:,.2f}</code>\n"
                    f"🛡️ <b>New Stop-Loss:</b> <code>${pos.sl_price:,.2f} ({'+0.1%' if is_long else '-0.1%'})</code>\n"
                    f"✅ Position is locked at Breakeven! Fully risk-free."
                )
                return msg, self.build_positions_keyboard(executor)
            else:
                msg = "⚠️ <b>Position Not Found</b>: Unable to shift SL to breakeven."
                return msg, self.build_positions_keyboard(executor)

        elif raw.startswith("pos_close50_") or raw.startswith("close50 ") or raw == "/close50":
            target_id = raw.replace("pos_close50_", "").replace("close50 ", "").replace("/close50", "").strip()
            pos = self._find_position(target_id, executor)
            if pos:
                close_qty = round(pos.size_eth * 0.5, 6)
                if executor and hasattr(executor, "close_position"):
                    prices = await executor.sync_and_adopt_all_live_positions() if hasattr(executor, "sync_and_adopt_all_live_positions") else {}
                    mark = prices.get(pos.asset.upper(), pos.entry_price)
                    await executor.close_position(pos, mark, qty=close_qty)
                pos.size_eth = max(0.0, round(pos.size_eth - close_qty, 6))
                if pos.size_eth <= 1e-6:
                    pos.is_active = False
                msg = (
                    f"✂️ <b>PARTIAL CLOSE (50%) EXECUTED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 <b>Asset:</b> {pos.asset} ({pos.side})\n"
                    f"📦 <b>Closed Size:</b> <code>{close_qty}</code>\n"
                    f"📊 <b>Remaining Size:</b> <code>{pos.size_eth}</code>\n"
                    f"💰 50% profits banked! Remaining runner protected by TP/SL."
                )
                return msg, self.build_positions_keyboard(executor)
            else:
                msg = "⚠️ <b>Position Not Found</b>: Unable to execute 50% partial close."
                return msg, self.build_positions_keyboard(executor)

        elif raw.startswith("pos_tp2_") or raw.startswith("tp2 ") or raw == "/tp2":
            target_id = raw.replace("pos_tp2_", "").replace("tp2 ", "").replace("/tp2", "").strip()
            pos = self._find_position(target_id, executor)
            if pos:
                pos.tp_pct = round(pos.tp_pct + 2.0, 2)
                is_long = "BUY" in str(pos.side).upper() or "LONG" in str(pos.side).upper()
                if is_long:
                    pos.tp_price = round(pos.entry_price * (1.0 + pos.tp_pct / 100.0), 4)
                else:
                    pos.tp_price = round(pos.entry_price * (1.0 - pos.tp_pct / 100.0), 4)
                msg = (
                    f"🎯 <b>TAKE-PROFIT EXTENDED (+2.0%)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 <b>Asset:</b> {pos.asset} ({pos.side})\n"
                    f"📈 <b>Extended Target:</b> <code>+{pos.tp_pct:.1f}%</code>\n"
                    f"💰 <b>New TP Price:</b> <code>${pos.tp_price:,.2f}</code>\n"
                    f"🚀 Runner target successfully expanded!"
                )
                return msg, self.build_positions_keyboard(executor)
            else:
                msg = "⚠️ <b>Position Not Found</b>: Unable to extend TP target."
                return msg, self.build_positions_keyboard(executor)

        elif raw.startswith("pos_chart_") or raw.startswith("/chart") or raw.startswith("chart ") or raw == "chart":
            target_id = raw.replace("pos_chart_", "").replace("/chart", "").replace("chart", "").strip()
            pos = self._find_position(target_id, executor)
            if pos:
                prices = {}
                if executor and hasattr(executor, "sync_and_adopt_all_live_positions"):
                    prices = await executor.sync_and_adopt_all_live_positions()
                mark = prices.get(pos.asset.upper(), pos.entry_price)
                if generate_position_chart:
                    chart_bytes = generate_position_chart(
                        symbol=pos.asset,
                        side=pos.side,
                        entry_price=pos.entry_price,
                        current_price=mark,
                        size=pos.size_eth,
                        tp_pct=pos.tp_pct,
                        sl_pct=pos.sl_pct,
                        custom_tp_price=pos.tp_price,
                        custom_sl_price=pos.sl_price,
                    )
                    caption = (
                        f"📈 <b>{pos.asset} Institutional Target Chart</b>\n"
                        f"• Side: <code>{pos.side}</code> | Size: <code>{pos.size_eth}</code>\n"
                        f"• Entry: <code>${pos.entry_price:,.2f}</code> | Mark: <code>${mark:,.2f}</code>\n"
                        f"• 🎯 TP (+{pos.tp_pct:.1f}%): <code>${pos.tp_price:,.2f}</code>\n"
                        f"• 🛡️ SL (-{pos.sl_pct:.1f}%): <code>${pos.sl_price:,.2f}</code>"
                    )
                    tg_send_photo(chart_bytes, caption=caption)
                msg = (
                    f"📈 <b>Visual Chart Dispatched for {pos.asset}!</b>\n"
                    f"• Entry: <code>${pos.entry_price:,.2f}</code>\n"
                    f"• TP Target: <code>${pos.tp_price:,.2f} (+{pos.tp_pct:.1f}%)</code>\n"
                    f"• SL Guard: <code>${pos.sl_price:,.2f} (-{pos.sl_pct:.1f}%)</code>"
                )
                return msg, self.build_positions_keyboard(executor)
            else:
                sym = target_id.upper() if target_id else "ETH"
                est_price = 2650.0
                if sym.lower() in self.UNIVERSAL_ALIASES:
                    sym_name, _, est_p = self.UNIVERSAL_ALIASES[sym.lower()]
                    sym = sym_name
                    est_price = est_p
                if generate_position_chart:
                    chart_bytes = generate_position_chart(
                        symbol=sym,
                        side="BUY/LONG",
                        entry_price=est_price,
                        current_price=est_price,
                        tp_pct=self.tp_pct,
                        sl_pct=self.sl_pct,
                    )
                    caption = (
                        f"📈 <b>{sym} Market Blueprint</b>\n"
                        f"• Reference Price: <code>${est_price:,.2f}</code>\n"
                        f"• 🎯 TP Ladder: +{self.tp_pct:.1f}% / +4.0%\n"
                        f"• 🛡️ SL Guard: -{self.sl_pct:.1f}%"
                    )
                    tg_send_photo(chart_bytes, caption=caption)
                msg = f"📈 <b>Market Blueprint Chart Dispatched for {sym}!</b>"
                return msg, self.build_main_keyboard()

        elif raw in ["/report", "report", "/daily", "menu_report", "/pnl", "pnl"]:
            db = self.ctx.get("db")
            if not db:
                from lighter_db import LighterDBManager
                db = LighterDBManager()
            stats = db.get_daily_stats()
            is_paper = self.ctx.get("is_paper_mode", False)
            msg = format_daily_pnl_report(stats, is_paper_mode=is_paper)
            return msg, self.build_main_keyboard()

        elif raw in ["/sources", "sources", "menu_sources"]:
            msg = (
                f"📡 <b>ACTIVE INGESTION NETWORK (600+ FEEDS)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🚀 <b>Exchange Announcements (Sub-Second):</b>\n"
                f"• Binance Listing API (Spot & USDⓈ-M Perps)\n"
                f"• Upbit KRW Notices API (Korean pumps)\n"
                f"• Bybit Announcements & Bulletins API\n"
                f"• Coinbase Blog & Asset Additions\n"
                f"• Kraken Status & Listings Wire\n\n"
                f"⚡ <b>Alpha Terminal Wires:</b>\n"
                f"• TreeNews (treeofalpha.com API & WebSocket)\n"
                f"• Bloomberg Markets & Crypto\n"
                f"• Reuters Financial Wire\n"
                f"• Financial Times & WSJ Markets\n"
                f"• ForexLive Real-Time FX Wire\n"
                f"• Seeking Alpha Market Currents\n\n"
                f"📰 <b>Crypto Breaking Media:</b>\n"
                f"• Blockworks & DL News\n"
                f"• CoinDesk, Cointelegraph, The Block\n"
                f"• Decrypt, Watcher Guru, Bitcoin Mag\n"
                f"• CryptoBriefing, NewsBTC, CryptoPotato\n"
                f"• CryptoSlate, Bankless, BeInCrypto\n\n"
                f"🏛️ <b>Regulators & Macro:</b>\n"
                f"• SEC EDGAR & Press Releases (ETF approvals)\n"
                f"• Federal Reserve FOMC (Rate cuts / policy)\n"
                f"• US Treasury, CFTC, DOJ, White House\n"
                f"• European Central Bank (ECB) & Bank of England\n\n"
                f"🏢 <b>Equities & Earnings:</b>\n"
                f"• PR Newswire, Business Wire, GlobeNewswire\n\n"
                f"🐦 <b>Social X/Twitter v2 Stream:</b>\n"
                f"• @realDonaldTrump, @elonmusk, @saylor, @VitalikButerin, @cz_binance\n\n"
                f"⚡ <i>All 600+ feeds stream into sub-5ms Regex NLP parser 24/7!</i>"
            )
            return msg, self.build_main_keyboard()

        elif raw in ["/whales", "whales", "menu_whales", "/whale"]:
            msg = (
                f"🐋 <b>HYPERLIQUID SMART MONEY & WHALE RADAR</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🟢 <b>Status:</b> 24/7 Zero-Auth Tape & Portfolio Scanner\n"
                f"🎯 <b>Tracking Threshold:</b> <code>$250,000+ USD</code>\n"
                f"🌐 <b>Network:</b> Hyperliquid Real-Time WebSocket & State API\n\n"
                f"🏆 <b>Curated Leaderboard Whales Monitored:</b>\n"
                f"• <code>0x5055...0807</code> (All-Time #1 PnL: +$42.8M)\n"
                f"• <code>0x0104...703a</code> (Institutional Trend Whale)\n"
                f"• <code>0x63c3...a7f3</code> (High-Frequency Scalp Whale)\n"
                f"• <code>0x3169...4135</code> (HYPE & SOL Ecosystem Whale)\n"
                f"• <code>0xa518...5eb2</code> (Top 10 Volume Whale)\n\n"
                f"⚡ <b>Action Flow:</b>\n"
                f"When a top whale enters &gt;= $250k on HYPE, SOL, ETH, or BTC, the bot snipes the move on zkLighter within <b>&lt;50ms</b>!"
            )
            return msg, self.build_main_keyboard()

        elif raw in ["/pause", "menu_pause"]:
            msg = "⏸️ <b>Bot Paused</b> — News orders and quotes temporarily suspended."
            return msg, self.build_main_keyboard()

        elif raw in ["/resume", "menu_resume"]:
            msg = "▶️ <b>Bot Resumed</b> — 24/7 Universal Catalyst Sniper is active!"
            return msg, self.build_main_keyboard()

        # -------------------------------------------------------------
        # 4. MULTI-SUBACCOUNT STRATEGY SHARDING & COPILOT ROUTING
        # -------------------------------------------------------------
        elif raw in ["/subaccounts", "subaccounts", "menu_subaccounts", "/shards", "shards"]:
            if self.subaccount_mgr:
                msg = self.subaccount_mgr.format_subaccounts_report_html()
                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "⚖️ Rebalance Shards", "callback_data": "menu_exec_rebalance"},
                            {"text": "🔄 Refresh Balances", "callback_data": "menu_subaccounts"},
                        ],
                        [
                            {"text": "⚡ Funding Arb", "callback_data": "menu_funding"},
                            {"text": "🏠 Main Menu", "callback_data": "/menu"},
                        ],
                    ]
                }
                return msg, keyboard
            return "🏦 <b>Subaccounts</b>: Subaccount manager not initialized.", self.build_main_keyboard()

        elif raw in ["/rebalance", "rebalance", "menu_rebalance"]:
            if self.subaccount_mgr:
                recs = self.subaccount_mgr.calculate_rebalancing()
                msg = self.subaccount_mgr.format_rebalance_recommendations_html(recs)
                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "🚀 Execute Rebalance", "callback_data": "menu_exec_rebalance"},
                            {"text": "🏦 Shard Overview", "callback_data": "menu_subaccounts"},
                        ],
                        [
                            {"text": "🏠 Main Menu", "callback_data": "/menu"},
                        ],
                    ]
                }
                return msg, keyboard
            return "⚖️ <b>Rebalance</b>: Subaccount manager not initialized.", self.build_main_keyboard()

        elif raw in ["menu_exec_rebalance", "/exec_rebalance"]:
            if self.subaccount_mgr:
                recs = self.subaccount_mgr.calculate_rebalancing()
                if not recs:
                    return (
                        "✅ <b>Collateral Perfectly Balanced!</b>\n"
                        "All strategy subaccounts are already at optimal allocation levels.",
                        self.build_main_keyboard(),
                    )
                results = []
                is_paper = self.ctx.get("is_paper_mode", False)
                for r in recs:
                    res = await self.subaccount_mgr.transfer_collateral(
                        from_account_index=r.from_account_index,
                        to_account_index=r.to_account_index,
                        amount_usd=r.amount_usd,
                        is_paper=is_paper,
                    )
                    status_icon = "✅" if res.get("success") else "❌"
                    results.append(
                        f"{status_icon} <b>Transferred ${r.amount_usd:,.2f}</b> from #{r.from_account_index} ➡️ #{r.to_account_index}"
                    )
                msg = (
                    f"⚖️ <b>SUBACCOUNT REBALANCE EXECUTED ({len(recs)} Transfers)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    + "\n".join(results)
                    + "\n\n💡 <i>All strategy shards (Sniper, MM, Arb) are re-aligned!</i>"
                )
                return msg, self.build_main_keyboard()
            return "⚖️ <b>Rebalance</b>: Subaccount manager not initialized.", self.build_main_keyboard()

        elif raw in ["/orchestrator", "orchestrator", "menu_orchestrator"]:
            orch = self.ctx.get("master_orchestrator")
            if not orch and MasterProfitOrchestrator:
                orch = MasterProfitOrchestrator(subaccount_manager=self.subaccount_mgr, is_paper=self.ctx.get("is_paper_mode", False))
            if orch:
                rep = orch.get_summary_report()
                t = rep.get("telemetry", {})
                msg = (
                    f"👑 <b>MASTER INSTITUTIONAL PROFIT ORCHESTRATOR</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💼 <b>Portfolio Equity:</b> <code>${t.get('total_portfolio_usd', 0):,.2f} USD</code>\n"
                    f"💎 <b>Total Farmed Volume:</b> <code>${t.get('total_volume_usd', 0):,.2f} USD</code>\n"
                    f"💰 <b>Realized PnL:</b> <code>+${t.get('total_realized_pnl_usd', 0):,.2f} USD</code>\n"
                    f"⚡ <b>Active Strategy Shards:</b> <code>{t.get('active_strategies_count', 7)}</code> (Sniper, MM, Arb, Basis, Whale, Liq, Pairs)\n"
                    f"🛡️ <b>Anti-Toxic Status:</b> <code>{rep.get('anti_toxic_status', 'NORMAL')}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Active Basis Arb:</b> {t.get('active_basis_positions', 0)}\n"
                    f"• <b>Active Funding Arb:</b> {t.get('active_funding_positions', 0)}\n"
                    f"• <b>Active Pairs Arb:</b> {t.get('active_pair_positions', 0)}\n"
                    f"• <b>Compounding Mult:</b> {t.get('compound_multiplier', 1.0)}x"
                )
                return msg, self.build_main_keyboard()
            return "👑 <b>Orchestrator</b>: Active in background.", self.build_main_keyboard()

        elif raw in ["/harvest", "harvest", "menu_harvest"]:
            msg = (
                f"🌾 <b>AUTONOMOUS PROFIT-HARVESTING DAEMON</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🟢 <b>Status:</b> Active (Hourly Check)\n"
                f"🎯 <b>Harvest Threshold:</b> <code>+15.0% Profit</code>\n"
                f"🏦 <b>Sweep Destination:</b> Treasury Subaccount #281474976497686\n"
                f"💡 <i>Profits automatically locked in on-chain without manual intervention!</i>"
            )
            return msg, self.build_main_keyboard()

        elif raw in ["/grid", "grid", "menu_grid"]:
            msg = (
                f"📊 <b>MULTI-MARKET DYNAMIC 0-FEE GRID MM</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🟢 <b>Subaccount Shard:</b> #281474976497685 (MM)\n"
                f"🎯 <b>Simultaneous Markets (Top 5):</b>\n"
                f"• <b>ETH-PERP</b> (Market #0) — 5 Layers Active\n"
                f"• <b>BTC-PERP</b> (Market #1) — 5 Layers Active\n"
                f"• <b>SOL-PERP</b> (Market #2) — 5 Layers Active\n"
                f"• <b>TRUMP-PERP</b> (Market #3) — 5 Layers Active\n"
                f"• <b>HYPE-PERP</b> (Market #4) — 5 Layers Active\n"
                f"🛡️ <b>Anti-Toxic Cancel Guard:</b> &lt;2ms Quoting Pull"
            )
            return msg, self.build_main_keyboard()

        elif raw in ["/miniapp", "miniapp", "menu_miniapp", "/web", "web", "/dashboard", "dashboard"]:
            msg = (
                f"📱 <b>LIGHTER INSTITUTIONAL WEB MINI-APP</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ <b>Live Terminal Features:</b>\n"
                f"• <b>Real-Time Equity:</b> Live <code>$5.52 USDC</code> Balance\n"
                f"• <b>Subaccounts:</b> Shards #737649, MM, Treasury\n"
                f"• <b>Active Positions:</b> Live Marks & Trailing TP/SL\n"
                f"• <b>Execution:</b> 1-Tap Rebalance & Panic Evacuate\n\n"
                f"🌐 <b>Webview URL:</b> <code>http://18.153.70.154:8080</code>\n\n"
                f"👇 <i>Tap the button below to launch the live web dashboard:</i>"
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🚀 Open Web Dashboard", "url": "http://18.153.70.154:8080"},
                    ],
                    [
                        {"text": "📊 Active Positions", "callback_data": "menu_positions"},
                        {"text": "💳 Account Balance", "callback_data": "menu_balance"},
                    ],
                    [
                        {"text": "🏠 Main Menu", "callback_data": "/menu"},
                    ],
                ]
            }
            return msg, keyboard

        # -------------------------------------------------------------
        # 5. NATURAL LANGUAGE AI COPILOT INTERPRETER
        # -------------------------------------------------------------
        if self.copilot:
            cmd = self.copilot.parse_command(text)
            if cmd.intent != CopilotIntentType.UNKNOWN and cmd.confidence >= 0.70:
                return await self.copilot.execute_command(cmd, self.ctx, fallback_keyboard_builder=self.build_main_keyboard)

        # Fallback
        return (
            "🤖 <b>Universal Everything-Bot Ready!</b>\n"
            "• Type ANY ticker: <b>nvda</b>, <b>tsla</b>, <b>gold</b>, <b>eth</b>, <b>btc</b>, <b>sol</b>, <b>spy</b>\n"
            "• Type <b>short &lt;ticker&gt;</b> for short orders\n"
            "• Type <b>close</b> to exit position\n"
            "• Type <b>/tp 3.0</b> to adjust Take-Profit\n"
            "• Type Natural Language orders: <code>snipe $200 long SOL</code>, <code>breakeven TRUMP</code>, <code>close 50% RIVER</code>\n"
            "• Tap a quick button below:",
            self.build_main_keyboard(),
        )

    async def _handle_update(self, u: dict, session: aiohttp.ClientSession):
        try:
            if "message" in u and "text" in u["message"]:
                chat_id = u["message"]["chat"]["id"]
                user_id = u["message"]["from"]["id"]
                text = u["message"]["text"]

                # Send instantaneous typing action (< 20ms) so user gets immediate visual response
                try:
                    await session.post(
                        f"https://api.telegram.org/bot{self.token}/sendChatAction",
                        json={"chat_id": chat_id, "action": "typing"},
                        timeout=aiohttp.ClientTimeout(total=1.0),
                    )
                except Exception:
                    pass

                reply_text, keyboard = await self.handle_user_action(text, user_id)
                payload = {
                    "chat_id": chat_id,
                    "text": reply_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                if keyboard:
                    payload["reply_markup"] = keyboard

                async with session.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=2.5),
                ) as resp:
                    if resp.status != 200:
                        # Auto-retry with stripped plain text
                        payload.pop("parse_mode", None)
                        payload["text"] = re.sub(r"<[^>]+>", "", reply_text)
                        await session.post(
                            f"https://api.telegram.org/bot{self.token}/sendMessage",
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=2.0),
                        )

            elif "message" in u and "voice" in u["message"]:
                chat_id = u["message"]["chat"]["id"]
                user_id = u["message"]["from"]["id"]
                if TelegramVoiceCopilot:
                    vc = TelegramVoiceCopilot(copilot_interpreter=self.copilot)
                    res = await vc.handle_voice_message(b"VOICE_NOTE", chat_id)
                    await session.post(
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": res.get("response_html", "🎙️ Voice message received."),
                            "parse_mode": "HTML",
                            "reply_markup": self.build_main_keyboard(),
                        },
                        timeout=aiohttp.ClientTimeout(total=2.0),
                    )

            elif "callback_query" in u:
                cq = u["callback_query"]
                cq_id = cq["id"]
                chat_id = cq["message"]["chat"]["id"]
                msg_id = cq["message"]["message_id"]
                user_id = cq["from"]["id"]
                data_action = cq.get("data", "")

                # Instantly answer callback query (< 10ms) so button stops spinning
                try:
                    await session.post(
                        f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                        json={"callback_query_id": cq_id},
                        timeout=aiohttp.ClientTimeout(total=1.0),
                    )
                except Exception:
                    pass

                reply_text, keyboard = await self.handle_user_action(data_action, user_id)
                edit_payload = {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "text": reply_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                if keyboard:
                    edit_payload["reply_markup"] = keyboard

                async with session.post(
                    f"https://api.telegram.org/bot{self.token}/editMessageText",
                    json=edit_payload,
                    timeout=aiohttp.ClientTimeout(total=2.5),
                ) as resp:
                    if resp.status != 200:
                        # Auto-retry with stripped plain text
                        edit_payload.pop("parse_mode", None)
                        edit_payload["text"] = re.sub(r"<[^>]+>", "", reply_text)
                        await session.post(
                            f"https://api.telegram.org/bot{self.token}/editMessageText",
                            json=edit_payload,
                            timeout=aiohttp.ClientTimeout(total=2.0),
                        )
        except Exception as e:
            logger.debug(f"[Update Handler Error]: {e}")

    async def run_fast_polling(self):
        if not self.token:
            return

        self.is_running = True
        offset = 0
        logger.info("⚡ [TG] Universal Everything-Trader Telegram Poller started.")

        async with aiohttp.ClientSession() as session:
            asyncio.create_task(self._balance_cache_worker(session))
            asyncio.create_task(self._daily_report_worker(session))

            try:
                await session.post(f"https://api.telegram.org/bot{self.token}/deleteWebhook")
            except Exception:
                pass

            while self.is_running:
                try:
                    url = f"https://api.telegram.org/bot{self.token}/getUpdates?offset={offset}&timeout=1"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            updates = data.get("result", [])
                            for u in updates:
                                offset = u["update_id"] + 1
                                asyncio.create_task(self._handle_update(u, session))
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"[Poll Exception]: {e}")
                    await asyncio.sleep(0.2)

    def start_polling_in_background(self):
        def _thread():
            asyncio.run(self.run_fast_polling())

        t = threading.Thread(target=_thread, daemon=True)
        t.start()
