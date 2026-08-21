from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class ExitPolicy:
    tp_pct: float
    sl_pct: float
    trail_arm_pct: float
    trail_gap_pct: float
    max_hold_seconds: float
    max_spread_bps: float


FX = {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "USDHKD", "USDKRW"}
INDEX = {"SPY", "QQQ", "SPX", "US500", "US100", "IWM"}
COMMODITY = {"WTI", "BRENTOIL", "XAU", "XAG", "XCU", "XPT", "XPD", "NATGAS", "WHEAT", "PAXG", "XAUT"}
CRYPTO = {"BTC", "ETH", "SOL", "HYPE", "XRP", "DOGE", "ADA", "AVAX", "BNB", "LTC", "LINK", "DOT"}


def policy_for(symbol: str, override_tp: float | None = None, override_sl: float | None = None) -> ExitPolicy:
    sym = (symbol or "").upper()
    if sym in FX:
        base = ExitPolicy(0.40, 0.30, 0.25, 0.15, 45 * 60, 25)
    elif sym in INDEX:
        base = ExitPolicy(1.20, 0.80, 0.80, 0.50, 60 * 60, 40)
    elif sym in COMMODITY:
        base = ExitPolicy(2.00, 1.20, 1.20, 0.80, 60 * 60, 50)
    elif sym in CRYPTO:
        base = ExitPolicy(2.00, 1.50, 2.00, 1.00, 45 * 60, 80)
    else:
        base = ExitPolicy(1.50, 1.00, 1.00, 0.60, 90 * 60, 60)
    return ExitPolicy(
        tp_pct=override_tp if override_tp is not None else base.tp_pct,
        sl_pct=override_sl if override_sl is not None else base.sl_pct,
        trail_arm_pct=base.trail_arm_pct,
        trail_gap_pct=base.trail_gap_pct,
        max_hold_seconds=base.max_hold_seconds,
        max_spread_bps=base.max_spread_bps,
    )


def already_through_exit(side: str, mark: float, tp_price: float, sl_price: float) -> Optional[str]:
    """If the mark is already through TP or SL, return TAKE_PROFIT / STOP_LOSS."""
    if mark <= 0:
        return None
    long = side.startswith("BUY")
    if long:
        if tp_price and mark >= tp_price:
            return "TAKE_PROFIT"
        if sl_price and mark <= sl_price:
            return "STOP_LOSS"
        return None
    if tp_price and mark <= tp_price:
        return "TAKE_PROFIT"
    if sl_price and mark >= sl_price:
        return "STOP_LOSS"
    return None


# Multi-Stage Scale-Out Ladder:
# Level 1: At +2.0% profit, close 50% size and shift SL to Breakeven (+0.1%).
# Level 2: At +4.0% profit, close 25% size.
# Level 3: Keep 25% runner with 1.0% dynamic trailing stop.
PARTIAL_FRACS = (0.50, 0.25, 0.25)
PARTIAL_MULTS = (1.0, 2.0, 3.0)
SCALE_OUT_TARGET_PCTS = (2.0, 4.0)
BE_OFFSET_PCT = 0.1
RUNNER_TRAIL_GAP_PCT = 1.0


def scale_tp_price(side: str, entry: float, policy: ExitPolicy, level: int) -> float:
    """TP price for scale-out level 1..3."""
    lvl = max(1, min(3, int(level)))
    if policy is not None and policy.tp_pct is not None:
        pct = policy.tp_pct * PARTIAL_MULTS[lvl - 1]
    else:
        pct = SCALE_OUT_TARGET_PCTS[min(len(SCALE_OUT_TARGET_PCTS) - 1, lvl - 1)]
    long = side.startswith("BUY")
    if long:
        return entry * (1.0 + pct / 100.0)
    return entry * (1.0 - pct / 100.0)


def breakeven_sl(side: str, entry: float, offset_pct: float = BE_OFFSET_PCT) -> float:
    """Calculates Breakeven (+0.1%) Stop-Loss price in favor of trade."""
    long = side.startswith("BUY")
    if long:
        return entry * (1.0 + offset_pct / 100.0)
    return entry * (1.0 - offset_pct / 100.0)


def partial_qty(original: float, remaining: float, level: int) -> float:
    """Calculates size to close at scale-out level (Level 1: 50%, Level 2: 25%, Level 3: remainder/25%)."""
    if remaining <= 0 or original <= 0:
        return 0.0
    if level == 1:
        qty = original * PARTIAL_FRACS[0]
    elif level == 2:
        qty = original * PARTIAL_FRACS[1]
    else:
        return remaining
    return min(remaining, max(0.0, qty))


def infer_tp_hits(side: str, entry: float, mark: float, policy: ExitPolicy) -> int:
    """How many scale-out levels the mark has already cleared (0..3)."""
    if entry <= 0 or mark <= 0:
        return 0
    long = side.startswith("BUY")
    pnl_pct = ((mark - entry) / entry * 100.0) if long else ((entry - mark) / entry * 100.0)
    hits = 0
    for i, mult in enumerate(PARTIAL_MULTS, 1):
        target = (policy.tp_pct * mult) if policy else (2.0 * mult)
        if pnl_pct + 1e-12 >= target:
            hits = i
        else:
            break
    return hits


def scaled_out_qty(original: float, remaining: float, hits: int) -> float:
    """Qty that should already be closed after `hits` TP levels."""
    if hits <= 0 or original <= 0 or remaining <= 0:
        return 0.0
    if hits == 1:
        return min(remaining, original * PARTIAL_FRACS[0])
    if hits == 2:
        return min(remaining, original * (PARTIAL_FRACS[0] + PARTIAL_FRACS[1]))
    return remaining


def tp_ladder_prices(side: str, entry: float, policy: ExitPolicy) -> Tuple[float, float, float]:
    return (
        scale_tp_price(side, entry, policy, 1),
        scale_tp_price(side, entry, policy, 2),
        scale_tp_price(side, entry, policy, 3),
    )


def tp_sl_prices(side: str, entry: float, policy: ExitPolicy) -> Tuple[float, float]:
    long = side.startswith("BUY")
    if long:
        return entry * (1.0 + policy.tp_pct / 100.0), entry * (1.0 - policy.sl_pct / 100.0)
    return entry * (1.0 - policy.tp_pct / 100.0), entry * (1.0 + policy.sl_pct / 100.0)


def trail_stop(side: str, entry: float, high: float, low: float, current_sl: float, policy: ExitPolicy) -> float:
    long = side.startswith("BUY")
    if long:
        pnl_pct = (high - entry) / entry * 100.0 if entry else 0.0
        if pnl_pct < policy.trail_arm_pct:
            return current_sl
        armed = entry * (1.0 + BE_OFFSET_PCT / 100.0)
        trailed = high * (1.0 - policy.trail_gap_pct / 100.0)
        return max(current_sl, armed, trailed)
    pnl_pct = (entry - low) / entry * 100.0 if entry else 0.0
    if pnl_pct < policy.trail_arm_pct:
        return current_sl
    armed = entry * (1.0 - BE_OFFSET_PCT / 100.0)
    trailed = low * (1.0 + policy.trail_gap_pct / 100.0)
    return min(current_sl, armed, trailed) if current_sl else min(armed, trailed)


def dynamic_kelly_margin(conviction: float) -> float:
    """
    Dynamic Kelly Margin Utilization scaling by catalyst conviction:
    - 98% conviction -> 90% size
    - 85% conviction -> 65% size
    - 75% conviction -> 40% size
    """
    c = float(conviction)
    if c <= 0:
        return 0.0
    if c >= 0.98:
        # 98% -> 90% (scaled up to max 95% at 1.0)
        return min(95.0, 90.0 + (c - 0.98) / 0.02 * 5.0) if c > 0.98 else 90.0
    if c >= 0.85:
        # 85% to 98% -> 65% to 90%
        return 65.0 + (c - 0.85) / (0.98 - 0.85) * (90.0 - 65.0)
    if c >= 0.75:
        # 75% to 85% -> 40% to 65%
        return 40.0 + (c - 0.75) / (0.85 - 0.75) * (65.0 - 40.0)
    # Below 75%, scale down proportionally
    return max(0.0, 40.0 * (c / 0.75))


def protect_limit_price(side: str, kind: str, trigger: float, slip_pct: float = 0.15) -> float:
    """Limit price for GTT TP/SL so the order can fill through the trigger."""
    long = side.startswith("BUY")
    slip = slip_pct / 100.0
    if kind == "tp":
        return trigger
    if long:
        return trigger * (1.0 - slip)
    return trigger * (1.0 + slip)


def order_age_seconds(order: dict, now: float) -> float:
    for key in ("timestamp", "created_at", "created_time", "time", "updated_at"):
        value = (order or {}).get(key)
        if value is None or value == "":
            continue
        try:
            ts = float(value)
        except (TypeError, ValueError):
            continue
        if ts > 1e12:
            ts /= 1000.0
        if ts > 1e9:
            return max(0.0, now - ts)
    return 0.0


def classify_working_order(
    order: dict,
    live_markets: set,
    live_symbols: set,
    now: float,
    max_age_seconds: float,
    entry_ttl_seconds: float = 90.0,
) -> str:
    """keep | orphan | stale_entry | stale_protect"""
    item = order or {}
    market = item.get("market_id", item.get("market_index", item.get("market")))
    try:
        market_i = int(float(market)) if market is not None and market != "" else -1
    except (TypeError, ValueError):
        market_i = -1
    symbol = str(item.get("symbol") or item.get("market_symbol") or "").upper()
    has_pos = (market_i in live_markets) or (bool(symbol) and symbol in live_symbols)
    if not has_pos:
        return "orphan"
    reduce_only = bool(item.get("reduce_only") or item.get("is_reduce_only") or item.get("reduceOnly"))
    otype = str(item.get("type") or item.get("order_type") or item.get("kind") or "").lower()
    if "tp" in otype or "sl" in otype or "stop" in otype or "take" in otype:
        reduce_only = True
    age = order_age_seconds(item, now)
    if not reduce_only and age >= entry_ttl_seconds:
        return "stale_entry"
    if reduce_only and max_age_seconds > 0 and age >= max_age_seconds:
        return "stale_protect"
    return "keep"


def parse_manual_trade(text: str) -> Optional[Tuple[str, bool]]:
    """Parse 'gold', 'buy aapl', 'short nvda' -> (SYMBOL, is_ask)."""
    raw = (text or "").strip().lower()
    if not raw or raw.startswith("/") or raw.startswith("menu_"):
        return None
    is_ask = False
    token = raw
    for prefix, ask in (("short ", True), ("sell ", True), ("buy ", False), ("long ", False)):
        if raw.startswith(prefix):
            is_ask = ask
            token = raw[len(prefix):].strip()
            break
    token = " ".join(token.split())
    if not token or token in {
        "close", "exit", "status", "pause", "resume", "help", "menu", "flatten",
        "kill", "live", "shadow", "positions", "balance", "sources", "signals",
        "start", "risk", "intents", "metrics", "pnl",
    }:
        return None
    try:
        from news_universe import ASSET_ALIASES, listed_symbols
    except Exception:
        ASSET_ALIASES = {}
        listed_symbols = lambda: set()  # noqa: E731
    if token in ASSET_ALIASES:
        return ASSET_ALIASES[token], is_ask
    up = token.upper().replace(" ", "")
    known = set(ASSET_ALIASES.values()) | listed_symbols()
    if up in known:
        return up, is_ask
    return None
