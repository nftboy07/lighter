#!/usr/bin/env python3
"""
SQLite Persistence & Analytics Manager for Lighter MM Bot
==========================================================
Manages thread-safe SQLite operations for:
- Maker order fills & trade execution logs
- Active and historical quotes
- Rolling 24h & hourly volume calculations
- Campaign reward points estimator (Lighter x Robinhood $25M pool)
- Realized & Unrealized PnL snapshots
"""

import sqlite3
import time
import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple


class LighterDBManager:
    """
    SQLite Database Manager with WAL (Write-Ahead Logging) mode
    for high-throughput market making execution and analytics.
    """

    def __init__(self, db_path: str = "lighter_mm.db"):
        self.db_path = db_path
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        """Returns a new connection with Row factory and WAL mode."""
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def init_db(self):
        """Initializes tables and indexes for MM tracking."""
        conn = self.get_connection()
        cursor = conn.cursor()

        # 1. Fills Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_index INTEGER NOT NULL,
                order_id TEXT,
                client_order_id TEXT,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                usd_value REAL NOT NULL,
                fee_rebate REAL DEFAULT 0.0,
                realized_pnl REAL DEFAULT 0.0,
                is_maker INTEGER DEFAULT 1,
                tx_hash TEXT,
                timestamp REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 2. Quotes Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_index INTEGER NOT NULL,
                mid_price REAL NOT NULL,
                spread_bps REAL NOT NULL,
                inventory REAL NOT NULL,
                bid_quotes_json TEXT NOT NULL,
                ask_quotes_json TEXT NOT NULL,
                timestamp REAL NOT NULL
            );
        """)

        # 3. PnL & Volume Snapshots
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pnl_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_index INTEGER NOT NULL,
                total_volume_usd REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                current_inventory REAL NOT NULL,
                mid_price REAL NOT NULL,
                estimated_points REAL NOT NULL,
                fill_count INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Indexes for fast querying
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fills_timestamp ON fills(timestamp);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fills_market ON fills(market_index);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pnl_timestamp ON pnl_snapshots(timestamp);")

        conn.commit()
        conn.close()

    def record_fill(
        self,
        market_index: int,
        order_id: str,
        client_order_id: str,
        side: str,
        price: float,
        size: float,
        usd_value: float,
        fee_rebate: float = 0.0,
        realized_pnl: float = 0.0,
        is_maker: bool = True,
        tx_hash: Optional[str] = None,
    ) -> int:
        """Records an executed fill and returns the new row id."""
        conn = self.get_connection()
        cursor = conn.cursor()
        now_ts = time.time()

        cursor.execute("""
            INSERT INTO fills (
                market_index, order_id, client_order_id, side, price, size,
                usd_value, fee_rebate, realized_pnl, is_maker, tx_hash, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market_index,
            str(order_id),
            str(client_order_id),
            side.upper(),
            price,
            size,
            usd_value,
            fee_rebate,
            realized_pnl,
            1 if is_maker else 0,
            tx_hash or "",
            now_ts,
        ))

        fill_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return fill_id

    def record_quote_snapshot(
        self,
        market_index: int,
        mid_price: float,
        spread_bps: float,
        inventory: float,
        bid_quotes: list,
        ask_quotes: list,
    ):
        """Saves a periodic snapshot of current quotes."""
        conn = self.get_connection()
        cursor = conn.cursor()
        now_ts = time.time()

        cursor.execute("""
            INSERT INTO quotes (
                market_index, mid_price, spread_bps, inventory,
                bid_quotes_json, ask_quotes_json, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            market_index,
            mid_price,
            spread_bps,
            inventory,
            json.dumps(bid_quotes),
            json.dumps(ask_quotes),
            now_ts,
        ))

        conn.commit()
        conn.close()

    def record_pnl_snapshot(
        self,
        market_index: int,
        total_volume_usd: float,
        realized_pnl: float,
        unrealized_pnl: float,
        current_inventory: float,
        mid_price: float,
        estimated_points: float,
        fill_count: int,
    ):
        """Records an equity/volume snapshot."""
        conn = self.get_connection()
        cursor = conn.cursor()
        now_ts = time.time()

        cursor.execute("""
            INSERT INTO pnl_snapshots (
                market_index, total_volume_usd, realized_pnl, unrealized_pnl,
                current_inventory, mid_price, estimated_points, fill_count, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market_index,
            total_volume_usd,
            realized_pnl,
            unrealized_pnl,
            current_inventory,
            mid_price,
            estimated_points,
            fill_count,
            now_ts,
        ))

        conn.commit()
        conn.close()

    def get_stats(self, market_index: Optional[int] = None, window_seconds: Optional[float] = None) -> Dict[str, Any]:
        """
        Calculates aggregate stats:
        - Total traded volume ($ USD)
        - Total fills count (Buy vs Sell)
        - Total Realized PnL ($ USD)
        - Estimated Lighter & Robinhood Campaign Reward Points
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        query = "SELECT COUNT(*), COALESCE(SUM(usd_value), 0.0), COALESCE(SUM(realized_pnl), 0.0), COALESCE(SUM(fee_rebate), 0.0) FROM fills"
        params = []
        conditions = []

        if market_index is not None:
            conditions.append("market_index = ?")
            params.append(market_index)

        if window_seconds is not None:
            since_ts = time.time() - window_seconds
            conditions.append("timestamp >= ?")
            params.append(since_ts)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        cursor.execute(query, params)
        row = cursor.fetchone()

        total_fills = row[0] if row else 0
        total_volume = row[1] if row else 0.0
        total_realized_pnl = row[2] if row else 0.0
        total_rebates = row[3] if row else 0.0

        # Points estimation:
        # Based on Lighter campaign volume program: ~4 points per $1M volume (or user rate ~12 points per $3M)
        # 12 points per $3,000,000 = 4.0 points per million USD
        points_per_million = 4.0
        estimated_points = (total_volume / 1_000_000.0) * points_per_million

        # Buy vs Sell count
        buy_query = "SELECT COUNT(*) FROM fills WHERE side = 'BUY'"
        sell_query = "SELECT COUNT(*) FROM fills WHERE side = 'SELL'"
        if conditions:
            buy_query += " AND " + " AND ".join(conditions)
            sell_query += " AND " + " AND ".join(conditions)

        cursor.execute(buy_query, params)
        buy_fills = cursor.fetchone()[0]

        cursor.execute(sell_query, params)
        sell_fills = cursor.fetchone()[0]

        # Win rate calculation (fills with realized_pnl > 0 vs realized_pnl < 0)
        win_query = "SELECT COUNT(*) FROM fills WHERE realized_pnl > 0"
        loss_query = "SELECT COUNT(*) FROM fills WHERE realized_pnl < 0"
        be_query = "SELECT COUNT(*) FROM fills WHERE realized_pnl = 0"
        if conditions:
            win_query += " AND " + " AND ".join(conditions)
            loss_query += " AND " + " AND ".join(conditions)
            be_query += " AND " + " AND ".join(conditions)

        cursor.execute(win_query, params)
        winning_trades = cursor.fetchone()[0]

        cursor.execute(loss_query, params)
        losing_trades = cursor.fetchone()[0]

        cursor.execute(be_query, params)
        breakeven_trades = cursor.fetchone()[0]

        closed_trades = winning_trades + losing_trades
        win_rate_pct = round((winning_trades / closed_trades * 100.0), 2) if closed_trades > 0 else 0.0

        conn.close()

        return {
            "total_fills": total_fills,
            "buy_fills": buy_fills,
            "sell_fills": sell_fills,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "breakeven_trades": breakeven_trades,
            "win_rate_pct": win_rate_pct,
            "total_volume_usd": round(total_volume, 2),
            "total_realized_pnl_usd": round(total_realized_pnl, 2),
            "total_rebates_usd": round(total_rebates, 4),
            "net_pnl_usd": round(total_realized_pnl + total_rebates, 2),
            "estimated_points": round(estimated_points, 4),
        }

    def get_daily_stats(self, market_index: Optional[int] = None, window_seconds: float = 86400.0) -> Dict[str, Any]:
        """
        Calculates 24-hour daily performance analytics:
        - Daily realized PnL & net PnL
        - Win rate percentage
        - Daily volume farmed
        - Daily estimated Robinhood x zkLighter points
        - Cumulative all-time totals
        """
        daily_stats = self.get_stats(market_index=market_index, window_seconds=window_seconds)
        all_time_stats = self.get_stats(market_index=market_index, window_seconds=None)

        return {
            "window_seconds": window_seconds,
            "daily_volume_usd": daily_stats["total_volume_usd"],
            "daily_realized_pnl_usd": daily_stats["total_realized_pnl_usd"],
            "daily_net_pnl_usd": daily_stats["net_pnl_usd"],
            "daily_rebates_usd": daily_stats["total_rebates_usd"],
            "daily_fills": daily_stats["total_fills"],
            "daily_buy_fills": daily_stats["buy_fills"],
            "daily_sell_fills": daily_stats["sell_fills"],
            "daily_winning_trades": daily_stats["winning_trades"],
            "daily_losing_trades": daily_stats["losing_trades"],
            "daily_win_rate_pct": daily_stats["win_rate_pct"],
            "daily_points": daily_stats["estimated_points"],
            # Cumulative all-time stats
            "all_time_volume_usd": all_time_stats["total_volume_usd"],
            "all_time_pnl_usd": all_time_stats["total_realized_pnl_usd"],
            "all_time_points": all_time_stats["estimated_points"],
            "all_time_fills": all_time_stats["total_fills"],
            "all_time_win_rate_pct": all_time_stats["win_rate_pct"],
        }

    def get_recent_fills(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Returns the most recent fills."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, market_index, side, price, size, usd_value, realized_pnl, is_maker, timestamp
            FROM fills ORDER BY timestamp DESC LIMIT ?
        """, (limit,))

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "id": r["id"],
                "market_index": r["market_index"],
                "side": r["side"],
                "price": r["price"],
                "size": r["size"],
                "usd_value": r["usd_value"],
                "realized_pnl": r["realized_pnl"],
                "is_maker": bool(r["is_maker"]),
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]
