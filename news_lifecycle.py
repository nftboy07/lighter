from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from lighter_news_risk import MarketSnapshot
from news_pipeline import NormalizedNewsEvent, stable_hash
from news_markets import AssetMarket


INTENT_STATES = (
    "intent",
    "reserved",
    "submitted",
    "acknowledged",
    "partial",
    "filled",
    "rejected",
    "canceled",
    "unknown",
    "invalidated",
)


@dataclass
class TradeIntent:
    intent_id: str
    event_id: str
    cluster_id: str
    asset: str
    side: str
    requested_usd: float
    status: str = "intent"
    reservation_id: str = ""
    correlation_id: str = ""
    reasons: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    market_index: int = 0
    fill_price: float = 0.0
    fill_size: float = 0.0
    fee_usd: float = 0.0
    tp_pct: float = 2.5
    sl_pct: float = 1.5


@dataclass
class PaperPosition:
    position_id: str
    intent_id: str
    asset: str
    market_index: int
    side: str
    entry_price: float
    size: float
    notional_usd: float
    tp_pct: float
    sl_pct: float
    cluster_id: str
    is_active: bool = True
    created_at: float = field(default_factory=time.time)
    exit_reason: str = ""
    exit_price: float = 0.0


@dataclass
class ExitRetry:
    position_id: str
    reason: str
    attempts: int = 0
    next_attempt_at: float = field(default_factory=time.time)
    last_error: str = ""


class PaperFillSimulator:
    def __init__(self, fee_bps: float = 4.0, slippage_bps: float = 8.0, partial_fill_pct: float = 1.0) -> None:
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.partial_fill_pct = min(1.0, max(0.1, partial_fill_pct))

    def fill(self, intent: TradeIntent, snapshot: MarketSnapshot) -> TradeIntent:
        slip = self.slippage_bps / 10_000.0
        price = snapshot.price * (1.0 + slip if intent.side == "BUY/LONG" else 1.0 - slip)
        filled_usd = intent.requested_usd * self.partial_fill_pct
        size = filled_usd / max(price, 1e-9)
        intent.fill_price = round(price, 8)
        intent.fill_size = round(size, 8)
        intent.fee_usd = round(filled_usd * (self.fee_bps / 10_000.0), 8)
        intent.status = "filled" if self.partial_fill_pct >= 0.999 else "partial"
        return intent


class TradeIntentQueue:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self.intents: Dict[str, TradeIntent] = {}
        self._clusters: Dict[str, str] = {}
        if db_path:
            self._init_db()
            self._restore()

    async def enqueue(self, event: NormalizedNewsEvent, market: AssetMarket, side: str, requested_usd: float) -> TradeIntent:
        async with self._lock:
            if event.cluster_id in self._clusters:
                existing = self.intents[self._clusters[event.cluster_id]]
                return existing
            intent = TradeIntent(
                intent_id=stable_hash(event.event_id, event.cluster_id, str(time.time())),
                event_id=event.event_id,
                cluster_id=event.cluster_id,
                asset=market.symbol,
                side=side,
                requested_usd=requested_usd,
                correlation_id=event.event_id,
                market_index=market.market_index,
                tp_pct=market.tp_pct,
                sl_pct=market.sl_pct,
            )
            self.intents[intent.intent_id] = intent
            self._clusters[event.cluster_id] = intent.intent_id
            self._persist(intent)
            return intent

    async def mark(self, intent_id: str, status: str, reasons: tuple[str, ...] = (), reservation_id: str = "") -> TradeIntent:
        async with self._lock:
            intent = self.intents[intent_id]
            if status not in INTENT_STATES:
                raise ValueError(status)
            intent.status = status
            intent.reasons = reasons
            if reservation_id:
                intent.reservation_id = reservation_id
            self._persist(intent)
            return intent

    async def invalidate_cluster(self, cluster_id: str) -> List[TradeIntent]:
        async with self._lock:
            changed: List[TradeIntent] = []
            intent_id = self._clusters.get(cluster_id)
            if not intent_id:
                return changed
            intent = self.intents[intent_id]
            if intent.status in {"filled", "canceled", "invalidated"}:
                return changed
            intent.status = "invalidated"
            intent.reasons = intent.reasons + ("source correction/retraction",)
            self._persist(intent)
            changed.append(intent)
            return changed

    def pending(self) -> List[TradeIntent]:
        return [intent for intent in self.intents.values() if intent.status in {"intent", "reserved", "submitted", "acknowledged", "partial"}]

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS news_intents (
                    intent_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL
                )"""
            )

    def _persist(self, intent: TradeIntent) -> None:
        if not self.db_path:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO news_intents VALUES (?, ?, ?)",
                (intent.intent_id, json.dumps(asdict(intent)), time.time()),
            )

    def _restore(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT payload FROM news_intents").fetchall()
        for (payload,) in rows:
            data = json.loads(payload)
            data["reasons"] = tuple(data.get("reasons") or ())
            intent = TradeIntent(**data)
            self.intents[intent.intent_id] = intent
            if intent.status not in {"rejected", "canceled", "invalidated"}:
                self._clusters[intent.cluster_id] = intent.intent_id


class PositionClock:
    """First-seen entry time per asset so time-stop survives restart."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path
        self._times: Dict[str, float] = {}
        if db_path:
            self._init_db()
            self._restore()

    def remember(self, asset: str, entry_time: float) -> float:
        key = (asset or "").upper()
        if not key:
            return entry_time
        existing = self._times.get(key)
        if existing and existing > 0:
            return existing
        self._times[key] = float(entry_time)
        self._persist(key, float(entry_time))
        return float(entry_time)

    def recall(self, asset: str) -> Optional[float]:
        return self._times.get((asset or "").upper())

    def forget(self, asset: str) -> None:
        key = (asset or "").upper()
        self._times.pop(key, None)
        if not self.db_path or not key:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM position_clock WHERE asset = ?", (key,))

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS position_clock (
                    asset TEXT PRIMARY KEY, entry_time REAL NOT NULL, updated_at REAL NOT NULL
                )"""
            )

    def _persist(self, asset: str, entry_time: float) -> None:
        if not self.db_path:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO position_clock VALUES (?, ?, ?)",
                (asset, entry_time, time.time()),
            )

    def _restore(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT asset, entry_time FROM position_clock").fetchall()
        for asset, entry_time in rows:
            self._times[str(asset).upper()] = float(entry_time)


class PositionBook:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path
        self.positions: Dict[str, PaperPosition] = {}
        self.exit_retries: Dict[str, ExitRetry] = {}
        if db_path:
            self._init_db()
            self._restore()

    def activate_from_fill(self, intent: TradeIntent) -> PaperPosition:
        position = PaperPosition(
            position_id=f"pos_{intent.intent_id[:16]}",
            intent_id=intent.intent_id,
            asset=intent.asset,
            market_index=intent.market_index,
            side=intent.side,
            entry_price=intent.fill_price,
            size=intent.fill_size,
            notional_usd=intent.fill_price * intent.fill_size,
            tp_pct=intent.tp_pct,
            sl_pct=intent.sl_pct,
            cluster_id=intent.cluster_id,
        )
        self.positions[position.position_id] = position
        self._persist(position)
        return position

    def active(self) -> List[PaperPosition]:
        return [pos for pos in self.positions.values() if pos.is_active]

    def mark_exit(self, position_id: str, reason: str, price: float, submitted: bool) -> PaperPosition:
        pos = self.positions[position_id]
        if submitted:
            pos.is_active = False
            pos.exit_reason = reason
            pos.exit_price = price
            self.exit_retries.pop(position_id, None)
        else:
            retry = self.exit_retries.get(position_id) or ExitRetry(position_id, reason)
            retry.attempts += 1
            retry.next_attempt_at = time.time() + min(60.0, 2 ** retry.attempts)
            retry.last_error = "exit submission failed"
            self.exit_retries[position_id] = retry
        self._persist(pos)
        return pos

    def due_retries(self) -> List[ExitRetry]:
        now = time.time()
        return [item for item in self.exit_retries.values() if item.next_attempt_at <= now]

    def emergency_flatten(self, prices: Dict[str, float]) -> List[PaperPosition]:
        closed = []
        for pos in self.active():
            price = prices.get(pos.asset, pos.entry_price)
            self.mark_exit(pos.position_id, "emergency_flatten", price, submitted=True)
            closed.append(pos)
        return closed

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS news_positions (
                    position_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL
                )"""
            )

    def _persist(self, position: PaperPosition) -> None:
        if not self.db_path:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO news_positions VALUES (?, ?, ?)",
                (position.position_id, json.dumps(asdict(position)), time.time()),
            )

    def _restore(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT payload FROM news_positions").fetchall()
        for (payload,) in rows:
            pos = PaperPosition(**json.loads(payload))
            self.positions[pos.position_id] = pos
            if pos.is_active:
                self.exit_retries.setdefault(pos.position_id, ExitRetry(pos.position_id, "restart_reconciliation"))
