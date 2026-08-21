#!/usr/bin/env python3
"""
Dynamic Visual Candlestick & Target Chart Generator (chart_generator.py)
========================================================================
Institutional dark-mode candlestick chart & price card generator for crypto
and multi-asset DEX trading bots.

Features:
- Renders clean dark-mode candlestick charts with OHLC data & volume bars.
- Dynamic entry price, Take-Profit ladder (TP1 +2.0%, TP2 +4.0%), and Stop-Loss (SL -1.5%).
- Auto-generates synthetic realistic candles leading up to entry & mark price
  if live candle history is not provided.
- Returns PNG bytes for direct Telegram sendPhoto dispatch, or saves to disk.
- Includes tg_send_photo helper for asynchronous & synchronous photo dispatch.
"""

from __future__ import annotations

import io
import math
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import requests

# Disable math text parser on dollar signs
matplotlib.rcParams["mathtext.default"] = "regular"


# Institutional Theme Colors
THEME = {
    "bg": "#0d1117",             # Dark canvas background
    "card_bg": "#161b22",        # Chart area background
    "grid": "#21262d",           # Subtle grid lines
    "text": "#c9d1d9",           # Off-white text
    "text_muted": "#8b949e",     # Dim gray text
    "text_highlight": "#f0f6fc", # Bright text
    "bull": "#00e676",           # Bright green for bullish candles
    "bear": "#ff5252",           # Bright red for bearish candles
    "bull_wick": "#00c853",
    "bear_wick": "#d50000",
    "entry": "#00b0ff",          # Cyan for entry price
    "tp1": "#00e676",            # Green for TP1 (+2.0%)
    "tp2": "#10b981",            # Emerald for TP2 (+4.0%)
    "sl": "#ff1744",             # Crimson for SL (-1.5%)
    "mark": "#ffeb3b",           # Gold / Yellow for current mark price
    "border": "#30363d",
}


def generate_synthetic_candles(
    entry_price: float,
    current_price: Optional[float] = None,
    side: str = "BUY/LONG",
    n_candles: int = 35,
    volatility: float = 0.006,
) -> List[Dict[str, float]]:
    """
    Generates realistic historical OHLC candles leading up to entry and current price.
    """
    if current_price is None or current_price <= 0:
        current_price = entry_price

    is_long = "BUY" in side.upper() or "LONG" in side.upper()
    candles: List[Dict[str, float]] = []

    # Start candles back with slight trend anchor
    base_start = entry_price * (0.988 if is_long else 1.012)
    p = base_start

    step = (entry_price - base_start) / max(1, n_candles - 6)
    random.seed(int(entry_price * 1000) % 99991)

    for i in range(n_candles):
        if i < n_candles - 6:
            drift = step + (random.gauss(0, entry_price * volatility * 0.3))
            o = p
            c = max(0.0001, o + drift)
        elif i < n_candles - 1:
            alpha = (i - (n_candles - 6)) / 5.0
            target = entry_price * (1 - alpha) + current_price * alpha
            o = p
            c = target + random.gauss(0, entry_price * volatility * 0.15)
        else:
            o = p
            c = current_price

        # Wicks
        spread = abs(c - o)
        noise = max(spread * 0.35, entry_price * volatility * 0.2)
        h = max(o, c) + abs(random.gauss(0, noise))
        l = min(o, c) - abs(random.gauss(0, noise))
        l = max(0.0001, l)

        v = random.uniform(10.0, 100.0) * (entry_price * 0.5)

        candles.append({
            "open": round(o, 4),
            "high": round(h, 4),
            "low": round(l, 4),
            "close": round(c, 4),
            "volume": round(v, 2),
        })
        p = c

    return candles


def calculate_target_levels(
    entry_price: float,
    side: str = "BUY/LONG",
    tp_pct: float = 2.0,
    tp2_pct: float = 4.0,
    sl_pct: float = 1.5,
) -> Dict[str, float]:
    """Calculates exact entry, TP ladder, and SL price levels."""
    is_long = "BUY" in side.upper() or "LONG" in side.upper()

    if is_long:
        tp1_price = entry_price * (1.0 + (tp_pct / 100.0))
        tp2_price = entry_price * (1.0 + (tp2_pct / 100.0))
        sl_price = entry_price * (1.0 - (sl_pct / 100.0))
    else:
        tp1_price = entry_price * (1.0 - (tp_pct / 100.0))
        tp2_price = entry_price * (1.0 - (tp2_pct / 100.0))
        sl_price = entry_price * (1.0 + (sl_pct / 100.0))

    return {
        "entry": entry_price,
        "tp1": tp1_price,
        "tp2": tp2_price,
        "sl": sl_price,
        "tp_pct": tp_pct,
        "tp2_pct": tp2_pct,
        "sl_pct": sl_pct,
    }


def _clean_str(text: str) -> str:
    """Escapes single dollar signs to prevent matplotlib mathtext syntax errors."""
    return text.replace("$", r"\$")


def generate_position_chart(
    symbol: str = "ETH",
    side: str = "BUY/LONG",
    entry_price: float = 2650.0,
    current_price: Optional[float] = None,
    size: Optional[float] = None,
    tp_pct: float = 2.0,
    tp2_pct: float = 4.0,
    sl_pct: float = 1.5,
    custom_tp_price: Optional[float] = None,
    custom_sl_price: Optional[float] = None,
    candles: Optional[List[Dict[str, float]]] = None,
    timeframe: str = "1M / 5M",
    width: int = 10,
    height: int = 6,
    dpi: int = 120,
) -> bytes:
    """
    Renders a dark-mode visual candlestick chart card with TP Ladder and SL lines.
    Returns PNG image bytes.
    """
    if current_price is None or current_price <= 0:
        current_price = entry_price

    is_long = "BUY" in side.upper() or "LONG" in side.upper()
    side_str = "LONG" if is_long else "SHORT"
    side_color = THEME["bull"] if is_long else THEME["bear"]
    side_badge = f"[{side_str}]"

    # Calculate Targets
    levels = calculate_target_levels(entry_price, side, tp_pct, tp2_pct, sl_pct)
    tp1_price = custom_tp_price if custom_tp_price is not None else levels["tp1"]
    tp2_price = levels["tp2"]
    sl_price = custom_sl_price if custom_sl_price is not None else levels["sl"]

    # Calculate PnL
    pnl_pct = ((current_price - entry_price) / entry_price * 100.0) if is_long else ((entry_price - current_price) / entry_price * 100.0)
    pnl_usd = (current_price - entry_price) * (size or 1.0) if is_long else (entry_price - current_price) * (size or 1.0)
    pnl_color = THEME["bull"] if pnl_pct >= 0 else THEME["bear"]
    pnl_sign = "+" if pnl_pct >= 0 else ""
    pnl_badge = f"{pnl_sign}{pnl_pct:.2f}%"

    # Candle Data
    if not candles:
        candles = generate_synthetic_candles(entry_price, current_price, side, n_candles=35)

    n = len(candles)

    # Matplotlib Figure & Dark Styling
    fig = plt.figure(figsize=(width, height), dpi=dpi, facecolor=THEME["bg"])
    
    # Subplots: Upper (Candles + Targets), Lower (Volume)
    gs = fig.add_gridspec(2, 1, height_ratios=[4, 1], hspace=0.08)
    ax_main = fig.add_subplot(gs[0, 0])
    ax_vol = fig.add_subplot(gs[1, 0], sharex=ax_main)

    ax_main.set_facecolor(THEME["card_bg"])
    ax_vol.set_facecolor(THEME["card_bg"])

    for spine in ax_main.spines.values():
        spine.set_color(THEME["border"])
        spine.set_linewidth(1.0)
    for spine in ax_vol.spines.values():
        spine.set_color(THEME["border"])
        spine.set_linewidth(1.0)

    ax_main.tick_params(colors=THEME["text_muted"], labelsize=9)
    ax_vol.tick_params(colors=THEME["text_muted"], labelsize=8)
    ax_main.grid(True, linestyle="--", alpha=0.25, color=THEME["grid"])
    ax_vol.grid(True, linestyle="--", alpha=0.15, color=THEME["grid"])
    plt.setp(ax_main.get_xticklabels(), visible=False)

    # Plot Candlesticks
    candle_width = 0.65
    wick_width = 1.2
    all_highs = []
    all_lows = []

    for i, c in enumerate(candles):
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        vol = c.get("volume", 0)
        all_highs.append(h)
        all_lows.append(l)

        is_bull = cl >= o
        body_color = THEME["bull"] if is_bull else THEME["bear"]
        wick_color = THEME["bull_wick"] if is_bull else THEME["bear_wick"]

        # Wick
        ax_main.plot([i, i], [l, h], color=wick_color, linewidth=wick_width, zorder=3)

        # Body
        body_bottom = min(o, cl)
        body_height = max(abs(cl - o), (h - l) * 0.02, 0.0001)
        rect = patches.Rectangle(
            (i - candle_width / 2, body_bottom),
            candle_width,
            body_height,
            linewidth=1,
            edgecolor=body_color,
            facecolor=body_color,
            zorder=4,
        )
        ax_main.add_patch(rect)

        # Volume Bar
        ax_vol.bar(i, vol, width=candle_width, color=body_color, alpha=0.6, zorder=3)

    # Calculate View Limits to fit Candles and Target Lines
    min_price = min(all_lows + [sl_price, tp1_price, tp2_price, entry_price, current_price])
    max_price = max(all_highs + [sl_price, tp1_price, tp2_price, entry_price, current_price])
    padding = (max_price - min_price) * 0.12
    ax_main.set_ylim(min_price - padding, max_price + padding)
    ax_main.set_xlim(-1, n + 5)  # extra space on right for price tags

    right_x = n + 0.2

    # Draw Entry Line
    ax_main.axhline(entry_price, color=THEME["entry"], linestyle="-.", linewidth=1.5, alpha=0.9, zorder=5)
    ax_main.text(
        right_x,
        entry_price,
        _clean_str(f" ENTRY ${entry_price:,.2f} "),
        color=THEME["bg"],
        fontsize=8.5,
        fontweight="bold",
        va="center",
        bbox=dict(boxstyle="square,pad=0.25", facecolor=THEME["entry"], edgecolor=THEME["entry"], alpha=0.95),
        zorder=6,
    )

    # Draw TP1 Ladder (+2.0%)
    ax_main.axhline(tp1_price, color=THEME["tp1"], linestyle="--", linewidth=1.5, alpha=0.9, zorder=5)
    ax_main.text(
        right_x,
        tp1_price,
        _clean_str(f" TP1 +{tp_pct:.1f}% (${tp1_price:,.2f}) "),
        color=THEME["bg"],
        fontsize=8.5,
        fontweight="bold",
        va="center",
        bbox=dict(boxstyle="square,pad=0.25", facecolor=THEME["tp1"], edgecolor=THEME["tp1"], alpha=0.95),
        zorder=6,
    )

    # Draw TP2 Ladder (+4.0%)
    ax_main.axhline(tp2_price, color=THEME["tp2"], linestyle=":", linewidth=1.5, alpha=0.9, zorder=5)
    ax_main.text(
        right_x,
        tp2_price,
        _clean_str(f" TP2 +{tp2_pct:.1f}% (${tp2_price:,.2f}) "),
        color=THEME["bg"],
        fontsize=8.5,
        fontweight="bold",
        va="center",
        bbox=dict(boxstyle="square,pad=0.25", facecolor=THEME["tp2"], edgecolor=THEME["tp2"], alpha=0.95),
        zorder=6,
    )

    # Draw SL Line (-1.5%)
    ax_main.axhline(sl_price, color=THEME["sl"], linestyle="--", linewidth=1.5, alpha=0.9, zorder=5)
    ax_main.text(
        right_x,
        sl_price,
        _clean_str(f" SL -{sl_pct:.1f}% (${sl_price:,.2f}) "),
        color=THEME["text_highlight"],
        fontsize=8.5,
        fontweight="bold",
        va="center",
        bbox=dict(boxstyle="square,pad=0.25", facecolor=THEME["sl"], edgecolor=THEME["sl"], alpha=0.95),
        zorder=6,
    )

    # Current Price Marker
    ax_main.plot(n - 1, current_price, "o", color=THEME["mark"], markersize=6, zorder=7)

    # Title Card / Header Overlay
    size_str = f" • Size: {size:g}" if size is not None else ""
    header_title = f"{symbol.upper()}/USD  {side_badge}{size_str}"
    pnl_summary = f"Mark: ${current_price:,.2f}  |  PnL: {pnl_badge}"
    if size:
        pnl_summary += f" ({pnl_sign}${pnl_usd:,.2f})"

    ax_main.set_title(
        _clean_str(f"{header_title}\n{pnl_summary}"),
        loc="left",
        color=THEME["text_highlight"],
        fontsize=11,
        fontweight="bold",
        pad=10,
    )

    # Timeframe and Watermark on Top Right
    timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    ax_main.text(
        0.98,
        1.08,
        _clean_str(f"TF: {timeframe} | {timestamp_str}"),
        transform=ax_main.transAxes,
        color=THEME["text_muted"],
        fontsize=8,
        ha="right",
        va="bottom",
    )

    # Format Volume Axis
    ax_vol.set_ylabel("Vol", color=THEME["text_muted"], fontsize=8)
    ax_vol.set_xlabel("Recent Execution Ticks (zkLighter OrderBook)", color=THEME["text_muted"], fontsize=8)

    # Convert Figure to PNG Bytes
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def generate_position_chart_file(
    output_path: str,
    symbol: str = "ETH",
    side: str = "BUY/LONG",
    entry_price: float = 2650.0,
    current_price: Optional[float] = None,
    size: Optional[float] = None,
    tp_pct: float = 2.0,
    tp2_pct: float = 4.0,
    sl_pct: float = 1.5,
    custom_tp_price: Optional[float] = None,
    custom_sl_price: Optional[float] = None,
    candles: Optional[List[Dict[str, float]]] = None,
) -> str:
    """Generates chart and writes PNG bytes directly to a file."""
    data = generate_position_chart(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        current_price=current_price,
        size=size,
        tp_pct=tp_pct,
        tp2_pct=tp2_pct,
        sl_pct=sl_pct,
        custom_tp_price=custom_tp_price,
        custom_sl_price=custom_sl_price,
        candles=candles,
    )
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(data)
    return output_path


def tg_send_photo(
    photo: Union[bytes, io.BytesIO, str],
    caption: str = "",
    chat_id: Optional[str] = None,
    token: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> bool:
    """
    Sends a photo directly to Telegram chat via sendPhoto API.
    Supports raw PNG bytes, BytesIO buffer, or local file path.
    """
    bot_token = token or os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TG_BOT_TOKEN") or ""
    dest_chat = chat_id or os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_USER_ID") or ""

    if not bot_token or not dest_chat or "YOUR_" in bot_token:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    data_payload: Dict[str, Any] = {
        "chat_id": dest_chat,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup:
        import json
        data_payload["reply_markup"] = json.dumps(reply_markup) if isinstance(reply_markup, dict) else reply_markup

    try:
        if isinstance(photo, (bytes, bytearray)):
            files = {"photo": ("chart.png", io.BytesIO(photo), "image/png")}
            resp = requests.post(url, data=data_payload, files=files, timeout=6.0)
        elif isinstance(photo, io.BytesIO):
            photo.seek(0)
            files = {"photo": ("chart.png", photo, "image/png")}
            resp = requests.post(url, data=data_payload, files=files, timeout=6.0)
        elif isinstance(photo, str) and os.path.exists(photo):
            with open(photo, "rb") as f:
                files = {"photo": ("chart.png", f, "image/png")}
                resp = requests.post(url, data=data_payload, files=files, timeout=6.0)
        else:
            return False

        return resp.status_code == 200
    except Exception:
        return False


if __name__ == "__main__":
    out_file = "sample_chart.png"
    generate_position_chart_file(
        out_file,
        symbol="NVDA",
        side="BUY/LONG",
        entry_price=128.50,
        current_price=131.20,
        size=35.0,
        tp_pct=2.0,
        tp2_pct=4.0,
        sl_pct=1.5,
    )
    print(f"✅ Generated sample position chart: {out_file}")
