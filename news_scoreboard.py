from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional


@dataclass
class ShadowBet:
    bet_id: str
    asset: str
    side: str
    event_type: str
    headline: str
    entry_price: float
    created_at: float
    horizon_seconds: float = 3600.0
    exit_price: float = 0.0
    hit: Optional[int] = None
    cluster_id: str = ""


class ShadowScoreboard:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path
        self.bets: Dict[str, ShadowBet] = {}
        if db_path:
            self._init()
            self._restore()

    def record(self, bet: ShadowBet) -> None:
        self.bets[bet.bet_id] = bet
        self._persist(bet)

    def due(self, now: Optional[float] = None) -> List[ShadowBet]:
        ts = now or time.time()
        return [bet for bet in self.bets.values() if bet.hit is None and ts - bet.created_at >= bet.horizon_seconds]

    def settle(self, bet_id: str, exit_price: float) -> Optional[ShadowBet]:
        bet = self.bets.get(bet_id)
        if bet is None or bet.entry_price <= 0 or exit_price <= 0:
            return None
        up = exit_price >= bet.entry_price
        long = bet.side.startswith("BUY")
        bet.exit_price = exit_price
        bet.hit = 1 if (long and up) or (not long and not up) else 0
        self._persist(bet)
        return bet

    def summary(self) -> Dict[str, float]:
        closed = [bet for bet in self.bets.values() if bet.hit is not None]
        if not closed:
            return {"closed": 0, "hits": 0, "hit_rate": 0.0}
        hits = sum(bet.hit or 0 for bet in closed)
        return {"closed": len(closed), "hits": hits, "hit_rate": round(hits / len(closed), 3)}

    def _init(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS news_shadow_bets (bet_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
            )

    def _persist(self, bet: ShadowBet) -> None:
        if not self.db_path:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO news_shadow_bets VALUES (?, ?, ?)",
                (bet.bet_id, json.dumps(asdict(bet)), time.time()),
            )

    def _restore(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT payload FROM news_shadow_bets").fetchall()
        for (payload,) in rows:
            data = json.loads(payload)
            self.bets[data["bet_id"]] = ShadowBet(**data)
