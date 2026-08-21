#!/usr/bin/env python3
"""
Unit and Integration Tests for Master Institutional Profit Orchestrator (tests/test_master_profit_orchestrator.py)
=================================================================================================================
"""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from master_profit_orchestrator import (
    MasterProfitOrchestrator,
    OrchestratorTelemetry,
)
from subaccount_manager import (
    SubaccountRole,
    SubaccountManager,
)
from liquidation_hunter import (
    LiquidationSide,
)


def test_master_orchestrator_initialization():
    orchestrator = MasterProfitOrchestrator(is_paper=True)
    assert orchestrator.subaccount_manager is not None
    assert orchestrator.basis_engine is not None
    assert orchestrator.funding_engine is not None
    assert orchestrator.whale_engine is not None
    assert orchestrator.liquidation_engine is not None
    assert orchestrator.grid_engine is not None
    assert orchestrator.stat_arb_engine is not None


def test_master_orchestrator_shard_routing():
    orchestrator = MasterProfitOrchestrator(is_paper=True)
    
    # Verify Sniper routing -> #737649
    sniper_shard = orchestrator.route_trade_to_shard("news_catalyst")
    assert sniper_shard.account_index == 737649

    # Verify MM routing -> #281474976497685
    mm_shard = orchestrator.route_trade_to_shard("dynamic_grid_mm")
    assert mm_shard.account_index == 281474976497685

    # Verify Arb routing -> #281474976497686
    arb_shard = orchestrator.route_trade_to_shard("funding_harvester")
    assert arb_shard.account_index == 281474976497686


def test_master_orchestrator_arbitrage_evaluation():
    orchestrator = MasterProfitOrchestrator(is_paper=True)
    
    # Feed spot & perp prices with basis spread
    orchestrator.basis_engine.update_spot_book("ETH", bid=2000.0, ask=2000.20)
    orchestrator.basis_engine.update_perp_book("ETH", bid=2005.0, ask=2005.20)

    res = orchestrator.evaluate_all_arbitrage("ETH")
    assert res["symbol"] == "ETH"
    assert res["has_actionable_arb"] is True
    assert res["basis_opportunity"] is not None


def test_master_orchestrator_orderbook_processing():
    orchestrator = MasterProfitOrchestrator(is_paper=True)
    
    bids = [(2000.0, 50.0), (1995.0, 10.0)]
    asks = [(2005.0, 2.0)]
    
    res = orchestrator.process_orderbook_frame(
        symbol="ETH",
        bids=bids,
        asks=asks,
        mid_price=2002.50,
    )

    assert "grid_state" in res
    assert len(res["grid_state"].buy_levels) == 5
    assert len(res["grid_state"].sell_levels) == 5


def test_master_orchestrator_capital_and_sweeps():
    orchestrator = MasterProfitOrchestrator(is_paper=True)
    
    # Capital of $750 on $500 base (1.5x ratio) -> 1.25x multiplier and triggers $50 sweep
    mult, sweep = orchestrator.evaluate_capital_and_sweeps(750.0)
    assert mult == 1.25
    assert sweep is not None
    assert sweep.amount_usd == 50.0

    summary = orchestrator.get_summary_report()
    assert summary["telemetry"]["total_portfolio_usd"] == 750.0
    assert summary["telemetry"]["active_strategies_count"] == 7
