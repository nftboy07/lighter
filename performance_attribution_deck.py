#!/usr/bin/env python3
"""
Multi-Source Profit Attribution & Performance Deck (performance_attribution_deck.py)
====================================================================================
Decomposes daily portfolio PnL into individual strategy alpha sources:
- Maker Spread %
- 1st-News Catalyst %
- Latency Lead Arbitrage %
- Funding & Basis Yield %
- Liquidation Wick Rebounds %
Formats clean visual markdown cards for Telegram daily reporting.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ProfitAttribution")


@dataclass
class StrategyAttribution:
    """PnL attribution for a specific strategy module."""
    strategy_name: str
    realized_pnl_usd: float
    volume_traded_usd: float
    trade_count: int
    contribution_pct: float            # % of total portfolio profit


@dataclass
class PerformanceAttributionDeck:
    """Consolidated daily attribution summary."""
    total_realized_pnl_usd: float
    total_volume_traded_usd: float
    total_trades_count: int
    strategy_attributions: List[StrategyAttribution]
    top_performing_strategy: str
    timestamp: float = field(default_factory=time.time)


class PerformanceAttributionEngine:
    """
    Decomposes realized returns into discrete alpha streams.
    """

    def __init__(self):
        # Strategy -> list of (pnl_usd, volume_usd)
        self._records: Dict[str, List[Tuple[float, float]]] = {
            "Maker_Spread_Quoting": [],
            "News_Catalyst_Sniper": [],
            "Latency_Lead_Arbitrage": [],
            "Funding_Basis_Yield": [],
            "Liquidation_Wick_Snipes": [],
        }

    def record_trade_pnl(
        self,
        strategy_name: str,
        pnl_usd: float,
        volume_usd: float,
    ) -> None:
        """Records a realized trade outcome under its strategy channel."""
        strat = strategy_name
        if strat not in self._records:
            self._records[strat] = []
        self._records[strat].append((pnl_usd, volume_usd))

    def generate_attribution_deck(self) -> PerformanceAttributionDeck:
        """
        Generates full performance attribution breakdown.
        """
        attributions: List[StrategyAttribution] = []
        total_pnl = 0.0
        total_vol = 0.0
        total_trades = 0

        for strat, trades in self._records.items():
            strat_pnl = sum(t[0] for t in trades)
            strat_vol = sum(t[1] for t in trades)
            total_pnl += strat_pnl
            total_vol += strat_vol
            total_trades += len(trades)

            attributions.append(
                StrategyAttribution(
                    strategy_name=strat,
                    realized_pnl_usd=round(strat_pnl, 2),
                    volume_traded_usd=round(strat_vol, 2),
                    trade_count=len(trades),
                    contribution_pct=0.0,
                )
            )

        # Calculate % contribution
        for a in attributions:
            if total_pnl > 0:
                a.contribution_pct = round((a.realized_pnl_usd / total_pnl) * 100.0, 1)

        attributions.sort(key=lambda a: a.realized_pnl_usd, reverse=True)
        top_strat = attributions[0].strategy_name if attributions else "None"

        deck = PerformanceAttributionDeck(
            total_realized_pnl_usd=round(total_pnl, 2),
            total_volume_traded_usd=round(total_vol, 2),
            total_trades_count=total_trades,
            strategy_attributions=attributions,
            top_performing_strategy=top_strat,
        )

        return deck

    def format_telegram_deck(self, deck: PerformanceAttributionDeck) -> str:
        """Formats clean HTML performance report for Telegram."""
        lines = [
            "📊 <b>DAILY ALPHA ATTRIBUTION DECK</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"💰 <b>Total Net PnL:</b> <code>${deck.total_realized_pnl_usd:+,.2f}</code>",
            f"🔄 <b>Total Volume:</b> <code>${deck.total_volume_traded_usd:,.2f}</code>",
            f"🎯 <b>Total Trades:</b> <code>{deck.total_trades_count}</code>",
            f"🏆 <b>Top Alpha:</b> <code>{deck.top_performing_strategy}</code>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            "📈 <b>Attribution Breakdown:</b>",
        ]
        for a in deck.strategy_attributions:
            emoji = "🟢" if a.realized_pnl_usd >= 0 else "🔴"
            lines.append(
                f"{emoji} <b>{a.strategy_name.replace('_', ' ')}:</b> "
                f"<code>${a.realized_pnl_usd:+,.2f}</code> ({a.contribution_pct:.0f}%)"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("⚡ <i>zkLighter Institutional Execution</i>")
        return "\n".join(lines)
