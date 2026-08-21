#!/usr/bin/env python3
"""
Unit and Integration Tests for Universal Ticker News Coverage (ticker_news_universe.py)
=====================================================================================
"""

from __future__ import annotations

import os
import sys
import re
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ticker_news_universe import (
    UniversalTickerNewsUniverse,
    TickerNewsCoverage,
    ALL_TRADED_TICKERS,
)


def test_universal_ticker_news_coverage_100_pct():
    universe = UniversalTickerNewsUniverse()
    report = universe.verify_all_tickers_coverage()

    assert report["total_tickers_monitored"] == len(ALL_TRADED_TICKERS)
    assert report["coverage_percentage"] == 100.0
    assert report["status"] == "ALL_TICKERS_FULLY_COVERED"
    assert report["min_sources_per_ticker"] >= 8  # Each ticker has >= 8 active sources


def test_individual_ticker_regex_matching():
    universe = UniversalTickerNewsUniverse()

    # Test Solana ETF Catalyst Match
    sol_cov = universe.get_coverage("SOL")
    assert sol_cov is not None
    pattern = re.compile(sol_cov.catalyst_regex_pattern)
    assert pattern.search("US SEC Formally Approves Solana Spot ETF Filings") is not None
    assert pattern.search("Solana foundation announced mainnet upgrade") is not None

    # Test Ethereum ETF & Fee Switch Match
    eth_cov = universe.get_coverage("ETH")
    assert eth_cov is not None
    pattern_eth = re.compile(eth_cov.catalyst_regex_pattern)
    assert pattern_eth.search("Ethereum core devs schedule major hardfork upgrade") is not None

    # Test Trump / World Liberty Fi Match
    trump_cov = universe.get_coverage("TRUMP")
    assert trump_cov is not None
    pattern_trump = re.compile(trump_cov.catalyst_regex_pattern)
    assert pattern_trump.search("Official Trump coin announced major binance listing") is not None
