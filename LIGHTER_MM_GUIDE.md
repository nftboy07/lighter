# 🚀 Lighter DEX High-Frequency Market Maker (MM) Bot

A quantitative, delta-neutral, volume-maximizing Market Making bot for **Lighter DEX** (zkLighter Central Limit Order Book on Ethereum/Arbitrum/ZK).

The bot is designed to farm the **Lighter × Robinhood Campaign** ($25M+ in rewards, $11M LIT community pool) by generating millions of dollars in maker volume while capturing the bid-ask spread and maintaining positive net PnL.

---

## 🌟 Key Features

1. **Avellaneda-Stoikov (2008) & Guéant-Tapia-Manziadi (GLFT) Quoting Engine**:
   - Computes continuous reservation (indifference) pricing: $r(s, q) = s - q \cdot \phi \cdot \sigma^2$
   - Real-time **Micro-Price** weighted Top-of-Book and **Order Flow Imbalance (OFI)** alpha
   - Asymmetric inventory skewing to passively shed accumulated positions without crossing spreads
   - Multi-layer grid tiers (1 to 5 layers) with geometric spread & size distribution

2. **High-Volume Campaign Farming Optimization**:
   - **Post-Only (ALO)** order enforcement guaranteeing 100% maker executions (zero taker fee drain)
   - Continuous high-throughput maker volume generation ($3M+ in 48 hours)
   - Real-time campaign reward points estimation tracking (e.g. ~12 points per $3M volume)

3. **Queue-Preserving Deadband OMS**:
   - Preserves FIFO order queue priority at Top-of-Book by suppressing unnecessary quote cancellations when price/size drift is within deadband tolerances ($\ge 1$ tick, $\ge 15\%$ size drift)

4. **Institutional Risk Controls & Circuit Breakers**:
   - **Pre-Trade Price Bands**: Rejects quotes exceeding $\pm 1.5\%$ deviation from mid price
   - **Hard & Soft Inventory Limits**: Stops quoting the accumulating side if delta reaches $Q_{max}$
   - **Volatility Spike Breaker**: Automatically pulls quotes if realized volatility surges $> 3\times$
   - **Daily Drawdown Auto-Pause**: Halts quoting if daily realized loss exceeds configured threshold
   - **WebSocket Deadman's Switch**: Cancels all live quotes if WebSocket feed drops for $> 3.0$ seconds
   - **File-based Kill Switch**: Instant emergency halt upon creating `lighter_kill_switch.flag`

5. **High-Fidelity Paper Trading Simulator & Live Execution**:
   - Seamless switching between **Paper Simulation** (`--paper` / `--dry-run`) and **Live Signed Trading** (`--live` with `lighter-sdk`)

6. **SQLite Analytics & Interactive Telegram Control Panel**:
   - High-performance SQLite database (`lighter_mm.db`) in WAL mode
   - Telegram bot with live status, PnL reports, volume tracking, and interactive inline buttons

---

## 📁 Architecture Overview

| Module | File | Purpose |
| :--- | :--- | :--- |
| **Orchestrator** | [`lighter_mm_bot.py`](file:///C:/Users/91907/Documents/antigravity/peaceful-bohr/lighter_mm_bot.py) | Master CLI entrypoint, WebSocket event loop, terminal dashboard |
| **Strategy Engine** | [`lighter_strategy.py`](file:///C:/Users/91907/Documents/antigravity/peaceful-bohr/lighter_strategy.py) | Avellaneda-Stoikov quoter, micro-price, OFI alpha, GLFT layers |
| **Execution & OMS** | [`lighter_execution.py`](file:///C:/Users/91907/Documents/antigravity/peaceful-bohr/lighter_execution.py) | Deadband OMS, Lighter SignerClient, WebSocket streamer, simulator |
| **Risk Manager** | [`lighter_risk_manager.py`](file:///C:/Users/91907/Documents/antigravity/peaceful-bohr/lighter_risk_manager.py) | Delta bounds, circuit breakers, price bands, deadman's switch |
| **Database** | [`lighter_db.py`](file:///C:/Users/91907/Documents/antigravity/peaceful-bohr/lighter_db.py) | SQLite fills, quotes, PnL snapshots, volume, points estimation |
| **Telegram Panel** | [`lighter_telegram.py`](file:///C:/Users/91907/Documents/antigravity/peaceful-bohr/lighter_telegram.py) | Interactive Telegram bot commands and outbound fill alerts |
| **Test Suite** | [`tests/test_lighter_mm.py`](file:///C:/Users/91907/Documents/antigravity/peaceful-bohr/tests/test_lighter_mm.py) | Automated pytest suite (11 unit/integration tests) |

---

## ⚙️ Installation & Configuration

### 1. Requirements
Ensure Python dependencies are installed:
```bash
pip install -r requirements.txt
pip install lighter-sdk
```

### 2. Configure Environment Variables
Copy `.env.lighter.example` to `.env`:
```bash
cp .env.lighter.example .env
```

Key configuration options in `.env`:
```ini
# --- Exchange & Network Settings ---
LIGHTER_BASE_URL=https://mainnet.zklighter.elliot.ai
LIGHTER_WS_URL=wss://mainnet.zklighter.elliot.ai/stream
MARKET_INDEX=0

# --- Authentication & Signer Credentials (for Live mode) ---
LIGHTER_ACCOUNT_INDEX=12345
LIGHTER_API_KEY_INDEX=2
LIGHTER_API_PRIVATE_KEY=0x...

# --- Safety & Strategy Defaults ---
DRY_RUN_DEFAULT=true
BASE_ORDER_SIZE=0.05
TARGET_SPREAD_BPS=2.0
NUM_LAYERS=3
MAX_INVENTORY=1.0
MAX_DAILY_LOSS_USD=100.0

# --- Telegram (Optional) ---
TELEGRAM_TOKEN=123456:ABC...
ADMIN_CHAT_ID=123456789
```

---

## 🚀 Running the Market Maker

### Mode A: Paper Trading Simulation (Default / Risk-Free)
Streams live real-time orderbooks and public trades from Lighter mainnet, quoting simulated orders and calculating fills/PnL in real time:
```bash
python lighter_mm_bot.py --paper --market 0 --layers 3 --size 0.05 --spread 2.0
```

### Mode B: Live Trading (Real Capital on zkLighter)
Places and cancels signed Post-Only limit orders directly on Lighter DEX:
```bash
python lighter_mm_bot.py --live --market 0 --layers 3 --size 0.05 --spread 2.0
```

### CLI Command Options
- `--paper` : Run in paper trading simulation mode (default)
- `--live` : Enable live signed transactions on zkLighter
- `--market <int>` : Market Index (e.g. `0` for ETH-USD perp)
- `--size <float>` : Base quote size in asset units (e.g. `0.05`)
- `--spread <float>` : Target half-spread in basis points (e.g. `2.0`)
- `--layers <int>` : Number of quoting grid tiers per side (e.g. `3`)
- `--no-telegram` : Disable Telegram bot integration

---

## 📱 Telegram Interactive Commands

| Command | Action |
| :--- | :--- |
| `/start` or `/menu` | Opens the interactive button control panel |
| `/status` | Shows live mid-price, spread, inventory, and circuit breaker state |
| `/volume` | Displays 24h & session maker volume and estimated campaign points |
| `/pnl` | Displays realized PnL, unrealized PnL, and total fills |
| `/inventory` | Displays current base delta vs soft & hard limits |
| `/spread <bps>` | Dynamically updates target spread (e.g. `/spread 2.5`) |
| `/size <units>` | Dynamically updates base order size (e.g. `/size 0.1`) |
| `/pause` | Pauses quoting and pulls all active orders |
| `/resume` | Resets circuit breaker and resumes quoting |
| `/cancelall` | Emergency instant cancellation of all active quotes |

---

## 🧪 Running Automated Tests

Run the full pytest suite:
```bash
pytest tests/test_lighter_mm.py -v
```
All 11 unit & integration tests validate strategy mathematics, inventory skewing, OFI alpha, deadband order diffs, risk circuit breakers, and SQLite points tracking.
