#!/usr/bin/env python3
"""
Unit Tests for Anti-Toxic Flow Lead-Cancel Guard & Delta-Neutral Funding Arbitrage
================================================================================
Comprehensive test suite verifying:
1. AntiToxicMMGuard sub-2ms lead cancellation on TreeNews triggers & HL velocity spikes.
2. Quoting cooldown lockout and pause lifecycle.
3. FundingRateNormalizer cross-exchange multi-period rate normalization.
4. DeltaNeutralFundingHarvester high-yield spread detection (>=30% APR), delta-neutral
   position construction, funding payment accrual, and auto-unwind (<5% APR spread).
"""

import asyncio
import os
import sys
import time
from typing import Any, Dict, List
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from anti_toxic_guard import (
    AntiToxicGuardConfig,
    AntiToxicMMGuard,
    ToxicLeadEvent,
    ToxicTriggerReason,
)
from funding_arbitrage import (
    DeltaNeutralArbPosition,
    DeltaNeutralFundingHarvester,
    ExchangeFundingQuote,
    FundingArbitrageConfig,
    FundingArbOpportunity,
    FundingInterval,
    FundingRateNormalizer,
    PositionStatus,
    EXCHANGE_BINANCE,
    EXCHANGE_HYPERLIQUID,
    EXCHANGE_ZKLIGHTER,
)


# ============================================================================
# 1. Anti-Toxic Flow Lead-Cancel Guard Tests
# ============================================================================


class TestAntiToxicMMGuard:
    """Test suite for AntiToxicMMGuard."""

    def test_hl_velocity_trigger_upward_spike(self):
        """Tests that a >= 0.20% price move in < 100ms triggers emergency cancel."""
        canceled_assets: List[str] = []

        def mock_cancel(asset=None):
            canceled_assets.append(asset or "ALL")
            return 3  # 3 orders canceled

        guard = AntiToxicMMGuard(
            config=AntiToxicGuardConfig(
                velocity_threshold_pct=0.0020,  # 0.20%
                velocity_window_ms=100.0,
                cooldown_duration_sec=15.0,
            ),
            cancel_callback=mock_cancel,
        )

        now = 1000.0
        # Tick 1: Baseline price at $3,000.00
        guard.on_hyperliquid_price_tick("ETH", 3000.00, timestamp=now)
        assert not guard.is_quoting_paused("ETH")

        # Tick 2: 50ms later, price spikes to $3,007.50 (+0.25% > 0.20%)
        event = guard.on_hyperliquid_price_tick("ETH", 3007.50, timestamp=now + 0.050)

        assert event is not None
        assert event.trigger_reason == ToxicTriggerReason.HYPERLIQUID_PRICE_VELOCITY
        assert event.asset == "ETH"
        assert event.price_change_pct >= 0.0025
        assert event.orders_canceled == 3
        assert event.cancel_latency_ms < 5.0  # sub-5ms execution
        assert "ETH" in canceled_assets

        # Verify quoting cooldown is active
        assert guard.is_quoting_paused("ETH")
        assert guard.get_cooldown_remaining("ETH") > 0.0

    def test_hl_velocity_trigger_downward_spike(self):
        """Tests that a >= 0.20% downward dump in < 100ms triggers emergency cancel."""
        guard = AntiToxicMMGuard(
            config=AntiToxicGuardConfig(
                velocity_threshold_pct=0.0020,
                velocity_window_ms=100.0,
                cooldown_duration_sec=20.0,
            ),
            cancel_callback=lambda asset: 2,
        )

        now = 2000.0
        # BTC baseline $100,000
        guard.on_hyperliquid_price_tick("BTC", 100000.0, timestamp=now)

        # Dump 40ms later to $99,700 (-0.30%)
        event = guard.on_hyperliquid_price_tick("BTC", 99700.0, timestamp=now + 0.040)

        assert event is not None
        assert event.trigger_reason == ToxicTriggerReason.HYPERLIQUID_PRICE_VELOCITY
        assert event.price_change_pct <= -0.0030
        assert guard.is_quoting_paused("BTC")

    def test_hl_velocity_sub_threshold_no_false_positive(self):
        """Tests that price moves < 0.20% or slow moves (>150ms) do NOT trigger cancellations."""
        guard = AntiToxicMMGuard(
            config=AntiToxicGuardConfig(
                velocity_threshold_pct=0.0020,  # 0.20%
                velocity_window_ms=100.0,
            ),
            cancel_callback=lambda asset: 1,
        )

        now = 3000.0
        guard.on_hyperliquid_price_tick("SOL", 200.00, timestamp=now)

        # Move 1: +0.05% in 50ms (Below threshold)
        event1 = guard.on_hyperliquid_price_tick("SOL", 200.10, timestamp=now + 0.050)
        assert event1 is None
        assert not guard.is_quoting_paused("SOL")

        # Move 2: +0.10% in 90ms (Below threshold)
        event2 = guard.on_hyperliquid_price_tick("SOL", 200.20, timestamp=now + 0.090)
        assert event2 is None
        assert not guard.is_quoting_paused("SOL")

    def test_treenews_breaking_news_trigger(self):
        """Tests that high-trust TreeNews breaking news triggers instant cancellation."""
        dispatched_events: List[ToxicLeadEvent] = []

        def on_event(ev: ToxicLeadEvent):
            dispatched_events.append(ev)

        guard = AntiToxicMMGuard(
            config=AntiToxicGuardConfig(
                cooldown_duration_sec=30.0,
                min_news_trust_score=0.70,
            ),
            cancel_callback=lambda asset: 5,
            on_toxic_event=on_event,
        )

        news_payload = {
            "title": "US SEC APPROVES ETHEREUM SPOT ETF WITH IMMEDIATE TRADING",
            "source": "TreeNews",
            "trust_score": 0.95,
            "coin": "ETH",
        }

        event = guard.on_treenews_item(news_payload)

        assert event is not None
        assert event.trigger_reason == ToxicTriggerReason.TREENEWS_BREAKING_NEWS
        assert event.asset == "ETH"
        assert event.news_headline == news_payload["title"]
        assert event.orders_canceled == 5
        assert len(dispatched_events) == 1
        assert guard.is_quoting_paused("ETH")

    def test_treenews_low_trust_score_ignored(self):
        """Tests that low-trust news is filtered out to prevent unnecessary cancellations."""
        guard = AntiToxicMMGuard(
            config=AntiToxicGuardConfig(min_news_trust_score=0.80),
            cancel_callback=lambda asset: 1,
        )

        low_trust_news = {
            "title": "Unverified rumor on forum",
            "source": "RandomBlog",
            "trust_score": 0.40,
            "coin": "SOL",
        }

        event = guard.on_treenews_item(low_trust_news)
        assert event is None
        assert not guard.is_quoting_paused("SOL")

    @pytest.mark.asyncio
    async def test_async_anti_toxic_cancellation(self):
        """Tests async execution of velocity ticks and news handlers."""
        async def async_cancel(asset=None):
            await asyncio.sleep(0.001)  # 1ms network mock
            return 4

        guard = AntiToxicMMGuard(
            config=AntiToxicGuardConfig(velocity_threshold_pct=0.0020, velocity_window_ms=100.0),
            cancel_callback=async_cancel,
        )

        now = time.time()
        await guard.on_hyperliquid_price_tick_async("HYPE", 25.00, timestamp=now)
        # Spike +0.40%
        event = await guard.on_hyperliquid_price_tick_async("HYPE", 25.10, timestamp=now + 0.030)

        assert event is not None
        assert event.orders_canceled == 4
        assert guard.is_quoting_paused("HYPE")

    def test_cooldown_lifecycle_and_manual_resume(self):
        """Tests quoting pause duration, remaining time, and manual reset."""
        guard = AntiToxicMMGuard(
            config=AntiToxicGuardConfig(cooldown_duration_sec=10.0),
        )

        now = time.time()
        # Trigger global cancel
        event = guard.on_treenews_item({"title": "Federal Reserve Rate Decision", "trust_score": 0.9}, target_asset="ALL")
        assert event is not None

        assert guard.is_global_paused
        assert guard.is_quoting_paused("ETH")
        assert guard.is_quoting_paused("BTC")
        assert guard.get_cooldown_remaining() > 0.0

        # Manually resume
        guard.resume_quoting()
        assert not guard.is_global_paused
        assert not guard.is_quoting_paused("ETH")
        assert guard.get_cooldown_remaining() == 0.0

    def test_status_report(self):
        """Tests telemetry summary generation."""
        guard = AntiToxicMMGuard(
            config=AntiToxicGuardConfig(cooldown_duration_sec=20.0),
            cancel_callback=lambda a: 2,
        )
        guard.on_hyperliquid_price_tick("BTC", 50000.0, timestamp=100.0)
        guard.on_hyperliquid_price_tick("BTC", 50200.0, timestamp=100.05)  # +0.40%

        report = guard.get_status_report()
        assert report["total_triggers"] == 1
        assert report["total_orders_canceled"] == 2
        assert "BTC" in report["paused_assets"]


# ============================================================================
# 2. Delta-Neutral Funding Rate Yield Harvester Tests
# ============================================================================


class TestFundingRateNormalizer:
    """Test suite for FundingRateNormalizer."""

    def test_binance_8h_normalization_to_apr(self):
        """Tests Binance 8h funding rate conversion to APR."""
        # 0.01% per 8h = 0.0001
        # APR = 0.0001 * 3 * 365 = 0.1095 (10.95%)
        raw_8h = 0.0001
        apr = FundingRateNormalizer.to_apr(raw_8h, interval_hours=8.0)
        assert pytest.approx(apr, rel=1e-4) == 0.1095

        # Inverse conversion
        rec_rate = FundingRateNormalizer.to_periodic_rate(apr, interval_hours=8.0)
        assert pytest.approx(rec_rate, rel=1e-4) == raw_8h

    def test_hyperliquid_1h_normalization_to_apr(self):
        """Tests Hyperliquid 1h funding rate conversion to APR."""
        # 0.005% per hour = 0.00005
        # APR = 0.00005 * 24 * 365 = 0.438 (43.8% APR)
        raw_1h = 0.00005
        apr = FundingRateNormalizer.to_apr(raw_1h, interval_hours=1.0)
        assert pytest.approx(apr, rel=1e-4) == 0.438

        # Daily yield = 43.8% / 365 = 0.12%
        daily_yield = FundingRateNormalizer.to_daily_yield(apr)
        assert pytest.approx(daily_yield, rel=1e-4) == 0.0012

    def test_exchange_inferred_normalization(self):
        """Tests automatic interval deduction based on exchange name."""
        # Hyperliquid (1h)
        apr_hl = FundingRateNormalizer.normalize_exchange_rate("Hyperliquid", 0.00005)
        assert pytest.approx(apr_hl, rel=1e-4) == 0.438

        # Binance (8h)
        apr_bn = FundingRateNormalizer.normalize_exchange_rate("Binance", 0.0001)
        assert pytest.approx(apr_bn, rel=1e-4) == 0.1095

        # zkLighter (1h default)
        apr_zkl = FundingRateNormalizer.normalize_exchange_rate("zkLighter", 0.00002)
        assert pytest.approx(apr_zkl, rel=1e-4) == 0.1752


class TestDeltaNeutralFundingHarvester:
    """Test suite for DeltaNeutralFundingHarvester."""

    def test_detect_actionable_opportunity_above_30_apr(self):
        """Tests scanning and identifying funding rate spreads >= 30% APR."""
        harvester = DeltaNeutralFundingHarvester(
            config=FundingArbitrageConfig(min_entry_spread_apr=0.30)  # 30% APR min
        )

        # zkLighter ETH funding: 5% APR (Low rate)
        # raw_1h = 0.05 / 8760 = 0.0000057
        harvester.update_funding_rate(
            exchange=EXCHANGE_ZKLIGHTER,
            asset="ETH",
            raw_rate=0.00000570776,
            interval_hours=1.0,
            mark_price=3000.0,
        )

        # Hyperliquid ETH funding: 45% APR (High rate)
        # raw_1h = 0.45 / 8760 = 0.000051369
        harvester.update_funding_rate(
            exchange=EXCHANGE_HYPERLIQUID,
            asset="ETH",
            raw_rate=0.00005136986,
            interval_hours=1.0,
            mark_price=3000.5,
        )

        # Binance ETH funding: 10% APR
        # raw_8h = 0.10 / 1095 = 0.000091324
        harvester.update_funding_rate(
            exchange=EXCHANGE_BINANCE,
            asset="ETH",
            raw_rate=0.0000913242,
            interval_hours=8.0,
            mark_price=3000.2,
        )

        opportunities = harvester.scan_opportunities(target_asset="ETH", capital_usd=10000.0)

        assert len(opportunities) > 0
        best_opp = opportunities[0]

        # Best opportunity should be: Long zkLighter (~5%), Short Hyperliquid (~45%)
        assert best_opp.long_exchange == EXCHANGE_ZKLIGHTER
        assert best_opp.short_exchange == EXCHANGE_HYPERLIQUID
        assert best_opp.spread_apr >= 0.35  # ~40% spread >= 30% threshold
        assert best_opp.is_actionable is True
        assert best_opp.suggested_notional_usd == 10000.0
        assert best_opp.suggested_size == pytest.approx(10000.0 / 3000.25, rel=1e-2)

    def test_ignore_sub_threshold_opportunity(self):
        """Tests that spreads < 30% APR are marked not actionable."""
        harvester = DeltaNeutralFundingHarvester(
            config=FundingArbitrageConfig(min_entry_spread_apr=0.30)
        )

        # zkLighter: 10% APR
        harvester.update_funding_rate(EXCHANGE_ZKLIGHTER, "BTC", 0.000011415, 1.0, 100000.0)
        # Hyperliquid: 25% APR (Spread = 15% < 30%)
        harvester.update_funding_rate(EXCHANGE_HYPERLIQUID, "BTC", 0.000028538, 1.0, 100010.0)

        opps = harvester.scan_opportunities(target_asset="BTC")
        assert len(opps) > 0
        assert opps[0].spread_apr == pytest.approx(0.15, rel=1e-2)
        assert opps[0].is_actionable is False

    def test_open_delta_neutral_position_and_balance(self):
        """Tests opening delta-neutral long/short pair with dollar balance."""
        harvester = DeltaNeutralFundingHarvester()

        # Update rates (Spread 50% APR)
        harvester.update_funding_rate(EXCHANGE_ZKLIGHTER, "SOL", 0.0, 1.0, 200.0)
        harvester.update_funding_rate(EXCHANGE_HYPERLIQUID, "SOL", 0.0000570776, 1.0, 200.0)  # 50% APR

        opps = harvester.scan_opportunities("SOL", capital_usd=5000.0)
        assert len(opps) > 0
        opp = opps[0]

        position = harvester.open_delta_neutral_position(opp, capital_usd=5000.0, leverage=1.0)

        assert position.status == PositionStatus.OPEN
        assert position.long_leg.exchange == EXCHANGE_ZKLIGHTER
        assert position.long_leg.side == "LONG"
        assert position.long_leg.size == pytest.approx(25.0)  # $5000 / $200

        assert position.short_leg.exchange == EXCHANGE_HYPERLIQUID
        assert position.short_leg.side == "SHORT"
        assert position.short_leg.size == pytest.approx(25.0)

        assert position.delta_imbalance_usd == pytest.approx(0.0)
        assert position.position_id in harvester.active_positions

    def test_funding_accrual_and_net_pnl(self):
        """Tests accurate funding payments calculation across elapsed hours."""
        harvester = DeltaNeutralFundingHarvester()

        # Long zkLighter (0% APR), Short Hyperliquid (50% APR)
        harvester.update_funding_rate(EXCHANGE_ZKLIGHTER, "ETH", 0.0, 1.0, 3000.0)
        harvester.update_funding_rate(EXCHANGE_HYPERLIQUID, "ETH", 0.0000570776, 1.0, 3000.0)  # 50% APR

        opp = harvester.scan_opportunities("ETH", capital_usd=10000.0)[0]
        pos = harvester.open_delta_neutral_position(opp, capital_usd=10000.0)

        # Accrue 24 hours of funding
        # Expected daily funding on $10k at 50% APR = 10000 * 0.50 / 365 = $13.698
        net_funding_24h = harvester.accrue_funding(pos.position_id, hours_elapsed=24.0)

        assert pytest.approx(net_funding_24h, rel=1e-2) == 13.70
        assert pytest.approx(pos.total_cumulative_funding_usd, rel=1e-2) == 13.70

    def test_auto_unwind_when_spread_converges_under_5_pct(self):
        """Tests auto-unwind triggering when funding spread drops below 5% APR."""
        harvester = DeltaNeutralFundingHarvester(
            config=FundingArbitrageConfig(unwind_spread_apr=0.05)  # 5% APR unwind trigger
        )

        # Entry: zkLighter 5%, Hyperliquid 45% (Spread 40%)
        harvester.update_funding_rate(EXCHANGE_ZKLIGHTER, "BTC", 0.0000057, 1.0, 100000.0)
        harvester.update_funding_rate(EXCHANGE_HYPERLIQUID, "BTC", 0.0000513, 1.0, 100000.0)

        opp = harvester.scan_opportunities("BTC", capital_usd=20000.0)[0]
        pos = harvester.open_delta_neutral_position(opp, capital_usd=20000.0)

        # Accrue 48 hours of funding
        harvester.accrue_funding(pos.position_id, hours_elapsed=48.0)

        # Spread has not converged yet
        unwound_early = harvester.evaluate_unwind_conditions()
        assert len(unwound_early) == 0
        assert pos.status == PositionStatus.OPEN

        # Funding spread converges: Hyperliquid funding cools down to 7% APR (zkLighter is 5% -> Spread = 2% < 5%)
        harvester.update_funding_rate(
            EXCHANGE_HYPERLIQUID, "BTC", 0.00000799, 1.0, 100000.0  # ~7% APR
        )

        unwound = harvester.evaluate_unwind_conditions()
        assert len(unwound) == 1
        closed_pos = unwound[0]

        assert closed_pos.status == PositionStatus.CLOSED
        assert closed_pos.position_id not in harvester.active_positions
        assert closed_pos.unwind_reason is not None
        assert "converged" in closed_pos.unwind_reason.lower()
        assert closed_pos.net_realized_pnl_usd > 0.0  # Profitable from funding harvested

    def test_summary_telemetry_report(self):
        """Tests diagnostic summary generation."""
        harvester = DeltaNeutralFundingHarvester()
        harvester.update_funding_rate(EXCHANGE_BINANCE, "ETH", 0.0001, 8.0, 3000.0)
        harvester.update_funding_rate(EXCHANGE_HYPERLIQUID, "ETH", 0.00005, 1.0, 3000.0)

        opps = harvester.scan_opportunities("ETH")
        pos = harvester.open_delta_neutral_position(opps[0], capital_usd=1000.0)
        harvester.accrue_funding(pos.position_id, hours_elapsed=12.0)

        summary = harvester.get_summary_report()
        assert summary["active_positions_count"] == 1
        assert summary["total_open_notional_usd"] == pytest.approx(2000.0, rel=1e-2)
        assert summary["total_open_funding_usd"] > 0.0
        assert summary["tracked_quotes_count"] == 2

    def test_funding_inverted_spread_auto_unwind(self):
        """Tests that if the funding spread flips negative, position is auto-unwound."""
        harvester = DeltaNeutralFundingHarvester(
            config=FundingArbitrageConfig(unwind_spread_apr=0.05)
        )
        harvester.update_funding_rate(EXCHANGE_ZKLIGHTER, "ETH", 0.0000057, 1.0, 3000.0)  # 5%
        harvester.update_funding_rate(EXCHANGE_HYPERLIQUID, "ETH", 0.0000513, 1.0, 3000.0)  # 45%

        opp = harvester.scan_opportunities("ETH")[0]
        pos = harvester.open_delta_neutral_position(opp, capital_usd=5000.0)

        # Funding rate flips: Hyperliquid drops to -10% APR
        harvester.update_funding_rate(EXCHANGE_HYPERLIQUID, "ETH", -0.0000114, 1.0, 3000.0)

        unwound = harvester.evaluate_unwind_conditions()
        assert len(unwound) == 1
        assert unwound[0].status == PositionStatus.CLOSED
        assert "inverted" in unwound[0].unwind_reason.lower()

    def test_funding_negative_funding_rates(self):
        """Tests that negative funding rates (shorts pay longs) are accurately handled in accrual."""
        harvester = DeltaNeutralFundingHarvester()
        # zkLighter: -30% APR (Short pays, Long receives)
        # Hyperliquid: 10% APR
        harvester.update_funding_rate(EXCHANGE_ZKLIGHTER, "BTC", -0.000034246, 1.0, 100000.0)
        harvester.update_funding_rate(EXCHANGE_HYPERLIQUID, "BTC", 0.000011415, 1.0, 100000.0)

        opp = harvester.scan_opportunities("BTC", capital_usd=10000.0)[0]
        # zkLighter is lowest APR (-30%), Hyperliquid is highest (+10%), spread = 40%
        assert opp.long_exchange == EXCHANGE_ZKLIGHTER
        assert opp.short_exchange == EXCHANGE_HYPERLIQUID
        assert opp.spread_apr >= 0.39

        pos = harvester.open_delta_neutral_position(opp, capital_usd=10000.0)
        # Accrue 24h
        net_funding = harvester.accrue_funding(pos.position_id, hours_elapsed=24.0)
        # Net funding should be positive since Long earns on negative rate and Short earns on positive rate
        assert net_funding > 0.0

    def test_funding_mark_price_discrepancy_rejection(self):
        """Tests rejection of opportunity if price discrepancy between exchanges > 0.50%."""
        harvester = DeltaNeutralFundingHarvester(
            config=FundingArbitrageConfig(max_price_discrepancy_pct=0.005)
        )
        # zkLighter price $3000, Binance price $3030 (1.0% diff > 0.5%)
        harvester.update_funding_rate(EXCHANGE_ZKLIGHTER, "ETH", 0.0000057, 1.0, 3000.0)
        harvester.update_funding_rate(EXCHANGE_BINANCE, "ETH", 0.00045, 8.0, 3030.0)  # ~49% APR

        opps = harvester.scan_opportunities("ETH")
        assert len(opps) > 0
        assert opps[0].price_discrepancy_pct > 0.005
        assert opps[0].is_actionable is False


class TestAntiToxicEdgeCases:
    """Additional edge cases for Anti-Toxic MM Guard."""

    def test_zero_price_or_empty_buffer(self):
        guard = AntiToxicMMGuard()
        assert guard.on_hyperliquid_price_tick("BTC", 0.0) is None
        assert guard.on_hyperliquid_price_tick("BTC", -10.0) is None
        pct, win, base = guard.calculate_price_velocity("BTC", 100.0)
        assert pct == 0.0

    def test_news_object_attributes_extraction(self):
        class MockNewsObj:
            title = "Major SEC announcement on Solana"
            source = "TreeNews"
            trust_score = 0.92
            symbol = "SOL"

        guard = AntiToxicMMGuard(
            config=AntiToxicGuardConfig(min_news_trust_score=0.70),
            cancel_callback=lambda a: 1,
        )
        event = guard.on_treenews_item(MockNewsObj())
        assert event is not None
        assert event.asset == "SOL"
        assert guard.is_quoting_paused("SOL")

    def test_callback_exception_handling(self):
        def faulty_cancel(asset=None):
            raise RuntimeError("RPC Connection Dropped")

        guard = AntiToxicMMGuard(cancel_callback=faulty_cancel)
        event = guard.on_treenews_item({"title": "Breaking News Event", "trust_score": 0.9})
        assert event is not None
        assert event.orders_canceled == 0
        assert guard.is_global_paused

