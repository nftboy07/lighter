from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from news_pipeline import NormalizedNewsEvent
from news_quality import quality_veto


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...] = ()
    reservation_id: str = ""
    sized_usd: float = 0.0


@dataclass
class MarketSnapshot:
    asset: str
    price: float
    spread_bps: float = 0.0
    timestamp: float = field(default_factory=time.time)
    depth_usd: float = 0.0
    volatility_bps: float = 0.0
    size_decimals: int = 4
    price_decimals: int = 2
    min_base_amount: float = 0.0
    market_index: int = 0

    @property
    def fresh(self) -> bool:
        max_age = float(os.getenv("NEWS_MAX_PRICE_AGE_SECONDS", "10"))
        return self.price > 0 and time.time() - self.timestamp <= max_age


def promotion_mode() -> str:
    return os.getenv("NEWS_PROMOTION_MODE", "paper").strip().lower()


def live_execution_allowed(cli_live: bool) -> bool:
    """`--live` is the operator confirmation. Kill switch still wins."""
    if os.getenv("NEWS_KILL_SWITCH", "").lower() in {"1", "true", "yes"}:
        return False
    if not cli_live:
        return False
    mode = promotion_mode()
    if mode in {"paper", "shadow"}:
        # Explicit --live still means live; paper/shadow only apply without --live.
        return True
    return True


class LighterNewsRiskGate:
    def __init__(self, live: bool = False) -> None:
        self.live = live
        self.execution_live = live_execution_allowed(live)
        self.max_exposure_usd = float(os.getenv("NEWS_MAX_EXPOSURE_USD", "1000"))
        self.max_trade_usd = float(os.getenv("NEWS_MAX_TRADE_USD", "100"))
        self.max_spread_bps = float(os.getenv("NEWS_MAX_SPREAD_BPS", "100"))
        self.min_confidence = float(os.getenv("NEWS_MIN_CONFIDENCE", "0.70"))
        self.confirmed_only = os.getenv("NEWS_AUTO_TRADE_CONFIRMED_ONLY", "true").lower() == "true"
        self.max_asset_usd = float(os.getenv("NEWS_MAX_ASSET_EXPOSURE_USD", os.getenv("NEWS_MAX_EXPOSURE_USD", "1000")))
        self.max_directional_usd = float(os.getenv("NEWS_MAX_DIRECTIONAL_USD", os.getenv("NEWS_MAX_EXPOSURE_USD", "1000")))
        self.max_daily_loss_usd = float(os.getenv("NEWS_MAX_DAILY_LOSS_USD", "50"))
        self.max_consecutive_losses = int(os.getenv("NEWS_MAX_CONSECUTIVE_LOSSES", "3"))
        self.max_session_trades = int(os.getenv("NEWS_MAX_SESSION_TRADES", "20"))
        self.cooldown_seconds = float(os.getenv("NEWS_ASSET_COOLDOWN_SECONDS", "30"))
        self.risk_per_trade_pct = float(os.getenv("NEWS_RISK_PER_TRADE_PCT", "1.0"))
        self._reserved_usd = 0.0
        self._reservations: Dict[str, float] = {}
        self._asset_reserved: Dict[str, float] = {}
        self._directional_reserved: Dict[str, float] = {}
        self._last_asset_trade: Dict[str, float] = {}
        self._session_trades = 0
        self._consecutive_losses = 0
        self._daily_loss_usd = 0.0
        self._daily_date = ""
        self._pnl_db = os.getenv("NEWS_DB_PATH", "lighter_news.db")
        self._lock = asyncio.Lock()
        self._load_daily_pnl()

    def size_trade(self, requested_usd: float, stop_distance_pct: float, collateral_usd: float) -> float:
        if stop_distance_pct <= 0:
            return min(requested_usd, self.max_trade_usd)
        risk_budget = collateral_usd * (self.risk_per_trade_pct / 100.0)
        sized = risk_budget / (stop_distance_pct / 100.0)
        return round(max(0.0, min(requested_usd, self.max_trade_usd, sized)), 4)

    async def approve(
        self,
        event: Optional[NormalizedNewsEvent],
        snapshot: Optional[MarketSnapshot],
        requested_usd: float,
        confirmed: bool,
        authorized: bool = True,
        asset: str = "",
        side: str = "",
        collateral_usd: Optional[float] = None,
        stop_distance_pct: float = 1.5,
        momentum_confirmed: Optional[bool] = None,
    ) -> RiskDecision:
        reasons = []
        self._roll_day()
        kill_file = os.getenv("NEWS_KILL_SWITCH_FILE", "NEWS_KILL_SWITCH")
        if os.getenv("NEWS_KILL_SWITCH", "").lower() in {"1", "true", "yes"} or os.path.exists(kill_file):
            reasons.append("news kill switch is engaged")
        if self.live and not self.execution_live:
            reasons.append("live promotion gate is closed; paper/shadow/canary only")
        if requested_usd <= 0:
            reasons.append("trade size exceeds news risk cap")
        if self.live and not authorized:
            reasons.append("live Telegram authorization is not configured")
        if self.live and (snapshot is None or not snapshot.fresh):
            reasons.append("live market price is missing or stale")
        if self.execution_live and collateral_usd is None:
            reasons.append("live collateral query failed")
        if snapshot and snapshot.spread_bps > self.max_spread_bps:
            reasons.append("market spread exceeds news risk cap")
        if snapshot and snapshot.volatility_bps > float(os.getenv("NEWS_MAX_VOL_BPS", "250")):
            reasons.append("volatility shock circuit breaker")
        ok, veto_reason = quality_veto(event)
        if not ok:
            reasons.append(veto_reason)
        elif event is not None and event.confidence < self.min_confidence:
            reasons.append("news confidence is below threshold")
        elif event is not None and event.contradiction:
            reasons.append("contradictory news cluster")
        elif event is not None and event.invalidated:
            reasons.append("news cluster was corrected or retracted")
        if self.live and self.confirmed_only and not confirmed:
            reasons.append("news event lacks independent-source confirmation")
        if momentum_confirmed is False:
            reasons.append("cross-exchange momentum confirmation failed (no Binance/Bybit volume spike)")
        elif momentum_confirmed is None and getattr(self, "momentum_filter", None) is not None and event is not None and asset:
            try:
                sentiment = "BULLISH" if side.startswith("BUY") else "BEARISH" if side.startswith("SELL") else "NEUTRAL"
                if event.confidence >= getattr(self.momentum_filter, "high_conviction_threshold", 0.80):
                    m_eval = self.momentum_filter.evaluate_buffer(asset, sentiment)
                    if not m_eval.direction_aligned:
                        reasons.append(f"cross-exchange momentum contradiction on Binance/Bybit for {asset}")
                    elif not m_eval.confirmed and getattr(self.momentum_filter, "require_confirmation", False):
                        reasons.append(f"cross-exchange momentum unconfirmed on Binance/Bybit for {asset}")
            except Exception:
                pass
        if collateral_usd is None:
            sized = min(requested_usd, self.max_trade_usd)
        else:
            sized = self.size_trade(requested_usd, stop_distance_pct, collateral_usd)
        if sized <= 0 or sized > self.max_trade_usd:
            reasons.append("trade size exceeds news risk cap")
        symbol = (asset or (snapshot.asset if snapshot else "")).upper()
        direction = "long" if side.startswith("BUY") else "short" if side.startswith("SELL") else "flat"
        async with self._lock:
            if self._session_trades >= self.max_session_trades:
                reasons.append("session trade-count breaker")
            if self._consecutive_losses >= self.max_consecutive_losses:
                reasons.append("consecutive-loss breaker")
            if self._daily_loss_usd >= self.max_daily_loss_usd:
                reasons.append("daily loss breaker")
            last = self._last_asset_trade.get(symbol, 0.0)
            if symbol and time.time() - last < self.cooldown_seconds:
                reasons.append("asset cooldown active")
            if self._reserved_usd + sized > self.max_exposure_usd:
                reasons.append("aggregate news exposure cap reached")
            if symbol and self._asset_reserved.get(symbol, 0.0) + sized > self.max_asset_usd:
                reasons.append("per-asset exposure cap reached")
            if direction != "flat" and self._directional_reserved.get(direction, 0.0) + sized > self.max_directional_usd:
                reasons.append("directional exposure cap reached")
            if reasons:
                return RiskDecision(False, tuple(reasons), sized_usd=sized)
            reservation_id = f"news_res_{int(time.time() * 1000)}"
            self._reservations[reservation_id] = sized
            self._reserved_usd += sized
            if symbol:
                self._asset_reserved[symbol] = self._asset_reserved.get(symbol, 0.0) + sized
            if direction != "flat":
                self._directional_reserved[direction] = self._directional_reserved.get(direction, 0.0) + sized
            return RiskDecision(True, (), reservation_id, sized)

    def record_fill(self, asset: str) -> None:
        symbol = (asset or "").upper()
        if symbol:
            self._last_asset_trade[symbol] = time.time()
        self._session_trades += 1
        self._save_daily_pnl()

    async def release(self, reservation_id: str, asset: str = "", side: str = "") -> None:
        async with self._lock:
            amount = self._reservations.pop(reservation_id, 0.0)
            self._reserved_usd = max(0.0, self._reserved_usd - amount)
            symbol = asset.upper()
            if symbol:
                self._asset_reserved[symbol] = max(0.0, self._asset_reserved.get(symbol, 0.0) - amount)
            direction = "long" if side.startswith("BUY") else "short" if side.startswith("SELL") else ""
            if direction:
                self._directional_reserved[direction] = max(0.0, self._directional_reserved.get(direction, 0.0) - amount)

    def record_pnl(self, pnl_usd: float) -> None:
        self._roll_day()
        if pnl_usd < 0:
            self._consecutive_losses += 1
            self._daily_loss_usd += abs(pnl_usd)
        else:
            self._consecutive_losses = 0
        self._save_daily_pnl()

    def _utc_day(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        today = self._utc_day()
        if self._daily_date != today:
            self._daily_date = today
            self._daily_loss_usd = 0.0
            self._consecutive_losses = 0
            self._session_trades = 0

    def _load_daily_pnl(self) -> None:
        try:
            with sqlite3.connect(self._pnl_db) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS news_daily_pnl (day TEXT PRIMARY KEY, loss_usd REAL NOT NULL, consecutive INTEGER NOT NULL, trades INTEGER NOT NULL, payload TEXT NOT NULL)"
                )
                row = conn.execute("SELECT day, loss_usd, consecutive, trades FROM news_daily_pnl WHERE day = ?", (self._utc_day(),)).fetchone()
            if row:
                self._daily_date, self._daily_loss_usd, self._consecutive_losses, self._session_trades = row[0], float(row[1]), int(row[2]), int(row[3])
            else:
                self._daily_date = self._utc_day()
        except sqlite3.Error:
            self._daily_date = self._utc_day()

    def _save_daily_pnl(self) -> None:
        try:
            with sqlite3.connect(self._pnl_db) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS news_daily_pnl (day TEXT PRIMARY KEY, loss_usd REAL NOT NULL, consecutive INTEGER NOT NULL, trades INTEGER NOT NULL, payload TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO news_daily_pnl VALUES (?, ?, ?, ?, ?)",
                    (
                        self._utc_day(),
                        self._daily_loss_usd,
                        self._consecutive_losses,
                        self._session_trades,
                        json.dumps({"updated": time.time()}),
                    ),
                )
        except sqlite3.Error:
            pass

    def readiness(self, authorized: bool, snapshot: Optional[MarketSnapshot], has_markets: bool) -> tuple[bool, tuple[str, ...]]:
        reasons = []
        if os.getenv("NEWS_KILL_SWITCH", "").lower() in {"1", "true", "yes"}:
            reasons.append("kill switch")
        if self.live and not authorized:
            reasons.append("missing telegram authorization")
        if self.live and (snapshot is None or not snapshot.fresh):
            reasons.append("missing or stale market data")
        if not has_markets:
            reasons.append("market registry empty")
        if self.live and not self.execution_live:
            reasons.append("live confirmation missing")
        return (not reasons), tuple(reasons)

    @property
    def reserved_usd(self) -> float:
        return self._reserved_usd
