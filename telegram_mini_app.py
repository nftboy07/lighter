#!/usr/bin/env python3
"""
Live Telegram Mini-App Web Dashboard Generator (telegram_mini_app.py)
====================================================================
Generates mobile-optimized HTML5/CSS3 dashboard for Telegram Mini-App Webview,
displaying live PnL, subaccount collateral allocation, and 1-tap command actions.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("TelegramMiniApp")


class TelegramMiniAppGenerator:
    """
    Renders clean, self-contained HTML5 dark-mode web application for Telegram webview.
    """

    @staticmethod
    def generate_html_dashboard(
        total_portfolio_usd: float = 0.0,
        subaccounts_data: Optional[List[Dict[str, Any]]] = None,
        active_positions: Optional[List[Dict[str, Any]]] = None,
        daily_volume_usd: float = 0.0,
        daily_pnl_usd: float = 0.0,
    ) -> str:
        """
        Builds standalone dark-mode HTML5 application string.
        """
        subs = subaccounts_data or []
        positions = active_positions or []

        pnl_class = "profit" if daily_pnl_usd >= 0 else "loss"
        pnl_sign = "+" if daily_pnl_usd >= 0 else ""

        # Subaccount rows
        sub_cards = ""
        for s in subs:
            name = s.get("name", "Shard")
            idx = s.get("account_index", 0)
            collat = s.get("collateral_usd", 0.0)
            util = s.get("margin_utilization_pct", 0.0)
            sub_cards += f"""
            <div class="card sub-card">
                <div class="card-header">
                    <span>{name}</span>
                    <span class="badge">#{idx}</span>
                </div>
                <div class="card-body">
                    <div class="metric-row">
                        <span class="label">Collateral</span>
                        <span class="value">${collat:,.2f}</span>
                    </div>
                    <div class="metric-row">
                        <span class="label">Margin Utilization</span>
                        <span class="value">{util:.1f}%</span>
                    </div>
                </div>
            </div>
            """

        # Position rows
        pos_cards = ""
        if not positions:
            pos_cards = "<div class='empty-state'>No open positions</div>"
        else:
            for p in positions:
                sym = p.get("asset", "ETH")
                side = p.get("side", "BUY")
                pnl = p.get("pnl_usd", 0.0)
                pnl_c = "profit" if pnl >= 0 else "loss"
                pos_cards += f"""
                <div class="card pos-card">
                    <div class="card-header">
                        <span class="pos-sym">{sym}</span>
                        <span class="pos-side {side.lower()}">{side}</span>
                    </div>
                    <div class="metric-row">
                        <span class="label">PnL</span>
                        <span class="value {pnl_c}">${pnl:+,.2f}</span>
                    </div>
                </div>
                """

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Lighter Bot Terminal</title>
    <style>
        :root {{
            --bg: #0d1117;
            --surface: #161b22;
            --border: #30363d;
            --text: #c9d1d9;
            --accent: #58a6ff;
            --green: #3fb950;
            --red: #f85149;
        }}
        body {{
            margin: 0;
            padding: 16px;
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            font-size: 14px;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }}
        .title {{
            font-size: 18px;
            font-weight: 700;
            color: #fff;
        }}
        .status-dot {{
            width: 8px;
            height: 8px;
            background: var(--green);
            border-radius: 50%;
            display: inline-block;
            margin-right: 6px;
        }}
        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 14px;
            margin-bottom: 12px;
        }}
        .hero-metric {{
            font-size: 28px;
            font-weight: 700;
            color: #fff;
            margin: 8px 0;
        }}
        .metric-row {{
            display: flex;
            justify-content: space-between;
            padding: 4px 0;
        }}
        .label {{ color: #8b949e; }}
        .value {{ font-weight: 600; }}
        .profit {{ color: var(--green); }}
        .loss {{ color: var(--red); }}
        .btn-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-top: 16px;
        }}
        button {{
            background: #21262d;
            border: 1px solid var(--border);
            color: #fff;
            padding: 10px;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }}
        button:hover {{ background: #30363d; }}
        .btn-danger {{ background: rgba(248, 81, 73, 0.15); border-color: var(--red); color: var(--red); }}
        .badge {{ background: #21262d; padding: 2px 6px; border-radius: 4px; font-size: 11px; }}
        .empty-state {{ text-align: center; color: #8b949e; padding: 12px; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="title"><span class="status-dot"></span>zkLighter Live Hub</div>
        <span class="badge">Institutional v3.5</span>
    </div>

    <div class="card">
        <span class="label">Total Portfolio Equity</span>
        <div class="hero-metric">${total_portfolio_usd:,.2f}</div>
        <div class="metric-row">
            <span class="label">24h Realized PnL</span>
            <span class="value {pnl_class}">{pnl_sign}${daily_pnl_usd:,.2f}</span>
        </div>
        <div class="metric-row">
            <span class="label">24h Farmed Volume</span>
            <span class="value">${daily_volume_usd:,.2f}</span>
        </div>
    </div>

    <div class="title" style="margin: 16px 0 8px 0; font-size: 15px;">Subaccount Shards</div>
    {sub_cards}

    <div class="title" style="margin: 16px 0 8px 0; font-size: 15px;">Active Positions</div>
    {pos_cards}

    <div class="btn-grid">
        <button onclick="Telegram.WebApp.sendData('/rebalance')">⚖️ Rebalance</button>
        <button onclick="Telegram.WebApp.sendData('/report')">📊 Report</button>
        <button class="btn-danger" style="grid-column: span 2;" onclick="Telegram.WebApp.sendData('/flatten')">🚨 Panic Flatten All</button>
    </div>
</body>
</html>"""
        return html
