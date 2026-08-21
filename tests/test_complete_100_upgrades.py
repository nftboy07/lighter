#!/usr/bin/env python3
"""
Unit & Integration Tests for Complete 100 Upgrades
==================================================
Tests:
- B20SaltPredictor CREATE2 address computation & Uniswap V3 pool prediction (#7)
- CrossPoolArbitrageDetector price spread and profit calculation (#6)
- DevAndWhalePatternDetector dev buy dump traps and whale flags (#12, #13)
- MultiQuoteAssetManager quote identification and pair validation (#15, #38)
- HomoglyphSpoofDetector zero-width character stripping and copycat detection (#33)
- TeamVestingAnalyzer supply lock calculation (#31)
- LiquidityDrainWatchdog event topic detection (#37)
- DirectPoolSwapExecutor calldata encoding and gas savings (#53)
- MemeCorrelationEngine duplicate trend detection (#67)
- DynamicAggressionController win-rate driven sizing (#74)
"""

import os
import sys
import pytest
from eth_utils import to_checksum_address

# Ensure root directory is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from complete_100_upgrades import (
    B20SaltPredictor,
    CrossPoolArbitrageDetector,
    DevAndWhalePatternDetector,
    MultiQuoteAssetManager,
    HomoglyphSpoofDetector,
    TeamVestingAnalyzer,
    LiquidityDrainWatchdog,
    DirectPoolSwapExecutor,
    MemeCorrelationEngine,
    DynamicAggressionController,
    WETH_BASE,
    USDC_BASE,
)


def test_create2_salt_predictor():
    """Test CREATE2 deterministic address calculation."""
    # Known deterministic test vectors
    factory = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
    token_a = WETH_BASE
    token_b = USDC_BASE
    fee = 3000

    predicted_pool = B20SaltPredictor.predict_pool_address(token_a, token_b, fee, factory=factory)
    assert predicted_pool.startswith("0x")
    assert len(predicted_pool) == 42
    # Invariant: predicting with swapped token order should yield identical pool address
    predicted_pool_reversed = B20SaltPredictor.predict_pool_address(token_b, token_a, fee, factory=factory)
    assert predicted_pool == predicted_pool_reversed


def test_cross_pool_arbitrage_detection():
    """Test Uniswap V3 vs Aerodrome price arbitrage engine."""
    detector = CrossPoolArbitrageDetector(min_profit_pct=1.5)

    # Opportunity: Aero price = 0.001 ETH, UniV3 price = 0.0012 ETH (20% spread)
    result = detector.check_arbitrage(
        token_address="0x1234567890123456789012345678901234567890",
        univ3_price_eth=0.0012,
        aerodrome_price_eth=0.0010,
        gas_cost_eth=0.0002,
    )

    assert result["has_opportunity"] is True
    assert result["buy_venue"] == "Aerodrome"
    assert result["sell_venue"] == "UniswapV3"
    assert result["spread_pct"] == 20.0
    assert result["estimated_net_profit_eth"] > 0

    # No opportunity: spread = 0.5%
    no_arb = detector.check_arbitrage(
        token_address="0x1234567890123456789012345678901234567890",
        univ3_price_eth=0.001005,
        aerodrome_price_eth=0.001000,
    )
    assert no_arb["has_opportunity"] is False


def test_dev_and_whale_pattern_detector():
    """Test dev buy dump trap detection and whale identification."""
    dev_wallet = "0xaaaa111122223333444455556666777788889999"
    detector = DevAndWhalePatternDetector(dev_wallet=dev_wallet)

    # Dev buying 60% of pool liquidity -> Red flag
    dev_analysis = detector.analyze_buyer(
        buyer_address=dev_wallet,
        buy_amount_eth=3.0,
        total_pool_liquidity_eth=5.0,
        is_dev_wallet=True,
    )
    assert dev_analysis["dev_red_flag"] is True
    assert dev_analysis["liquidity_fraction_pct"] == 60.0

    # Normal small buyer
    normal_analysis = detector.analyze_buyer(
        buyer_address="0xbbbb111122223333444455556666777788889999",
        buy_amount_eth=0.02,
        total_pool_liquidity_eth=5.0,
    )
    assert normal_analysis["dev_red_flag"] is False
    assert normal_analysis["is_whale"] is False

    # Large whale buy
    whale_analysis = detector.analyze_buyer(
        buyer_address="0xcccc111122223333444455556666777788889999",
        buy_amount_eth=1.0,
        total_pool_liquidity_eth=5.0,
    )
    assert whale_analysis["is_whale"] is True


def test_multi_quote_asset_manager():
    """Test identification of supported quote tokens and pairs on Base."""
    # Test WETH
    weth_info = MultiQuoteAssetManager.get_quote_asset_info(WETH_BASE)
    assert weth_info is not None
    assert weth_info["symbol"] == "WETH"
    assert weth_info["is_canonical_eth"] is True

    # Test USDC
    usdc_info = MultiQuoteAssetManager.get_quote_asset_info(USDC_BASE)
    assert usdc_info is not None
    assert usdc_info["symbol"] == "USDC"

    # Test pair validation
    meme_token = "0x9999999999999999999999999999999999999999"
    is_valid, meme_addr, quote_sym = MultiQuoteAssetManager.is_valid_pair(meme_token, WETH_BASE)
    assert is_valid is True
    assert meme_addr == to_checksum_address(meme_token)
    assert quote_sym == "WETH"


def test_homoglyph_and_spoof_detector():
    """Test unicode homoglyph sanitization and copycat detection."""
    # String containing zero-width space (\u200B)
    spoofed_ticker = "B\u200BRETT"
    assert HomoglyphSpoofDetector.sanitize_string(spoofed_ticker) == "BRETT"

    analysis = HomoglyphSpoofDetector.analyze_name_spoofing(
        token_name="Brett Official",
        token_symbol="B\u200BRETT",
    )
    assert analysis["is_suspicious"] is True
    assert analysis["has_hidden_chars"] is True
    assert analysis["is_impersonating"] is True
    assert analysis["target_brand"] == "BRETT"

    # Legitimate non-spoofed symbol
    clean_analysis = HomoglyphSpoofDetector.analyze_name_spoofing(
        token_name="Quantum Cat",
        token_symbol="QCAT",
    )
    assert clean_analysis["is_suspicious"] is False
    assert clean_analysis["has_hidden_chars"] is False


def test_team_vesting_analyzer():
    """Test team allocation and LP lock percentage analysis."""
    top_holders = [
        {"address": "0x000000000000000000000000000000000000dead", "percent": 55.0, "label": "Dead"},
        {"address": "0x1111111111111111111111111111111111111111", "percent": 10.0, "label": "UNCX Locker"},
        {"address": "0x2222222222222222222222222222222222222222", "percent": 35.0, "label": "User"},
    ]

    lock_info = TeamVestingAnalyzer.analyze_locks(top_holders)
    assert lock_info["locked_percentage"] == 65.0
    assert lock_info["is_well_locked"] is True


def test_liquidity_drain_watchdog():
    """Test detection of Uniswap V3 Burn and Collect event topics."""
    burn_topic = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"
    transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    assert LiquidityDrainWatchdog.is_liquidity_drain_event(burn_topic) is True
    assert LiquidityDrainWatchdog.is_liquidity_drain_event(transfer_topic) is False


def test_direct_pool_swap_calldata_encoding():
    """Test low-level direct pool swap calldata generation and gas savings."""
    recipient = "0x1111111111111111111111111111111111111111"
    calldata = DirectPoolSwapExecutor.encode_direct_swap_calldata(
        recipient=recipient,
        zero_for_one=True,
        amount_specified=1_000_000_000_000_000_000,
        sqrt_price_limit_x96=4295128739,
    )

    assert calldata.startswith(bytes.fromhex("128acb08"))
    assert len(calldata) > 4

    savings = DirectPoolSwapExecutor.estimate_direct_swap_gas_savings()
    assert savings["gas_saved"] > 0
    assert savings["savings_percent"] >= 15.0


def test_meme_correlation_engine():
    """Test duplicate and trend meme correlation detection."""
    engine = MemeCorrelationEngine()
    engine.record_launch("PEPE")
    engine.record_launch("PEPE2")

    # New launch is also a PEPE variant
    check = engine.check_correlation("PEPE3")
    assert check["is_correlated"] is True
    assert check["matching_recent_count"] >= 2

    # Unique unrelated launch
    check_unique = engine.check_correlation("SOLARIS")
    assert check_unique["is_correlated"] is False


def test_dynamic_aggression_controller():
    """Test dynamic scaling of position sizing and gas based on win rate."""
    controller = DynamicAggressionController(base_snipe_eth=0.03, base_gas_premium_gwei=1.0)

    # 1. Calibration stage (< 3 trades)
    calib = controller.get_dynamic_parameters(win_rate_pct=100.0, total_trades=2)
    assert calib["tier"] == "CALIBRATION"
    assert calib["snipe_amount_eth"] == 0.03

    # 2. Aggressive tier (Win Rate >= 65%)
    agg = controller.get_dynamic_parameters(win_rate_pct=75.0, total_trades=10)
    assert agg["tier"] == "AGGRESSIVE"
    assert agg["snipe_amount_eth"] == 0.039  # 0.03 * 1.3
    assert agg["gas_premium_gwei"] == 1.5

    # 3. Conservative tier (Win Rate < 45%)
    cons = controller.get_dynamic_parameters(win_rate_pct=30.0, total_trades=10)
    assert cons["tier"] == "CONSERVATIVE"
    assert cons["snipe_amount_eth"] == 0.018  # 0.03 * 0.6
    assert cons["gas_premium_gwei"] == 0.8
