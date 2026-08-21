#!/usr/bin/env python3
"""
Dynamic ATR & Volatility-Adaptive TP/SL Engine (volatility_adaptive_exits.py)
=============================================================================
Institutional volatility analytics & adaptive exit ladder for crypto & multi-asset DEX bots.

Key Capabilities:
1. True Range (TR), 14-period Wilder's & SMA ATR, and 1-hour / Annualized Realized Volatility (RV)
   computed from live price ticks and 1-minute OHLCV candles.
2. Dynamic ATR Multiplier = Current ATR / Baseline ATR.
3. Dynamic TP Expansion: If ATR Multiplier >= 2.0x (violent catalyst), expand TP1 (+3.5%..+5.0%)
   and TP2 (+7.0%..+12.0%).
4. Dynamic Trailing Stop Cushion: Expand trailing cushion from 1.0% to 2.0% on high-volatility spikes
   to prevent premature wick-outs.
5. Dynamic Breakeven Acceleration: Tighten SL to Breakeven (+0.1%) faster (+0.75%..+1.0% threshold)
   when volatility normalizes quickly (ATR Multiplier <= 1.1x).
6. Full integration with trade_exits.py and lighter_news_sniper.py.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class Candle:
    """Standard 1-minute OHLCV candlestick."""
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass
class Tick:
    """Live price tick."""
    timestamp: float
    price: float
    size: float = 0.0


@dataclass(frozen=True)
class VolatilityState:
    """Computed volatility metrics and regime classification for an asset."""
    asset: str
    current_price: float
    current_atr: float
    current_atr_pct: float
    baseline_atr: float
    baseline_atr_pct: float
    atr_multiplier: float
    realized_vol_1h_pct: float
    realized_vol_annual_pct: float
    is_violent_catalyst: bool
    is_normalized: bool
    sample_count: int
    updated_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class AdaptiveExitLevels:
    """Volatility-adapted Take-Profit ladder and Stop-Loss configuration."""
    tp1_pct: float
    tp2_pct: float
    tp3_pct: float
    sl_pct: float
    trail_arm_pct: float
    trail_gap_pct: float
    be_activation_pct: float
    be_offset_pct: float
    tp1_price: float
    tp2_price: float
    sl_price: float
    be_price: float
    atr_multiplier: float
    is_expanded: bool
    is_be_accelerated: bool


# =============================================================================
# CORE VOLATILITY MATH
# =============================================================================

def compute_true_range(high: float, low: float, prev_close: Optional[float] = None) -> float:
    """
    Computes True Range (TR) for a bar:
    TR = max(High - Low, |High - PrevClose|, |Low - PrevClose|)
    """
    hl = max(0.0, high - low)
    if prev_close is None or prev_close <= 0:
        return hl
    hc = abs(high - prev_close)
    lc = abs(low - prev_close)
    return max(hl, hc, lc)


def compute_true_range_pct(high: float, low: float, prev_close: float) -> float:
    """Computes True Range as a percentage of previous close."""
    if prev_close <= 0:
        return 0.0
    tr = compute_true_range(high, low, prev_close)
    return (tr / prev_close) * 100.0


def compute_atr(
    candles: Sequence[Union[Candle, Dict[str, float]]],
    period: int = 14,
    method: str = "wilder",
) -> float:
    """
    Computes 14-period ATR from a sequence of candles using Wilder's RMA or SMA.
    """
    if not candles:
        return 0.0
    if len(candles) == 1:
        c0 = candles[0]
        h = c0.high if isinstance(c0, Candle) else float(c0.get("high", 0.0))
        l = c0.low if isinstance(c0, Candle) else float(c0.get("low", 0.0))
        return max(0.0, h - l)

    trs: List[float] = []
    prev_close: Optional[float] = None

    for c in candles:
        h = c.high if isinstance(c, Candle) else float(c.get("high", 0.0))
        l = c.low if isinstance(c, Candle) else float(c.get("low", 0.0))
        cl = c.close if isinstance(c, Candle) else float(c.get("close", 0.0))
        tr = compute_true_range(h, l, prev_close)
        trs.append(tr)
        prev_close = cl

    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0

    if method.lower() == "sma":
        return sum(trs[-period:]) / period

    # Wilder's Smoothing (Institutional standard ATR)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_atr_pct(
    candles: Sequence[Union[Candle, Dict[str, float]]],
    period: int = 14,
    method: str = "wilder",
) -> float:
    """Computes ATR as a percentage of the latest closing price."""
    if not candles:
        return 0.0
    latest = candles[-1]
    close_price = latest.close if isinstance(latest, Candle) else float(latest.get("close", 0.0))
    if close_price <= 0:
        return 0.0
    atr = compute_atr(candles, period=period, method=method)
    return (atr / close_price) * 100.0


def compute_realized_volatility_1h(
    candles: Sequence[Union[Candle, Dict[str, float]]],
    min_periods: int = 2,
) -> float:
    """
    Computes 1-hour rolling realized volatility (percentage) from up to 60 1-minute candles.
    Uses log returns: r_t = ln(Close_t / Close_{t-1}).
    Returns rolling realized volatility scaled for a 1-hour window.
    """
    if len(candles) < min_periods:
        return 0.0

    # Take up to the last 60 candles (1 hour)
    window = candles[-60:]
    returns: List[float] = []
    for i in range(1, len(window)):
        c_prev = window[i - 1].close if isinstance(window[i - 1], Candle) else float(window[i - 1].get("close", 0.0))
        c_curr = window[i].close if isinstance(window[i], Candle) else float(window[i].get("close", 0.0))
        if c_prev > 0 and c_curr > 0:
            returns.append(math.log(c_curr / c_prev))

    if len(returns) < 2:
        return 0.0

    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_1m = math.sqrt(max(0.0, variance))
    # Scale to 1-hour (60 minutes)
    rv_1h = std_1m * math.sqrt(60) * 100.0
    return rv_1h


def compute_annualized_realized_volatility(
    candles: Sequence[Union[Candle, Dict[str, float]]],
    periods_per_year: int = 525600,  # 365 days * 24h * 60m for crypto
    min_periods: int = 2,
) -> float:
    """
    Computes Annualized Realized Volatility (%) from 1-minute candles.
    sigma_ann = std(1m returns) * sqrt(525600) * 100%
    """
    if len(candles) < min_periods:
        return 0.0

    returns: List[float] = []
    for i in range(1, len(candles)):
        c_prev = candles[i - 1].close if isinstance(candles[i - 1], Candle) else float(candles[i - 1].get("close", 0.0))
        c_curr = candles[i].close if isinstance(candles[i], Candle) else float(candles[i].get("close", 0.0))
        if c_prev > 0 and c_curr > 0:
            returns.append(math.log(c_curr / c_prev))

    if len(returns) < 2:
        return 0.0

    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_1m = math.sqrt(max(0.0, variance))
    return std_1m * math.sqrt(periods_per_year) * 100.0


def calculate_atr_multiplier(current_atr: float, baseline_atr: float) -> float:
    """
    Calculates ATR multiplier = current_ATR / baseline_ATR.
    Safely handles edge cases and clamps output within [0.1, 10.0].
    """
    if baseline_atr <= 1e-12:
        return 1.0
    if current_atr <= 0:
        return 1.0
    mult = current_atr / baseline_atr
    return round(max(0.1, min(10.0, mult)), 4)


# Default baseline ATR percentages by asset class
DEFAULT_BASELINE_ATR_PCT = {
    "CRYPTO": 0.75,     # 0.75% 14-period 1-min baseline ATR
    "FX": 0.15,         # 0.15% baseline ATR
    "INDEX": 0.40,      # 0.40% baseline ATR
    "COMMODITY": 0.60,  # 0.60% baseline ATR
}


# =============================================================================
# VOLATILITY ADAPTIVE EXIT LOGIC
# =============================================================================

def calculate_dynamic_tp_levels(
    base_tp1: float = 2.0,
    base_tp2: float = 4.0,
    atr_multiplier: float = 1.0,
) -> Tuple[float, float]:
    """
    Dynamic TP Expansion:
    - If ATR Multiplier >= 2.0x (violent catalyst):
        TP1 expands from +2.0% -> [+3.5% .. +5.0%]
        TP2 expands from +4.0% -> [+7.0% .. +12.0%]
    - If 1.0 < ATR Multiplier < 2.0:
        Smooth linear interpolation between baseline and expanded.
    - If ATR Multiplier <= 1.0:
        Baseline TP levels preserved.
    """
    if atr_multiplier >= 2.0:
        # Violent catalyst expansion:
        # At 2.0x -> TP1 = 3.5%, TP2 = 7.0%
        # At 3.0x+ -> TP1 = 5.0%, TP2 = 12.0%
        excess = min(1.0, (atr_multiplier - 2.0) / 1.0)
        tp1 = 3.5 + excess * (5.0 - 3.5)
        tp2 = 7.0 + excess * (12.0 - 7.0)
        return round(tp1, 4), round(tp2, 4)

    if atr_multiplier > 1.0:
        # Intermediate scaling
        alpha = atr_multiplier - 1.0  # [0.0, 1.0]
        tp1 = base_tp1 + alpha * (3.5 - base_tp1)
        tp2 = base_tp2 + alpha * (7.0 - base_tp2)
        return round(tp1, 4), round(tp2, 4)

    return round(base_tp1, 4), round(base_tp2, 4)


def calculate_dynamic_trailing_cushion(
    base_trail_gap: float = 1.0,
    atr_multiplier: float = 1.0,
) -> float:
    """
    Dynamic Trailing Stop Cushion:
    - Expand trailing cushion from 1.0% to 2.0% on high-volatility spikes (ATR mult >= 2.0x)
      to avoid early wick-outs.
    - Interpolates smoothly for intermediate spikes.
    """
    if atr_multiplier >= 2.0:
        # At 2.0x and above -> full 2.0% cushion (up to max 2.5% for extreme 3.5x+ spikes)
        excess = min(1.0, (atr_multiplier - 2.0) / 1.5)
        cushion = 2.0 + excess * 0.5
        return round(min(2.5, cushion), 4)

    if atr_multiplier > 1.0:
        alpha = atr_multiplier - 1.0
        cushion = base_trail_gap + alpha * (2.0 - base_trail_gap)
        return round(cushion, 4)

    return round(base_trail_gap, 4)


def calculate_dynamic_be_threshold(
    base_be_threshold: float = 1.5,
    atr_multiplier: float = 1.0,
) -> float:
    """
    Dynamic Breakeven Acceleration:
    - When volatility normalizes quickly (ATR multiplier <= 1.1x), accelerate BE threshold
      from +1.5% down to +0.75%..+1.0% to secure risk-free status faster.
    """
    if atr_multiplier <= 1.1:
        # Normalized volatility: tighten BE activation threshold
        if atr_multiplier <= 0.8:
            return 0.75
        # Linear between 0.8x and 1.1x -> [0.75%, 1.0%]
        return round(0.75 + (atr_multiplier - 0.8) / 0.3 * 0.25, 4)

    return round(base_be_threshold, 4)


def compute_adaptive_exit_levels(
    symbol: str,
    side: str,
    entry_price: float,
    current_atr_pct: float,
    baseline_atr_pct: float = 0.75,
    base_tp1: float = 2.0,
    base_tp2: float = 4.0,
    base_sl: float = 1.5,
    base_trail_gap: float = 1.0,
    be_offset_pct: float = 0.1,
    override_multiplier: Optional[float] = None,
) -> AdaptiveExitLevels:
    """
    Computes complete volatility-adaptive TP ladder and Stop-Loss prices.
    """
    if override_multiplier is not None:
        atr_mult = max(0.1, float(override_multiplier))
    else:
        atr_mult = calculate_atr_multiplier(current_atr_pct, baseline_atr_pct)

    is_long = "BUY" in side.upper() or "LONG" in side.upper()
    is_expanded = atr_mult >= 2.0
    is_normalized = atr_mult <= 1.1

    # 1. Dynamic TP levels
    tp1_pct, tp2_pct = calculate_dynamic_tp_levels(base_tp1, base_tp2, atr_mult)
    tp3_pct = round(tp2_pct * 1.5, 4)  # Runner target / scale 3

    # 2. Dynamic Trailing Cushion
    trail_gap_pct = calculate_dynamic_trailing_cushion(base_trail_gap, atr_mult)
    trail_arm_pct = round(tp1_pct * 0.75, 4)

    # 3. Dynamic BE Acceleration
    be_act_pct = calculate_dynamic_be_threshold(base_be_threshold=base_tp1 * 0.75, atr_multiplier=atr_mult)

    # 4. SL percentage: adaptively widen slightly on extreme spikes to prevent wick-out
    sl_pct = base_sl
    if atr_mult >= 2.5:
        sl_pct = min(base_sl * 1.35, 2.5)

    # 5. Price level calculations
    if is_long:
        tp1_price = entry_price * (1.0 + tp1_pct / 100.0) if entry_price > 0 else 0.0
        tp2_price = entry_price * (1.0 + tp2_pct / 100.0) if entry_price > 0 else 0.0
        sl_price = entry_price * (1.0 - sl_pct / 100.0) if entry_price > 0 else 0.0
        be_price = entry_price * (1.0 + be_offset_pct / 100.0) if entry_price > 0 else 0.0
    else:
        tp1_price = entry_price * (1.0 - tp1_pct / 100.0) if entry_price > 0 else 0.0
        tp2_price = entry_price * (1.0 - tp2_pct / 100.0) if entry_price > 0 else 0.0
        sl_price = entry_price * (1.0 + sl_pct / 100.0) if entry_price > 0 else 0.0
        be_price = entry_price * (1.0 - be_offset_pct / 100.0) if entry_price > 0 else 0.0

    return AdaptiveExitLevels(
        tp1_pct=tp1_pct,
        tp2_pct=tp2_pct,
        tp3_pct=tp3_pct,
        sl_pct=sl_pct,
        trail_arm_pct=trail_arm_pct,
        trail_gap_pct=trail_gap_pct,
        be_activation_pct=be_act_pct,
        be_offset_pct=be_offset_pct,
        tp1_price=tp1_price,
        tp2_price=tp2_price,
        sl_price=sl_price,
        be_price=be_price,
        atr_multiplier=atr_mult,
        is_expanded=is_expanded,
        is_be_accelerated=is_normalized,
    )


# =============================================================================
# VOLATILITY TRACKER & ADAPTIVE ENGINE
# =============================================================================

class AssetVolatilityTracker:
    """
    Maintains 1-minute rolling candles, live tick aggregation, ATR, and Realized Volatility
    for a single trading asset.
    """

    def __init__(
        self,
        asset: str,
        baseline_atr_pct: Optional[float] = None,
        max_candle_history: int = 1440,
        atr_period: int = 14,
    ):
        self.asset = asset.upper()
        self.max_candle_history = max(60, max_candle_history)
        self.atr_period = atr_period

        # Determine default baseline ATR pct
        if baseline_atr_pct is not None and baseline_atr_pct > 0:
            self.baseline_atr_pct = float(baseline_atr_pct)
        else:
            self.baseline_atr_pct = DEFAULT_BASELINE_ATR_PCT.get("CRYPTO", 0.75)

        self.candles: List[Candle] = []
        self.current_candle: Optional[Candle] = None
        self.last_tick_price: float = 0.0
        self.last_tick_time: float = 0.0
        self.recent_ticks: List[Tick] = []

    def on_tick(self, price: float, timestamp: Optional[float] = None, size: float = 0.0) -> None:
        """Ingests a live price tick, aggregating into the active 1-minute candle."""
        if price <= 0:
            return
        ts = timestamp if timestamp is not None else time.time()
        self.last_tick_price = price
        self.last_tick_time = ts
        self.recent_ticks.append(Tick(timestamp=ts, price=price, size=size))

        # Prune old ticks (> 3600 seconds)
        cutoff_tick = ts - 3600.0
        if len(self.recent_ticks) > 100 and self.recent_ticks[0].timestamp < cutoff_tick:
            self.recent_ticks = [t for t in self.recent_ticks if t.timestamp >= cutoff_tick]

        # 1-minute candle bucket timestamp (floored to minute)
        minute_ts = math.floor(ts / 60.0) * 60.0

        if self.current_candle is None:
            self.current_candle = Candle(
                timestamp=minute_ts,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=size,
            )
        elif minute_ts == self.current_candle.timestamp:
            # Update current candle
            self.current_candle.high = max(self.current_candle.high, price)
            self.current_candle.low = min(self.current_candle.low, price)
            self.current_candle.close = price
            self.current_candle.volume += size
        else:
            # Finalize previous candle and start new one
            self.candles.append(self.current_candle)
            if len(self.candles) > self.max_candle_history:
                self.candles.pop(0)
            self.current_candle = Candle(
                timestamp=minute_ts,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=size,
            )

    def on_candle(self, candle: Union[Candle, Dict[str, float]]) -> None:
        """Ingests a completed 1-minute candle directly."""
        c = candle if isinstance(candle, Candle) else Candle(
            timestamp=float(candle.get("timestamp", time.time())),
            open=float(candle.get("open", 0.0)),
            high=float(candle.get("high", 0.0)),
            low=float(candle.get("low", 0.0)),
            close=float(candle.get("close", 0.0)),
            volume=float(candle.get("volume", 0.0)),
        )
        self.candles.append(c)
        if len(self.candles) > self.max_candle_history:
            self.candles.pop(0)
        self.last_tick_price = c.close
        self.last_tick_time = c.timestamp

    def on_candles(self, candles: Sequence[Union[Candle, Dict[str, float]]]) -> None:
        """Batch-ingests historical candles."""
        for c in candles:
            self.on_candle(c)

    def get_all_candles(self) -> List[Candle]:
        """Returns all completed candles plus the active partial candle."""
        res = list(self.candles)
        if self.current_candle is not None:
            res.append(self.current_candle)
        return res

    def get_state(self, current_price: Optional[float] = None) -> VolatilityState:
        """Computes and returns the complete VolatilityState for this asset."""
        all_c = self.get_all_candles()
        price = current_price or self.last_tick_price or (all_c[-1].close if all_c else 0.0)

        if not all_c:
            return VolatilityState(
                asset=self.asset,
                current_price=price,
                current_atr=0.0,
                current_atr_pct=self.baseline_atr_pct,
                baseline_atr=price * (self.baseline_atr_pct / 100.0) if price > 0 else 0.0,
                baseline_atr_pct=self.baseline_atr_pct,
                atr_multiplier=1.0,
                realized_vol_1h_pct=0.0,
                realized_vol_annual_pct=0.0,
                is_violent_catalyst=False,
                is_normalized=True,
                sample_count=0,
            )

        atr_val = compute_atr(all_c, period=self.atr_period, method="wilder")
        atr_pct = (atr_val / price * 100.0) if price > 0 else 0.0

        # If we have sufficient history (> 60 candles), dynamically refine baseline ATR
        if len(all_c) >= 60 and self.baseline_atr_pct <= 0:
            long_atr = compute_atr(all_c, period=min(60, len(all_c)), method="sma")
            baseline_pct = (long_atr / price * 100.0) if price > 0 else self.baseline_atr_pct
        else:
            baseline_pct = self.baseline_atr_pct

        baseline_atr_val = price * (baseline_pct / 100.0) if price > 0 else 0.0
        mult = calculate_atr_multiplier(atr_pct, baseline_pct)
        rv_1h = compute_realized_volatility_1h(all_c)
        rv_ann = compute_annualized_realized_volatility(all_c)

        is_violent = mult >= 2.0
        is_norm = mult <= 1.1

        return VolatilityState(
            asset=self.asset,
            current_price=price,
            current_atr=atr_val,
            current_atr_pct=atr_pct,
            baseline_atr=baseline_atr_val,
            baseline_atr_pct=baseline_pct,
            atr_multiplier=mult,
            realized_vol_1h_pct=rv_1h,
            realized_vol_annual_pct=rv_ann,
            is_violent_catalyst=is_violent,
            is_normalized=is_norm,
            sample_count=len(all_c),
        )


class VolatilityAdaptiveExitEngine:
    """
    Multi-asset Engine managing real-time volatility tracking and adaptive exits.
    """

    def __init__(self, default_baseline_atr_pct: float = 0.75):
        self.default_baseline_atr_pct = default_baseline_atr_pct
        self._trackers: Dict[str, AssetVolatilityTracker] = {}

    def get_tracker(self, symbol: str) -> AssetVolatilityTracker:
        sym = (symbol or "").upper()
        if sym not in self._trackers:
            base_pct = self._resolve_default_baseline(sym)
            self._trackers[sym] = AssetVolatilityTracker(sym, baseline_atr_pct=base_pct)
        return self._trackers[sym]

    def _resolve_default_baseline(self, symbol: str) -> float:
        sym = symbol.upper()
        try:
            from trade_exits import COMMODITY, CRYPTO, FX, INDEX
            if sym in FX:
                return DEFAULT_BASELINE_ATR_PCT["FX"]
            if sym in INDEX:
                return DEFAULT_BASELINE_ATR_PCT["INDEX"]
            if sym in COMMODITY:
                return DEFAULT_BASELINE_ATR_PCT["COMMODITY"]
            if sym in CRYPTO:
                return DEFAULT_BASELINE_ATR_PCT["CRYPTO"]
        except Exception:
            pass
        return self.default_baseline_atr_pct

    def on_tick(self, symbol: str, price: float, timestamp: Optional[float] = None, size: float = 0.0) -> None:
        tracker = self.get_tracker(symbol)
        tracker.on_tick(price, timestamp, size)

    def on_candle(self, symbol: str, candle: Union[Candle, Dict[str, float]]) -> None:
        tracker = self.get_tracker(symbol)
        tracker.on_candle(candle)

    def on_candles(self, symbol: str, candles: Sequence[Union[Candle, Dict[str, float]]]) -> None:
        tracker = self.get_tracker(symbol)
        tracker.on_candles(candles)

    def get_state(self, symbol: str, current_price: Optional[float] = None) -> VolatilityState:
        tracker = self.get_tracker(symbol)
        return tracker.get_state(current_price)

    def calculate_exits(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        base_tp1: float = 2.0,
        base_tp2: float = 4.0,
        base_sl: float = 1.5,
        base_trail_gap: float = 1.0,
        override_multiplier: Optional[float] = None,
    ) -> AdaptiveExitLevels:
        """
        Calculates volatility-adaptive TP/SL and trailing parameters for a position.
        """
        state = self.get_state(symbol, current_price=entry_price)
        baseline = state.baseline_atr_pct or self._resolve_default_baseline(symbol)
        curr_atr = state.current_atr_pct if state.sample_count > 0 else baseline

        return compute_adaptive_exit_levels(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            current_atr_pct=curr_atr,
            baseline_atr_pct=baseline,
            base_tp1=base_tp1,
            base_tp2=base_tp2,
            base_sl=base_sl,
            base_trail_gap=base_trail_gap,
            override_multiplier=override_multiplier,
        )

    def should_accelerate_breakeven(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        current_price: float,
        pnl_pct: Optional[float] = None,
    ) -> bool:
        """
        Returns True if volatility has normalized and position meets accelerated BE threshold.
        """
        if entry_price <= 0 or current_price <= 0:
            return False

        is_long = "BUY" in side.upper() or "LONG" in side.upper()
        if pnl_pct is None:
            pnl_pct = ((current_price - entry_price) / entry_price * 100.0) if is_long else ((entry_price - current_price) / entry_price * 100.0)

        state = self.get_state(symbol, current_price=current_price)
        if not state.is_normalized:
            return False

        # Threshold under normalized volatility (+0.75% to +1.0% profit)
        threshold = calculate_dynamic_be_threshold(base_be_threshold=1.5, atr_multiplier=state.atr_multiplier)
        return pnl_pct >= threshold


# Singleton engine instance for direct import across modules
_GLOBAL_VOLATILITY_ENGINE = VolatilityAdaptiveExitEngine()


def get_volatility_engine() -> VolatilityAdaptiveExitEngine:
    return _GLOBAL_VOLATILITY_ENGINE
