#!/usr/bin/env python3
"""
Instant Telegram Visual Fill Cards with Technical Overlays (visual_fill_cards.py)
=================================================================================
Renders dark-mode visual price cards with Entry, TP Ladder (+2%, +4%), Stop-Loss,
and VWAP overlays, outputting PNG bytes for instant Telegram sendPhoto dispatch.
"""

from __future__ import annotations

import io
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("VisualFillCards")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as patches
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


class VisualFillCardRenderer:
    """
    Renders institutional dark-mode visual trade fill cards.
    """

    @staticmethod
    def render_trade_card(
        symbol: str,
        side: str,
        entry_price: float,
        tp1_price: float,
        tp2_price: float,
        sl_price: float,
        vwap_price: Optional[float] = None,
        notional_usd: float = 200.0,
        catalyst_headline: str = "",
    ) -> bytes:
        """
        Renders PNG bytes for Telegram photo dispatch.
        """
        if not HAS_MATPLOTLIB:
            return b""

        plt.style.use("dark_background")
        fig, ax = plt.subplots(figsize=(6.5, 3.8), dpi=120)
        fig.patch.set_facecolor("#0d1117")
        ax.set_facecolor("#161b22")

        is_long = "BUY" in side.upper() or "LONG" in side.upper()
        main_color = "#3fb950" if is_long else "#f85149"

        # Reference price lines
        ax.axhline(entry_price, color="#58a6ff", linestyle="--", linewidth=1.5, label=f"Entry: ${entry_price:,.2f}")
        ax.axhline(tp1_price, color="#3fb950", linestyle="-", linewidth=1.8, label=f"TP 1 (+2%): ${tp1_price:,.2f}")
        ax.axhline(tp2_price, color="#2ea043", linestyle="-", linewidth=1.8, label=f"TP 2 (+4%): ${tp2_price:,.2f}")
        ax.axhline(sl_price, color="#f85149", linestyle="-", linewidth=1.8, label=f"Stop-Loss: ${sl_price:,.2f}")

        if vwap_price and vwap_price > 0:
            ax.axhline(vwap_price, color="#d29922", linestyle=":", linewidth=1.2, label=f"VWAP: ${vwap_price:,.2f}")

        # Setup limits
        prices = [entry_price, tp1_price, tp2_price, sl_price]
        if vwap_price:
            prices.append(vwap_price)
        min_p, max_p = min(prices) * 0.995, max(prices) * 1.005
        ax.set_ylim(min_p, max_p)
        ax.set_xlim(0, 10)

        # Title & Badges
        title_text = f"⚡ {symbol} {side.upper()} FILLED (${notional_usd:,.0f})"
        ax.set_title(title_text, color="#ffffff", fontsize=13, fontweight="bold", pad=12)

        # Remove X axis ticks
        ax.set_xticks([])
        ax.set_ylabel("Price (USDC)", color="#8b949e", fontsize=10)
        ax.grid(color="#30363d", linestyle="--", linewidth=0.5, alpha=0.7)

        # Legend
        ax.legend(loc="upper left", facecolor="#21262d", edgecolor="#30363d", fontsize=9)

        # Footer subtitle
        if catalyst_headline:
            short_head = catalyst_headline[:60] + "..." if len(catalyst_headline) > 60 else catalyst_headline
            fig.text(0.08, 0.02, f"Catalyst: {short_head}", color="#8b949e", fontsize=8)

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
