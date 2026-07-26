#!/usr/bin/env python3
"""
Backtest Engine (Upgrade #90)
=============================
Replays recorded trades from b20_trades.db and evaluates alternative exit
strategies against what actually happened.

Honest scope: we only have entry/exit fills recorded per trade (no tick-level
price history), so strategies are evaluated on realized round-trips:
- "actual"        : what the bot really did
- "hold_all"      : never sold early — exit at the final recorded sell multiple
- "tp_ladder"     : sell 25% at 2x, 25% at 5x, rest at final exit
- "stop_loss_X"   : cap losses at -X% (assumes SL would have filled)

Results are written to backtest_results (db_manager schema) when available.

Usage:
  python backtest_engine.py            # run all strategies, print report
  python backtest_engine.py --json     # machine-readable output
"""

import os
import json
import sqlite3
import argparse
import uuid
from datetime import datetime
from typing import Dict, List

DB_FILE = "b20_trades.db"
ANALYTICS_DB = os.path.join("data", "b20_bot.db")


def load_round_trips() -> List[Dict]:
    """Pair each successful buy with its subsequent sells (FIFO per token)."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""SELECT timestamp, token, action, amount, status, COALESCE(token_amount, 0)
                 FROM trades WHERE status='success' ORDER BY timestamp ASC""")
    rows = c.fetchall()
    conn.close()

    open_buys: Dict[str, List[Dict]] = {}
    trips: List[Dict] = []
    for ts, token, action, amount, status, token_amount in rows:
        if action == "buy":
            open_buys.setdefault(token, []).append({
                "entry_ts": ts, "eth_in": amount, "tokens": token_amount,
                "eth_out": 0.0, "exits": []})
        elif action == "sell" and open_buys.get(token):
            trip = open_buys[token][0]
            trip["eth_out"] += amount
            trip["exits"].append({"ts": ts, "eth": amount})
            # Consider the round-trip closed once proceeds recorded
            if trip not in trips:
                trips.append(trip)
                trip["token"] = token
    # Unclosed buys count as open positions (excluded from PnL strategies)
    open_count = sum(len(v) for v in open_buys.values()) - len(trips)
    return trips


def evaluate_strategies(trips: List[Dict]) -> Dict[str, Dict]:
    """Evaluate each strategy over completed round trips."""
    strategies = {}

    def summarize(name: str, pnls: List[float]):
        wins = [p for p in pnls if p > 0]
        total = sum(pnls)
        strategies[name] = {
            "trades": len(pnls),
            "total_pnl_eth": round(total, 6),
            "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
            "avg_pnl_eth": round(total / len(pnls), 6) if pnls else 0.0,
            "max_loss_eth": round(min(pnls), 6) if pnls else 0.0,
        }

    actual, hold_all, tp_ladder, sl_30 = [], [], [], []
    for t in trips:
        eth_in, eth_out = t["eth_in"], t["eth_out"]
        if eth_in <= 0:
            continue
        multiple = eth_out / eth_in if eth_in else 0.0
        pnl_actual = eth_out - eth_in
        actual.append(pnl_actual)
        # hold_all: same as actual for single-exit trips; models no partial TP
        hold_all.append(eth_in * multiple - eth_in)
        # tp_ladder: 25% out at 2x, 25% at 5x if the final multiple reached those levels
        remaining, out = 1.0, 0.0
        if multiple >= 2.0:
            out += 0.25 * eth_in * 2.0
            remaining -= 0.25
        if multiple >= 5.0:
            out += 0.25 * eth_in * 5.0
            remaining -= 0.25
        out += remaining * eth_in * multiple
        tp_ladder.append(out - eth_in)
        # stop_loss_30: losses capped at -30%
        sl_30.append(max(pnl_actual, -0.30 * eth_in))

    summarize("actual", actual)
    summarize("hold_all", hold_all)
    summarize("tp_ladder_2x_5x", tp_ladder)
    summarize("stop_loss_30pct", sl_30)
    return strategies


def store_results(strategies: Dict[str, Dict]) -> bool:
    """Persist to db_manager's backtest_results table if the analytics DB exists."""
    if not os.path.exists(ANALYTICS_DB):
        return False
    try:
        conn = sqlite3.connect(ANALYTICS_DB)
        c = conn.cursor()
        for name, s in strategies.items():
            if s["trades"] == 0:
                continue
            wins = int(round(s["trades"] * s["win_rate"] / 100.0))
            c.execute("""INSERT OR REPLACE INTO backtest_results
                (backtest_id, strategy_name, total_trades, winning_trades, losing_trades,
                 win_rate, roi_percent, parameters, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4())[:8], name, s["trades"], wins, s["trades"] - wins,
                 s["win_rate"], 0.0, json.dumps(s), datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[BACKTEST] store error: {e}")
        return False


def run_backtest(as_json: bool = False) -> Dict[str, Dict]:
    trips = load_round_trips()
    strategies = evaluate_strategies(trips)
    stored = store_results(strategies)

    if as_json:
        print(json.dumps({"round_trips": len(trips), "strategies": strategies, "stored": stored}, indent=2))
    else:
        print(f"=== B20 Backtest ({len(trips)} completed round-trips) ===")
        if not trips:
            print("No completed buy→sell round-trips recorded yet. Trade first, then backtest.")
        for name, s in strategies.items():
            print(f"\n[{name}]")
            print(f"  trades:    {s['trades']}")
            print(f"  total PnL: {s['total_pnl_eth']} ETH")
            print(f"  win rate:  {s['win_rate']}%")
            print(f"  avg PnL:   {s['avg_pnl_eth']} ETH")
            print(f"  max loss:  {s['max_loss_eth']} ETH")
        if stored:
            print("\nResults stored in data/b20_bot.db → backtest_results")
    return strategies


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="B20 backtest engine (upgrade #90)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()
    run_backtest(as_json=args.json)
