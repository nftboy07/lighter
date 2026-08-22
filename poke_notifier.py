#!/usr/bin/env python3
"""
Poke AI Integration Client (poke_notifier.py)
==============================================
Asynchronous, non-blocking client for Poke.com (Poke AI).
Dispatches high-priority trading executions, take-profit fills,
stop-loss triggers, and market catalysts to Poke AI in <0.01ms (fire-and-forget).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("PokeAI")

POKE_API_URL = os.getenv("POKE_API_URL", "https://poke.com/api/v1/inbound/api-message")
POKE_API_KEY = os.getenv(
    "POKE_API_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJiYTI1NWE3MS1hM2Q1LTQ3YWMtOTFmNi05YjkzZjMwN2JlYjAiLCJqdGkiOiJkYjRkNTliMS00ZWE4LTQ0MjQtYTViYi1mMWFiMTZhODNjNWIiLCJpYXQiOjE3ODczNzk3MDIsImV4cCI6MjEwMjczOTcwMn0.BKasiODc-jsUjSWpC9iiJLtkGu856dqLqj_gklrHbic"
)

# Bounded queue for non-blocking execution
_POKE_QUEUE: queue.Queue = queue.Queue(maxsize=500)
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def strip_html_tags(text: str) -> str:
    """Strips HTML formatting tags for clean plain-text delivery to Poke AI."""
    clean = re.sub(r"<[^>]+>", "", text)
    clean = clean.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return clean.strip()


class PokeNotifierWorker(threading.Thread):
    """Background daemon worker delivering notifications to Poke AI without blocking trading."""

    def __init__(self, api_key: str, api_url: str):
        super().__init__(daemon=True, name="PokeNotifierWorker")
        self.api_key = api_key
        self.api_url = api_url
        self.is_running = True

    def run(self):
        while self.is_running:
            try:
                msg = _POKE_QUEUE.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                self._dispatch(msg)
            except Exception as e:
                logger.debug("Poke AI dispatch exception: %s", e)
            finally:
                _POKE_QUEUE.task_done()

    def _dispatch(self, message: str):
        if not self.api_key:
            return
        clean_text = strip_html_tags(message)
        payload = json.dumps({"message": clean_text}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "LighterTradingBot/PokeAI-v1",
        }
        req = urllib.request.Request(self.api_url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            if resp.status == 200:
                logger.debug("Poke AI message delivered successfully")
            else:
                logger.warning("Poke AI returned HTTP %s", resp.status)


def _ensure_worker_started():
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        with _WORKER_LOCK:
            if not _WORKER_STARTED:
                key = os.getenv("POKE_API_KEY", POKE_API_KEY).strip()
                url = os.getenv("POKE_API_URL", POKE_API_URL).strip()
                worker = PokeNotifierWorker(api_key=key, api_url=url)
                worker.start()
                _WORKER_STARTED = True
                logger.info("⚡ Poke AI background notification worker started")


def poke_send(message: str) -> bool:
    """
    Sub-0.01ms non-blocking notification dispatch to Poke AI.
    Never blocks trading threads or asyncio event loops.
    """
    if not message:
        return False
    _ensure_worker_started()
    try:
        _POKE_QUEUE.put_nowait(message)
        return True
    except queue.Full:
        logger.warning("Poke AI notification queue full; dropping message")
        return False


def poke_send_trade_alert(
    asset: str,
    side: str,
    size: float,
    price: float,
    tp_price: float,
    sl_price: float,
    reason: str = "CATALYST_SNIPE",
    notional_usd: float = 5.52,
) -> bool:
    """Dispatches formatted institutional trade execution alert to Poke AI."""
    msg = (
        f"🚨 LIGHTER TRADE EXECUTED\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Asset: {asset} ({side})\n"
        f"💵 Size: {size} (~${notional_usd:,.2f} USD)\n"
        f"⚡ Entry Price: ${price:,.2f}\n"
        f"🎯 Target (TP): ${tp_price:,.2f}\n"
        f"🛡️ Stop-Loss (SL): ${sl_price:,.2f}\n"
        f"📰 Trigger: {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    return poke_send(msg)


def poke_send_tp_sl_alert(
    asset: str,
    exit_type: str,
    pnl_usd: float,
    pnl_pct: float,
    exit_price: float,
) -> bool:
    """Dispatches Take-Profit or Stop-Loss exit event to Poke AI."""
    emoji = "🎯" if "TP" in exit_type.upper() or "PROFIT" in exit_type.upper() else "🛡️"
    msg = (
        f"{emoji} POSITION CLOSED ({exit_type.upper()})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Asset: {asset}\n"
        f"💰 Exit Price: ${exit_price:,.2f}\n"
        f"📈 Realized PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f} USD)\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    return poke_send(msg)
