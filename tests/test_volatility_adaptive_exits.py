#!/usr/bin/env python3
"""
Unit Test Suite for Dynamic ATR & Volatility-Adaptive TP/SL Engine (tests/test_volatility_adaptive_exits.py)
========================================================================================================
Comprehensive institutional testing of:
1. True Range (TR) calculations (gap up/down, standard bar, zero prev close).
2. 14-period ATR calculation via Wilder's RMA and SMA.
3. 1-hour rolling and Annualized Realized Volatility from 1-min candles.
4. ATR multiplier (current / baseline).
5. Dynamic TP Expansion (TP1 +3.5%..+5.0%, TP2 +7.0%..+12.0% on >= 2.0x violent catalysts).
6. Dynamic Trailing Stop Cushion (1.0% -> 2.0% on high-volatility spikes).
7. Dynamic Breakeven Acceleration (tightening to BE faster when volatility normalizes <= 1.1x).
8. Tick & candle ingestion into AssetVolatilityTracker and multi-asset VolatilityAdaptiveExitEngine.
9. Integration with trade_exits.py policy and ladder pricing.
10. Integration with lighter_news_sniper.py MaxSizeExecutionEngine and watchdog.
"""

from __future__ import annotations

import math
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from volatility_adaptive_exits import (
    Candle,
    Tick,
    VolatilityState,
    AdaptiveExitLevels,
    AssetVolatilityTracker,
    VolatilityAdaptiveExitEngine,
    compute_true_range,
    compute_true_range_pct,
    compute_atr,
    compute_atr_pct,
    compute_realized_volatility_1h,
    compute_annualized_realized_volatility,
    calculate_atr_multiplier,
    calculate_dynamic_tp_levels,
    calculate_dynamic_trailing_cushion,
    calculate_dynamic_be_threshold,
    compute_adaptive_exit_levels,
    get_volatility_engine,
)
from trade_exits import (
    ExitPolicy,
    policy_for,
    adaptive_policy_for,
    scale_tp_price,
    tp_sl_prices,
    trail_stop,
    breakeven_sl,
)
from lighter_news_sniper import (
    ActivePosition,
    MaxSizeExecutionEngine,
)


# =============================================================================
# 1. TRUE RANGE & ATR COMPUTATIONS
# =============================================================================

def test_compute_true_range_standard():
    # Normal bar without gap: High=105, Low=95, PrevClose=100 -> TR = 10
    assert compute_true_range(105.0, 95.0, 100.0) == 10.0
    assert compute_true_range_pct(105.0, 95.0, 100.0) == 10.0


def test_compute_true_range_gap_up():
    # Gap up: High=115, Low=108, PrevClose=100 -> TR = |115 - 100| = 15
    assert compute_true_range(115.0, 108.0, 100.0) == 15.0


def test_compute_true_range_gap_down():
    # Gap down: High=92, Low=85, PrevClose=100 -> TR = |85 - 100| = 15
    assert compute_true_range(92.0, 85.0, 100.0) == 15.0


def test_compute_true_range_no_prev_close():
    # First bar with no prev close
    assert compute_true_range(105.0, 95.0, None) == 10.0
    assert compute_true_range(105.0, 95.0, 0.0) == 10.0
    assert compute_true_range_pct(105.0, 95.0, 0.0) == 0.0


def test_compute_atr_empty_and_single_candle():
    assert compute_atr([]) == 0.0
    assert compute_atr_pct([]) == 0.0

    single = [Candle(timestamp=1000.0, open=100.0, high=105.0, low=95.0, close=102.0)]
    assert compute_atr(single) == 10.0
    assert compute_atr_pct(single) == pytest.approx(10.0 / 102.0 * 100.0, rel=1e-4)


def test_compute_atr_14_period_wilder_and_sma():
    # Create 20 synthetic candles with known ranges
    candles = []
    base = 100.0
    for i in range(25):
        c = Candle(
            timestamp=1000.0 + i * 60,
            open=base,
            high=base + 2.0,
            low=base - 2.0,
            close=base + 1.0,
        )
        candles.append(c)
        base += 1.0

    # Each bar has High-Low = 4.0, PrevClose was base-1, High-(base-1)=3.0, Low-(base-1)=-3.0 -> TR=4.0
    atr_wilder = compute_atr(candles, period=14, method="wilder")
    atr_sma = compute_atr(candles, period=14, method="sma")

    assert atr_wilder == pytest.approx(4.0, rel=1e-3)
    assert atr_sma == pytest.approx(4.0, rel=1e-3)


# =============================================================================
# 2. REALIZED VOLATILITY (1-HOUR & ANNUALIZED)
# =============================================================================

def test_compute_realized_volatility_constant_prices():
    # Flat prices -> RV = 0%
    flat = [Candle(timestamp=1000.0 + i * 60, open=100.0, high=100.0, low=100.0, close=100.0) for i in range(60)]
    assert compute_realized_volatility_1h(flat) == 0.0
    assert compute_annualized_realized_volatility(flat) == 0.0


def test_compute_realized_volatility_1h_and_annualized():
    # Synthetic trending / oscillating price series
    candles = []
    p = 100.0
    for i in range(60):
        # 1.0% oscillation
        p = p * 1.01 if i % 2 == 0 else p * 0.99
        candles.append(Candle(timestamp=1000.0 + i * 60, open=p, high=p * 1.002, low=p * 0.998, close=p))

    rv_1h = compute_realized_volatility_1h(candles)
    rv_ann = compute_annualized_realized_volatility(candles)

    # Log return std is ~0.01 (1%), 1h scaled is ~0.01 * sqrt(60) * 100% ~ 7.74%
    assert rv_1h > 0.0
    assert rv_1h == pytest.approx(math.sqrt(60) * 1.0, rel=0.15)
    # Annualized should be much larger (sqrt(525600) ~ 725)
    assert rv_ann > rv_1h
    assert rv_ann == pytest.approx(math.sqrt(525600) * 1.0, rel=0.15)


def test_realized_volatility_too_few_candles():
    assert compute_realized_volatility_1h([]) == 0.0
    assert compute_realized_volatility_1h([Candle(1, 100, 101, 99, 100)]) == 0.0
    assert compute_annualized_realized_volatility([]) == 0.0


# =============================================================================
# 3. ATR MULTIPLIER & DYNAMIC TP EXPANSION
# =============================================================================

def test_calculate_atr_multiplier():
    assert calculate_atr_multiplier(1.5, 0.75) == 2.0
    assert calculate_atr_multiplier(0.75, 0.75) == 1.0
    assert calculate_atr_multiplier(0.375, 0.75) == 0.5
    # Edge cases
    assert calculate_atr_multiplier(0.0, 0.75) == 1.0
    assert calculate_atr_multiplier(1.5, 0.0) == 1.0
    assert calculate_atr_multiplier(100.0, 1.0) == 10.0  # Capped at 10.0


def test_dynamic_tp_expansion_violent_catalyst():
    # ATR Multiplier >= 2.0x -> TP1 in [+3.5% .. +5.0%], TP2 in [+7.0% .. +12.0%]
    tp1_2x, tp2_2x = calculate_dynamic_tp_levels(base_tp1=2.0, base_tp2=4.0, atr_multiplier=2.0)
    assert tp1_2x == 3.5
    assert tp2_2x == 7.0

    tp1_2_5x, tp2_2_5x = calculate_dynamic_tp_levels(base_tp1=2.0, base_tp2=4.0, atr_multiplier=2.5)
    assert 3.5 < tp1_2_5x < 5.0
    assert 7.0 < tp2_2_5x < 12.0
    assert tp1_2_5x == 4.25
    assert tp2_2_5x == 9.5

    tp1_3x, tp2_3x = calculate_dynamic_tp_levels(base_tp1=2.0, base_tp2=4.0, atr_multiplier=3.0)
    assert tp1_3x == 5.0
    assert tp2_3x == 12.0

    tp1_max, tp2_max = calculate_dynamic_tp_levels(base_tp1=2.0, base_tp2=4.0, atr_multiplier=5.0)
    assert tp1_max == 5.0
    assert tp2_max == 12.0


def test_dynamic_tp_expansion_intermediate_and_baseline():
    # 1.5x multiplier -> intermediate expansion between 2.0% and 3.5%
    tp1_1_5x, tp2_1_5x = calculate_dynamic_tp_levels(base_tp1=2.0, base_tp2=4.0, atr_multiplier=1.5)
    assert tp1_1_5x == 2.75
    assert tp2_1_5x == 5.5

    # 1.0x or lower -> preserved baseline
    tp1_1x, tp2_1x = calculate_dynamic_tp_levels(base_tp1=2.0, base_tp2=4.0, atr_multiplier=1.0)
    assert tp1_1x == 2.0
    assert tp2_1x == 4.0

    tp1_low, tp2_low = calculate_dynamic_tp_levels(base_tp1=2.0, base_tp2=4.0, atr_multiplier=0.8)
    assert tp1_low == 2.0
    assert tp2_low == 4.0


# =============================================================================
# 4. DYNAMIC TRAILING STOP CUSHION & BREAKEVEN ACCELERATION
# =============================================================================

def test_dynamic_trailing_cushion():
    # Normal volatility (<= 1.0x) -> base 1.0% cushion
    assert calculate_dynamic_trailing_cushion(base_trail_gap=1.0, atr_multiplier=1.0) == 1.0
    assert calculate_dynamic_trailing_cushion(base_trail_gap=1.0, atr_multiplier=0.8) == 1.0

    # High volatility spike (>= 2.0x) -> expands to 2.0% (and up to 2.5% on extreme)
    assert calculate_dynamic_trailing_cushion(base_trail_gap=1.0, atr_multiplier=2.0) == 2.0
    cushion_2_5x = calculate_dynamic_trailing_cushion(base_trail_gap=1.0, atr_multiplier=2.5)
    assert cushion_2_5x > 2.0
    assert cushion_2_5x <= 2.5

    # Intermediate spike (1.5x)
    assert calculate_dynamic_trailing_cushion(base_trail_gap=1.0, atr_multiplier=1.5) == 1.5


def test_dynamic_be_threshold():
    # Volatility normalizes quickly (<= 1.1x) -> accelerated BE threshold (+0.75% .. +1.0%)
    assert calculate_dynamic_be_threshold(base_be_threshold=1.5, atr_multiplier=0.8) == 0.75
    assert calculate_dynamic_be_threshold(base_be_threshold=1.5, atr_multiplier=1.1) == 1.0
    assert 0.75 < calculate_dynamic_be_threshold(base_be_threshold=1.5, atr_multiplier=0.95) < 1.0

    # Normal or elevated volatility (> 1.1x) -> standard +1.5% threshold
    assert calculate_dynamic_be_threshold(base_be_threshold=1.5, atr_multiplier=1.5) == 1.5
    assert calculate_dynamic_be_threshold(base_be_threshold=1.5, atr_multiplier=2.0) == 1.5


def test_compute_adaptive_exit_levels_long_and_short():
    # Long trade with 2.0x violent catalyst expansion
    long_exits = compute_adaptive_exit_levels(
        symbol="ETH",
        side="BUY/LONG",
        entry_price=2000.0,
        current_atr_pct=1.5,
        baseline_atr_pct=0.75,
        base_tp1=2.0,
        base_tp2=4.0,
        base_sl=1.5,
        base_trail_gap=1.0,
    )
    assert long_exits.atr_multiplier == 2.0
    assert long_exits.is_expanded is True
    assert long_exits.tp1_pct == 3.5
    assert long_exits.tp2_pct == 7.0
    assert long_exits.trail_gap_pct == 2.0
    assert long_exits.tp1_price == pytest.approx(2000.0 * 1.035, rel=1e-5)
    assert long_exits.tp2_price == pytest.approx(2000.0 * 1.070, rel=1e-5)
    assert long_exits.sl_price == pytest.approx(2000.0 * 0.985, rel=1e-5)
    assert long_exits.be_price == pytest.approx(2000.0 * 1.001, rel=1e-5)

    # Short trade with normalized volatility (0.8x)
    short_exits = compute_adaptive_exit_levels(
        symbol="ETH",
        side="SELL/SHORT",
        entry_price=2000.0,
        current_atr_pct=0.6,
        baseline_atr_pct=0.75,
        base_tp1=2.0,
        base_tp2=4.0,
        base_sl=1.5,
        base_trail_gap=1.0,
    )
    assert short_exits.atr_multiplier == 0.8
    assert short_exits.is_expanded is False
    assert short_exits.is_be_accelerated is True
    assert short_exits.tp1_pct == 2.0
    assert short_exits.tp2_pct == 4.0
    assert short_exits.trail_gap_pct == 1.0
    assert short_exits.tp1_price == pytest.approx(2000.0 * 0.98, rel=1e-5)
    assert short_exits.tp2_price == pytest.approx(2000.0 * 0.96, rel=1e-5)
    assert short_exits.sl_price == pytest.approx(2000.0 * 1.015, rel=1e-5)
    assert short_exits.be_price == pytest.approx(2000.0 * 0.999, rel=1e-5)


# =============================================================================
# 5. ASSET VOLATILITY TRACKER & ADAPTIVE ENGINE
# =============================================================================

def test_asset_volatility_tracker_tick_aggregation():
    tracker = AssetVolatilityTracker("ETH", baseline_atr_pct=0.75)
    base_time = 1700000040.0  # clean minute aligned (1700000040 % 60 == 0)

    # Ingest ticks within same minute (minute 0)
    tracker.on_tick(2500.0, timestamp=base_time + 10)
    tracker.on_tick(2520.0, timestamp=base_time + 25)
    tracker.on_tick(2490.0, timestamp=base_time + 40)
    tracker.on_tick(2510.0, timestamp=base_time + 55)

    assert tracker.current_candle is not None
    assert tracker.current_candle.open == 2500.0
    assert tracker.current_candle.high == 2520.0
    assert tracker.current_candle.low == 2490.0
    assert tracker.current_candle.close == 2510.0
    assert len(tracker.candles) == 0

    # Ingest tick in minute 1 -> rolls over minute 0 candle
    tracker.on_tick(2515.0, timestamp=base_time + 65)
    assert len(tracker.candles) == 1
    assert tracker.candles[0].close == 2510.0
    assert tracker.current_candle.open == 2515.0


def test_volatility_adaptive_engine_state_and_catalyst_detection():
    engine = VolatilityAdaptiveExitEngine(default_baseline_atr_pct=0.75)
    base_time = 1700000000.0

    # Feed 20 candles with 2.0% high-low volatility (violent catalyst)
    p = 2500.0
    candles = []
    for i in range(20):
        c = Candle(
            timestamp=base_time + i * 60,
            open=p,
            high=p * 1.015,
            low=p * 0.985,
            close=p * 1.005,
        )
        candles.append(c)
        p = c.close

    engine.on_candles("ETH", candles)
    state = engine.get_state("ETH", current_price=p)

    # ATR pct should be ~3.0%, vs baseline 0.75% -> multiplier >= 2.0x
    assert state.current_atr_pct > 1.5
    assert state.atr_multiplier >= 2.0
    assert state.is_violent_catalyst is True
    assert state.is_normalized is False

    # Calculate exits with this violent catalyst state
    exits = engine.calculate_exits("ETH", "BUY/LONG", entry_price=2500.0)
    assert exits.is_expanded is True
    assert exits.tp1_pct >= 3.5
    assert exits.tp2_pct >= 7.0
    assert exits.trail_gap_pct >= 2.0


def test_volatility_adaptive_engine_breakeven_acceleration():
    engine = VolatilityAdaptiveExitEngine(default_baseline_atr_pct=0.75)
    base_time = 1700000000.0

    # Feed 20 calm candles (0.2% high-low -> ATR pct ~0.4%, multiplier ~0.53x <= 1.1x)
    p = 2500.0
    candles = []
    for i in range(20):
        c = Candle(
            timestamp=base_time + i * 60,
            open=p,
            high=p * 1.001,
            low=p * 0.999,
            close=p,
        )
        candles.append(c)

    engine.on_candles("SOL", candles)
    state = engine.get_state("SOL", current_price=2500.0)
    assert state.is_normalized is True
    assert state.atr_multiplier <= 1.1

    # At +0.85% PnL, normal BE (+1.5%) would NOT trigger, but accelerated BE (+0.75%) SHOULD trigger
    assert engine.should_accelerate_breakeven("SOL", "BUY/LONG", entry_price=2500.0, current_price=2500.0 * 1.0085) is True
    # Below +0.75% threshold (+0.5%) -> should not trigger
    assert engine.should_accelerate_breakeven("SOL", "BUY/LONG", entry_price=2500.0, current_price=2500.0 * 1.005) is False


# =============================================================================
# 6. INTEGRATION WITH TRADE_EXITS.PY
# =============================================================================

def test_trade_exits_policy_for_with_atr_multiplier():
    # Baseline crypto policy: tp=2.0%, sl=1.5%, trail_gap=1.0%
    base_policy = policy_for("ETH")
    assert base_policy.tp_pct == 2.0
    assert base_policy.trail_gap_pct == 1.0

    # Violent catalyst 2.0x multiplier
    expanded_policy = policy_for("ETH", atr_multiplier=2.0)
    assert expanded_policy.tp_pct == 3.5
    assert expanded_policy.trail_gap_pct == 2.0

    # Helper function adaptive_policy_for
    adap_policy = adaptive_policy_for("ETH", atr_multiplier=2.5)
    assert adap_policy.tp_pct == 4.25
    assert adap_policy.trail_gap_pct > 2.0


def test_trade_exits_scale_tp_price_with_atr_multiplier():
    entry = 100.0
    policy = policy_for("ETH")

    # Standard scale-out TP prices (TP1=2.0%, TP2=4.0%, TP3=6.0%)
    tp1_base = scale_tp_price("BUY/LONG", entry, policy, 1)
    tp2_base = scale_tp_price("BUY/LONG", entry, policy, 2)
    assert tp1_base == pytest.approx(102.0)
    assert tp2_base == pytest.approx(104.0)

    # Expanded 2.0x ATR scale-out TP prices (TP1=3.5%, TP2=7.0%, TP3=10.5%)
    tp1_exp = scale_tp_price("BUY/LONG", entry, policy, 1, atr_multiplier=2.0)
    tp2_exp = scale_tp_price("BUY/LONG", entry, policy, 2, atr_multiplier=2.0)
    tp3_exp = scale_tp_price("BUY/LONG", entry, policy, 3, atr_multiplier=2.0)
    assert tp1_exp == pytest.approx(103.5)
    assert tp2_exp == pytest.approx(107.0)
    assert tp3_exp == pytest.approx(110.5)


def test_trade_exits_trail_stop_with_expanded_cushion():
    entry = 100.0
    # Create policy with 2.0% expanded cushion
    policy = policy_for("ETH", atr_multiplier=2.0)
    assert policy.trail_gap_pct == 2.0

    # High reached 105.0 (+5.0% > trail_arm 2.625%)
    # Trailed SL = 105.0 * (1 - 0.02) = 102.9
    current_sl = 98.5
    new_sl = trail_stop("BUY/LONG", entry=entry, high=105.0, low=100.0, current_sl=current_sl, policy=policy)
    assert new_sl == pytest.approx(102.9)


# =============================================================================
# 7. INTEGRATION WITH LIGHTER_NEWS_SNIPER.PY
# =============================================================================

@pytest.mark.asyncio
async def test_news_sniper_ensure_exit_prices_with_volatility_expansion():
    executor = MaxSizeExecutionEngine(is_live=False)
    # Feed high-volatility candles into executor's volatility engine
    base_time = 1700000000.0
    candles = [
        Candle(base_time + i * 60, 2000.0, 2040.0, 1960.0, 2010.0)
        for i in range(20)
    ]
    executor.volatility_engine.on_candles("ETH", candles)

    pos = ActivePosition(
        position_id="vol_pos_1",
        asset="ETH",
        market_index=0,
        side="BUY/LONG",
        entry_price=2000.0,
        size_eth=1.0,
        notional_usd=2000.0,
        tp_pct=0.0,
        sl_pct=0.0,
    )

    executor.ensure_exit_prices(pos)

    # Position should have expanded TP1 (+3.5%..+5.0%) and expanded cushion (2.0%)
    assert pos.tp_pct >= 3.5
    assert pos.tp_price >= 2000.0 * 1.035
    assert pos.trail_gap_pct >= 2.0
    assert pos.atr_multiplier >= 2.0


@pytest.mark.asyncio
async def test_news_sniper_watchdog_accelerates_breakeven_on_normalization():
    executor = MaxSizeExecutionEngine(is_live=False)
    executor.volatility_engine = VolatilityAdaptiveExitEngine()
    # Feed calm / normalized candles
    base_time = 1700000040.0
    candles = [
        Candle(base_time + i * 60, 2000.0, 2002.0, 1998.0, 2000.0)
        for i in range(20)
    ]
    executor.volatility_engine.on_candles("ETH", candles)

    pos = ActivePosition(
        position_id="vol_pos_2",
        asset="ETH",
        market_index=0,
        side="BUY/LONG",
        entry_price=2000.0,
        size_eth=1.0,
        notional_usd=2000.0,
        tp_pct=2.0,
        sl_pct=1.5,
        tp_price=2040.0,
        sl_price=1970.0,
        is_active=True,
    )
    executor.active_positions[pos.position_id] = pos

    # Mark price at +0.85% ($2017.0) -> below TP1 ($2040.0), but triggers accelerated BE (+0.1% = $2002.0)
    prices = {"ETH": 2017.0}
    events = await executor.check_take_profit_and_stop_loss(prices)

    assert pos.sl_price == pytest.approx(breakeven_sl("BUY/LONG", 2000.0, 0.1))
    assert pos.pending_sl_amend is True
    # Position stays open (not exited yet)
    assert len(events) == 0


@pytest.mark.asyncio
async def test_news_sniper_scale_out_with_expanded_tp():
    executor = MaxSizeExecutionEngine(is_live=False)
    pos = ActivePosition(
        position_id="vol_pos_3",
        asset="ETH",
        market_index=0,
        side="BUY/LONG",
        entry_price=2000.0,
        size_eth=2.0,
        notional_usd=4000.0,
        tp_pct=3.5,
        sl_pct=1.5,
        atr_multiplier=2.0,
        is_active=True,
    )
    executor.active_positions[pos.position_id] = pos
    executor.ensure_exit_prices(pos)

    # Mark hits expanded TP1 (+3.5% = $2070.0)
    prices = {"ETH": 2075.0}
    events = await executor.check_take_profit_and_stop_loss(prices)

    assert len(events) >= 1
    assert events[0]["type"] == "PARTIAL_TP_1"
    assert events[0]["tp_level"] == 1
    # 50% closed -> 1.0 ETH
    assert events[0]["close_qty"] == 1.0
    # Stop-Loss shifted to Breakeven +0.1% ($2002.0)
    assert pos.sl_price == pytest.approx(breakeven_sl("BUY/LONG", 2000.0, 0.1))
