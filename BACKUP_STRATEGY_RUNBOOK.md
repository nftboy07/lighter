# 🏛️ zkLighter High-Frequency Trading Bot & Strategy Backup Runbook

> **Repository**: [https://github.com/nftboy07/lighter](https://github.com/nftboy07/lighter)  
> **Release Tag**: `v2.0-stable-production-backup`  
> **Target Account**: zkLighter Mainnet Sniper Subaccount `#737649`  
> **Live Server**: Windows VPS (`18.153.70.154`)

---

## 📋 1. Strategy Architecture Overview

Your bot is an institutional-grade, multi-strategy trading engine consisting of:

1. **Sub-15ms TreeNews & Multi-Source News Ingestion**:
   - Ingests from **610+ feeds** (TreeNews WebSocket, Binance Listings, Coinbase Roadmap, Upbit & Bithumb Korea, CryptoPanic, SEC EDGAR, Bloomberg, Reuters, Benzinga, DeFiLlama).
   - Instant single-source execution (`min_sources=1`) with 15-minute story fingerprint lockout preventing duplicate re-entries.
2. **Hyperliquid Cross-DEX Price-Lag Arbitrage**:
   - Compares live mark prices on Hyperliquid with zkLighter Level-2 Orderbooks to capture >= 25 bps price-lag lead opportunities.
3. **Hyperliquid Smart Money & Whale Radar**:
   - Zero-auth tape scanner tracking >= $250,000 sweeps by top leaderboard whales.
4. **0-Fee Avellaneda-Stoikov Maker Volume Farmer**:
   - Captures spread and farms Robinhood/zkLighter points during quiet hours, with instant <2ms catalyst cancellation.
5. **Scale-Out Take-Profit Ladder & Dynamic ATR Exits**:
   - Level 1: +2.5% profit -> Close 50% & shift Stop-Loss to Breakeven (+0.1%).
   - Level 2: +4.0% profit -> Close 25%.
   - Level 3: 25% Runner with dynamic 1.0%–2.0% trailing stop.
   - Hard Stop-Loss: -1.5% with on-chain Lighter GTT trigger orders.
6. **Decoupled Zero-Lag Telegram Bot & Mini-App Dashboard**:
   - Non-blocking notification dispatch queue (`queue.Queue(maxsize=500)`).
   - One-tap inline buttons (`[🔒 Breakeven SL]`, `[✂️ Close 50%]`, `[🎯 +2% TP]`, `[📈 Chart]`).
   - Poke AI webhook integration for multi-channel mobile notifications.
7. **4-Layer Indestructible 24/7 Auto-Revive**:
   - Windows Task Scheduler (`ONSTART`), 60s cron (`LighterRespawnGuard`), and Python Watchdog (`watchdog_supervisor.py`).

---

## 🔑 2. Subaccounts & Environment Configuration

Store these credentials in your `.env` or system environment:

```env
# zkLighter Mainnet Credentials
LIGHTER_BASE_URL=https://mainnet.zklighter.elliot.ai
WALLET_ADDRESS=0x5cE95F8F7594c082549B34A32c26f4bf2F1bcFe9
LIGHTER_ACCOUNT_INDEX=737649
LIGHTER_API_KEY_INDEX=5
LIGHTER_PUBLIC_KEY=76daf5b1c97a508a0ebb139910e9138c9ab9097f573b0dca13e7cb0598b9847a8d8f64a332039336
LIGHTER_PRIVATE_KEY=d8f911f20a7a283983e7b2963eb5072a88d8df23e7b36b297da56c767d0c3f20a3c9849c9657ee6a

# Telegram Bot Credentials
TELEGRAM_BOT_TOKEN=8708988669:AAEJYyUocg0ytkUMtGxMK3Ba3HcvTEPuL08
ADMIN_CHAT_ID=1267102944
TELEGRAM_CHAT_ID=1267102944
TELEGRAM_NEWS_BROADCAST=false

# Poke AI Webhook
POKE_WEBHOOK_URL=https://poke.com/api/v1/inbound/api-message
POKE_API_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJiYTI1NWE3MS1hM2Q1LTQ3YWMtOTFmNi05YjkzZjMwN2JlYjAiLCJqdGkiOiJkYjRkNTliMS00ZWE4LTQ0MjQtYTViYi1mMWFiMTZhODNjNWIiLCJpYXQiOjE3ODczNzk3MDIsImV4cCI6MjEwMjczOTcwMn0.BKasiODc-jsUjSWpC9iiJLtkGu856dqLqj_gklrHbic

# Strategy Parameters
NEWS_MIN_SOURCES=1
CROSS_DEX_ARB_ENABLED=true
HL_WHALES_ENABLED=true
TREENEWS_WS_ENABLED=true
DAILY_BRIEFING_HOUR_UTC=8
```

---

## 🚀 3. How to Restore & Run on Any Machine

### Step 1: Clone Repository
```bash
git clone https://github.com/nftboy07/lighter.git
cd lighter
```

### Step 2: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 3: Run Full Test Suite (Verify 100% Pass)
```bash
pytest tests/
```

### Step 4: Launch 24/7 Production Engine
```bash
# Launch Guardian Supervisor (which manages and revives the trading engine)
python watchdog_supervisor.py
```
