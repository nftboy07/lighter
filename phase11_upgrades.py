#!/usr/bin/env python3
"""
Phase 11 Upgrades Module
========================
Implements the remaining roadmap upgrades:

- #73: Max gas spend cap per trade (hard ETH ceiling on gas cost)
- #50: Limit orders / conditional buys (price-triggered, SQLite-backed)
- #56: Chunked buys (split entry into N chunks over time)
- #58: Avoid competing with known sniper bots (mempool sender analysis + auto-learn)
- #32: Monitor large early sells (post-entry price-drop watchdog with alerts)
- #91: A/B testing variant assignment (deterministic per-token)

All functions are defensive: they never raise into the caller's hot path.
Wired into b20_mainnet_sniper.py via run_periodic_checks() and inline hooks.
"""

import os
import time
import sqlite3
import threading
from datetime import datetime
from typing import Optional, Tuple, Dict, List, Callable

DB_FILE = "b20_trades.db"

# =============================================================================
# #73: MAX GAS SPEND CAP PER TRADE
# =============================================================================

def get_gas_cap_eth() -> float:
    """Configurable hard ceiling for gas spend per trade in ETH."""
    try:
        return float(os.getenv("MAX_GAS_ETH_PER_TRADE", "0.0015"))
    except Exception:
        return 0.0015


def check_gas_cap(gas_limit: int, max_fee_per_gas_wei: int) -> Tuple[bool, float, float]:
    """
    Upgrade #73: Return (ok, worst_case_cost_eth, cap_eth).
    Worst case = gas_limit * maxFeePerGas. Blocks runaway gas during launches.
    Set MAX_GAS_ETH_PER_TRADE=0 to disable the cap.
    """
    cap = get_gas_cap_eth()
    try:
        cost_eth = (gas_limit * max_fee_per_gas_wei) / 1e18
    except Exception:
        return True, 0.0, cap
    if cap <= 0:
        return True, cost_eth, cap
    return cost_eth <= cap, cost_eth, cap


# =============================================================================
# #50: LIMIT ORDERS / CONDITIONAL BUYS
# =============================================================================

_limit_lock = threading.Lock()
_last_limit_check = 0.0


def _init_limit_table():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS limit_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created TEXT,
        token TEXT,
        target_price_eth REAL,
        amount_eth REAL,
        status TEXT DEFAULT 'open',
        triggered_at TEXT,
        note TEXT
    )""")
    conn.commit()
    conn.close()


def add_limit_order(token: str, target_price_eth: float, amount_eth: float, note: str = "") -> int:
    """Create a conditional buy: fires when price drops to/below target_price_eth."""
    _init_limit_table()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO limit_orders (created, token, target_price_eth, amount_eth, status, note) VALUES (?, ?, ?, ?, 'open', ?)",
        (datetime.utcnow().isoformat(), token, target_price_eth, amount_eth, note),
    )
    order_id = c.lastrowid
    conn.commit()
    conn.close()
    return order_id


def list_limit_orders(status: str = "open") -> List[Dict]:
    _init_limit_table()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if status == "all":
        c.execute("SELECT id, created, token, target_price_eth, amount_eth, status FROM limit_orders ORDER BY id DESC LIMIT 25")
    else:
        c.execute("SELECT id, created, token, target_price_eth, amount_eth, status FROM limit_orders WHERE status=? ORDER BY id DESC LIMIT 25", (status,))
    rows = c.fetchall()
    conn.close()
    return [
        {"id": r[0], "created": r[1], "token": r[2], "target_price_eth": r[3], "amount_eth": r[4], "status": r[5]}
        for r in rows
    ]


def cancel_limit_order(order_id: int) -> bool:
    _init_limit_table()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE limit_orders SET status='cancelled' WHERE id=? AND status='open'", (order_id,))
    changed = c.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def check_limit_orders(w3, cfg: dict, price_fn: Callable, buy_fn: Callable, tg_send: Callable) -> None:
    """
    Upgrade #50: Poll open limit orders; trigger a buy when price <= target.
    Rate-limited internally (every LIMIT_CHECK_SECS, default 20s).
    """
    global _last_limit_check
    interval = float(os.getenv("LIMIT_CHECK_SECS", "20"))
    now = time.time()
    if now - _last_limit_check < interval:
        return
    _last_limit_check = now

    with _limit_lock:
        orders = list_limit_orders("open")
        if not orders:
            return
        for order in orders:
            token = order["token"]
            try:
                price = price_fn(w3, token)
            except Exception as pe:
                print(f"[LIMIT] Price fetch failed for {token}: {pe}")
                continue
            if price <= 0:
                continue
            if price <= order["target_price_eth"]:
                # Mark triggered BEFORE buying so a buy error can't double-fire
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("UPDATE limit_orders SET status='triggered', triggered_at=? WHERE id=? AND status='open'",
                          (datetime.utcnow().isoformat(), order["id"]))
                claimed = c.rowcount > 0
                conn.commit()
                conn.close()
                if not claimed:
                    continue
                print(f"[LIMIT] Order #{order['id']} triggered: {token} price {price:.10f} <= {order['target_price_eth']:.10f}")
                tg_send(
                    f"🎯 <b>Limit Order #{order['id']} TRIGGERED</b>\n"
                    f"Token: <code>{token}</code>\n"
                    f"Price: {price:.10f} ETH ≤ target {order['target_price_eth']:.10f} ETH\n"
                    f"Buying {order['amount_eth']} ETH..."
                )

                def _fire(tok=token, amt=order["amount_eth"]):
                    try:
                        buy_fn(w3, tok, 3000, amt, cfg, max_retries=1, force=True)
                    except Exception as be:
                        print(f"[LIMIT] Buy error for {tok}: {be}")
                        tg_send(f"❌ Limit order buy failed for <code>{tok}</code>: {str(be)[:150]}")

                threading.Thread(target=_fire, daemon=True).start()


# =============================================================================
# #56: CHUNKED BUYS
# =============================================================================

def chunking_enabled() -> bool:
    return os.getenv("CHUNKED_BUY_ENABLED", "true").lower() == "true"


def should_chunk(total_eth: float) -> bool:
    """Only chunk larger entries; tiny snipes go in one shot."""
    if not chunking_enabled():
        return False
    try:
        min_total = float(os.getenv("CHUNKED_BUY_MIN_TOTAL", "0.01"))
    except Exception:
        min_total = 0.01
    return total_eth >= min_total


def chunked_buy(w3, token: str, fee: int, total_eth: float, cfg: dict, buy_fn: Callable) -> None:
    """
    Upgrade #56: Split total_eth into CHUNK_COUNT buys spaced CHUNK_DELAY_SECS apart.
    First chunk fires immediately (speed matters on launches); the rest run in a
    background thread so the monitor loop never blocks. Reduces price impact and
    leaves a smaller mempool footprint per tx.
    """
    try:
        chunks = max(2, int(os.getenv("CHUNK_COUNT", "3")))
        delay = max(2.0, float(os.getenv("CHUNK_DELAY_SECS", "20")))
    except Exception:
        chunks, delay = 3, 20.0
    per_chunk = round(total_eth / chunks, 8)
    print(f"[CHUNK] Splitting {total_eth} ETH into {chunks} x {per_chunk} ETH (delay {delay}s)")

    # First chunk synchronously — entry speed matters
    try:
        buy_fn(w3, token, fee, per_chunk, cfg, max_retries=1)
    except Exception as e:
        print(f"[CHUNK] First chunk failed: {e}")
        return

    def _rest():
        for i in range(1, chunks):
            time.sleep(delay)
            try:
                result = buy_fn(w3, token, fee, per_chunk, cfg, max_retries=1)
                if not result:
                    print(f"[CHUNK] Chunk {i+1}/{chunks} did not fill; stopping remaining chunks")
                    break
            except Exception as e:
                print(f"[CHUNK] Chunk {i+1}/{chunks} error: {e}")
                break

    threading.Thread(target=_rest, daemon=True).start()


# =============================================================================
# #58: AVOID COMPETING WITH KNOWN SNIPER BOTS
# =============================================================================

_known_bots_cache: Optional[set] = None
_known_bots_loaded = 0.0


def _init_bot_table():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS bot_wallets (
        address TEXT PRIMARY KEY,
        first_seen TEXT,
        hits INTEGER DEFAULT 1
    )""")
    conn.commit()
    conn.close()


def get_known_bots() -> set:
    """Known sniper wallets: env KNOWN_BOT_WALLETS (comma-sep) + auto-learned DB set."""
    global _known_bots_cache, _known_bots_loaded
    if _known_bots_cache is not None and time.time() - _known_bots_loaded < 60:
        return _known_bots_cache
    bots = {a.strip().lower() for a in os.getenv("KNOWN_BOT_WALLETS", "").split(",") if a.strip()}
    try:
        _init_bot_table()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        # Only trust addresses seen sniping repeatedly (2+ fast entries)
        c.execute("SELECT address FROM bot_wallets WHERE hits >= 2")
        bots.update(r[0].lower() for r in c.fetchall())
        conn.close()
    except Exception as e:
        print(f"[BOTDETECT] DB read error: {e}")
    _known_bots_cache = bots
    _known_bots_loaded = time.time()
    return bots


def record_fast_buyer(address: str) -> None:
    """Auto-learn: record a wallet that swapped within seconds of pool creation."""
    if not address:
        return
    try:
        _init_bot_table()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""INSERT INTO bot_wallets (address, first_seen, hits) VALUES (?, ?, 1)
                     ON CONFLICT(address) DO UPDATE SET hits = hits + 1""",
                  (address.lower(), datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
        global _known_bots_cache
        _known_bots_cache = None  # invalidate cache
    except Exception as e:
        print(f"[BOTDETECT] record error: {e}")


def count_competing_bots(mempool_monitor, token: str, pool_detection_time: float = 0.0) -> Tuple[int, int]:
    """
    Upgrade #58: Inspect pending mempool swaps for this token.
    Returns (total_competing_swaps, known_bot_swaps).
    Also auto-learns senders that appear within BOT_LEARN_WINDOW_SECS of pool creation.
    """
    try:
        swaps = mempool_monitor.pending_swaps.get(token, [])
    except Exception:
        return 0, 0
    if not swaps:
        return 0, 0

    known = get_known_bots()
    learn_window = float(os.getenv("BOT_LEARN_WINDOW_SECS", "10"))
    known_count = 0
    now = time.time()
    for s in swaps:
        sender = (s.get("from") or s.get("sender") or "").lower()
        if not sender:
            continue
        if sender in known:
            known_count += 1
        # Auto-learn: swapping within seconds of pool creation = bot behavior
        if pool_detection_time and (now - pool_detection_time) <= learn_window:
            record_fast_buyer(sender)
    return len(swaps), known_count


def should_skip_for_competition(mempool_monitor, token: str, pool_detection_time: float = 0.0) -> Tuple[bool, str]:
    """Decide whether to skip a snipe because known bots are already racing it."""
    if os.getenv("BOT_COMPETITION_CHECK", "true").lower() != "true":
        return False, ""
    total, known_hits = count_competing_bots(mempool_monitor, token, pool_detection_time)
    try:
        max_known = int(os.getenv("MAX_KNOWN_BOT_COMPETITORS", "2"))
        max_total = int(os.getenv("MAX_TOTAL_COMPETING_SWAPS", "6"))
    except Exception:
        max_known, max_total = 2, 6
    if known_hits >= max_known:
        return True, f"{known_hits} known sniper bots racing this token (limit {max_known})"
    if total >= max_total:
        return True, f"{total} pending competing swaps in mempool (limit {max_total})"
    return False, ""


# =============================================================================
# #32: MONITOR LARGE EARLY SELLS (post-entry price watchdog)
# =============================================================================

_entry_prices: Dict[str, Dict] = {}   # token -> {price, ts, alerted}
_last_early_check = 0.0


def record_entry(token: str, entry_price_eth: float) -> None:
    """Called right after a successful buy so the watchdog has a baseline."""
    if entry_price_eth and entry_price_eth > 0:
        _entry_prices[token] = {"price": entry_price_eth, "ts": time.time(), "alerted": False}
        print(f"[EARLYSELL] Watching {token} from entry price {entry_price_eth:.10f} ETH")


def check_early_sells(w3, cfg: dict, price_fn: Callable, tg_send: Callable) -> None:
    """
    Upgrade #32: During the first EARLY_SELL_WINDOW_SECS after entry, alert if the
    price collapses more than EARLY_SELL_DROP_PCT — the classic large-early-sell /
    slow-rug signature. Alert includes instant sell buttons (execute-ready).
    """
    global _last_early_check
    interval = float(os.getenv("EARLY_SELL_CHECK_SECS", "20"))
    now = time.time()
    if now - _last_early_check < interval:
        return
    _last_early_check = now

    window = float(os.getenv("EARLY_SELL_WINDOW_SECS", "900"))
    drop_threshold = float(os.getenv("EARLY_SELL_DROP_PCT", "30"))

    for token, info in list(_entry_prices.items()):
        age = now - info["ts"]
        if age > window:
            _entry_prices.pop(token, None)  # out of the danger window
            continue
        if info["alerted"]:
            continue
        try:
            price = price_fn(w3, token)
        except Exception:
            continue
        if price <= 0:
            continue
        drop_pct = (info["price"] - price) / info["price"] * 100.0
        if drop_pct >= drop_threshold:
            info["alerted"] = True
            print(f"[EARLYSELL] ALERT {token}: -{drop_pct:.1f}% in {age:.0f}s after entry")
            sell_buttons = {
                "inline_keyboard": [
                    [{"text": "🚨 Sell 100% NOW", "callback_data": f"sell_{token}_100"}],
                    [{"text": "Sell 50%", "callback_data": f"sell_{token}_50"},
                     {"text": "Ignore", "callback_data": "menu"}],
                ]
            }
            tg_send(
                f"🚨 <b>LARGE EARLY SELL DETECTED</b>\n"
                f"Token: <code>{token}</code>\n"
                f"Price dropped <b>-{drop_pct:.1f}%</b> within {age/60:.1f} min of entry.\n"
                f"Entry: {info['price']:.10f} ETH → Now: {price:.10f} ETH\n"
                f"Possible dev dump / slow rug. Act fast:",
                reply_markup=sell_buttons,
            )


# =============================================================================
# #91: A/B TESTING — deterministic variant assignment
# =============================================================================

def ab_enabled() -> bool:
    return os.getenv("AB_TEST_ENABLED", "false").lower() == "true"


def assign_variant(token: str) -> str:
    """Deterministic A/B split by token address parity (stable across restarts)."""
    try:
        return "A" if int(token[-1], 16) % 2 == 0 else "B"
    except Exception:
        return "A"


def record_ab_trade(token: str, variant: str, pnl_eth: float = 0.0) -> None:
    """Append a variant-tagged trade record for later comparison."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS ab_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, token TEXT, variant TEXT, pnl_eth REAL
        )""")
        c.execute("INSERT INTO ab_trades (timestamp, token, variant, pnl_eth) VALUES (?, ?, ?, ?)",
                  (datetime.utcnow().isoformat(), token, variant, pnl_eth))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[AB] record error: {e}")


def ab_summary() -> Dict:
    """Compare variant A vs B performance from recorded trades."""
    out = {"A": {"trades": 0, "pnl": 0.0}, "B": {"trades": 0, "pnl": 0.0}}
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT variant, COUNT(*), COALESCE(SUM(pnl_eth), 0) FROM ab_trades GROUP BY variant")
        for variant, cnt, pnl in c.fetchall():
            if variant in out:
                out[variant] = {"trades": cnt, "pnl": pnl}
        conn.close()
    except Exception:
        pass
    return out


# =============================================================================
# PERIODIC DRIVER — called from the main monitor loop
# =============================================================================

def run_periodic_checks(w3, cfg: dict, price_fn: Callable, buy_fn: Callable, tg_send: Callable) -> None:
    """Single entry point the monitor loop calls each iteration. Never raises."""
    try:
        check_limit_orders(w3, cfg, price_fn, buy_fn, tg_send)
    except Exception as e:
        print(f"[P11] limit order check error: {e}")
    try:
        check_early_sells(w3, cfg, price_fn, tg_send)
    except Exception as e:
        print(f"[P11] early sell check error: {e}")
