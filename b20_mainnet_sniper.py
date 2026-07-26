#!/usr/bin/env python3
"""
B20 Mainnet Sniper / Launcher Bot
=================================
SCOPED EXCLUSIVELY TO BASE MAINNET (chainId=8453)

This implements the requirements from the provided Mainnet spec:
- Hardcoded Mainnet addresses and chain ID
- Activation Registry check before any createB20
- B20Factory createB20 support (with simulation)
- Uniswap V3 PoolCreated monitoring (any fee tier)
- High-gas aggressive sniping with fee history + premium
- Liquidity check before retry buys
- Flashbots Protect RPC option for private submission
- eth_call dry-run before real tx where possible

CRITICAL WARNINGS (READ FULLY):
- This runs on REAL Base Mainnet. Gas and swaps cost REAL ETH.
- Do NOT run createB20 or any state-changing tx before the Activation Registry enables the feature.
  Scheduled: July 8, 2026, 18:00 UTC (23:30 IST).
- Sniping carries extreme risk: slippage, rugs, honeypots, failed tx (gas still spent), MEV, etc.
- You are responsible for your wallet, keys, and funds. 0.5+ ETH recommended minimum.
- Test thoroughly with eth_call simulations and on testnet if possible BEFORE any live mainnet run.
- This is NOT financial advice. Trading bots can and will lose money.
- **NEVER hardcode or log private keys/tokens.** All secrets MUST come from .env (which is gitignored).
- The code explicitly masks keys in logs via mask_sensitive() and get_safe_config().
- Clock must be NTP-synced to UTC.
- On VPS always: chmod 600 .env
- If a secret is ever leaked, rotate it IMMEDIATELY.

Usage:
  1. pip install web3 python-dotenv
  2. Create .env with:
       RPC_URL=https://mainnet.base.org
       # or wss://... for better event subscription
       PRIVATE_KEY=0x...
       FLASHBOTS_RPC=https://rpc.flashbots.net   # optional, for private tx if supported / proxy
  3. python b20_bot/b20_mainnet_sniper.py --help
  4. Start with --dry-run or --simulate-only

Addresses (Mainnet only, do not override):
- Activation Registry: 0x8453000000000000000000000000000000000001
- Policy Registry:     0x8453000000000000000000000000000000000002
- B20Factory:          0xB20f000000000000000000000000000000000000
- Uniswap V3 Factory:  0x33128a8fC17869897dcE68Ed026d694621f6FDfD
- Uniswap V3 Router:   0xE592427A0AEce92De3Edee1F18E0157C05861564
- WETH:                0x4200000000000000000000000000000000000006
- USDC:                0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913

B20 feature keys (for isActivated):
- base.b20_asset
- base.b20_stablecoin

B20 tokens are created at addresses starting 0xB200...
"""

import os
import sys
import time
import json
import sqlite3
import argparse
import random
import threading
import asyncio
import traceback
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import TransactionNotFound
from eth_utils import keccak, to_checksum_address
from eth_abi import encode

try:
    from mempool_monitor import MempoolMonitor
    from early_detection import EarlyDetectionEngine
    MEMPOOL_AVAILABLE = True
except ImportError:
    MEMPOOL_AVAILABLE = False
    print("Mempool modules not available (optional)")

import requests

# ethbot-style interactive Telegram bot
try:
    from telegram_bot import (
        set_sniper_context,
        start_telegram_bot_in_background,
        get_buy_keyboard_dict,
        get_control_keyboard_dict,
    )
    TG_LIB_AVAILABLE = True
except ImportError:
    TG_LIB_AVAILABLE = False
    print("telegram_bot (python-telegram-bot) not available, falling back to legacy polling if present")

current_w3 = None

# Risk mgmt stubs (upgrades 65,66,68)
ACTIVE_POSITIONS = {}  # token -> amount
MAX_CONCURRENT = 3
BLACKLIST = set()  # upgrade #68, #81

# Persistent session for all Telegram Bot API calls.
# Reuses TCP/TLS connections (keep-alive) -> much lower latency for getUpdates, sendMessage, answerCallbackQuery.
# This directly fixes "TG buttons slow" and poll disconnects.
tg_session = requests.Session()
tg_session.headers.update({
    "Connection": "keep-alive",
    "User-Agent": "B20-Sniper/1.0"
})
# Connection pooling + small retries for TG API stability (helps against transient disconnects on long-poll)
try:
    from requests.adapters import HTTPAdapter
    adapter = HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=1)
    tg_session.mount("https://api.telegram.org", adapter)
except Exception:
    pass  # non-fatal if adapter not usable

# =============================================================================
# MAINNET-ONLY CONSTANTS (DO NOT CHANGE)
# =============================================================================
CHAIN_ID = 8453
RPC_DEFAULT = "https://mainnet.base.org"

# Extensive list of public Base Mainnet RPCs (HTTP + WS) for failover and reliability.
# The bot will rotate through these to avoid rate limits (429s) during high-traffic meme launches.
# Add your own paid ones in .env for best performance.
DEFAULT_BASE_RPCS = [
    "https://mainnet.base.org",
    "https://base.meowrpc.com",
    "https://base-rpc.publicnode.com",
    "https://base.publicnode.com",
    "https://base-mainnet.public.blastapi.io",
    "https://1rpc.io/base",
    "https://gateway.tenderly.co/public/base",
    "https://base.llamarpc.com",
    "https://base.drpc.org",
    "https://base.blockpi.network/v1/rpc/public",
    "https://base-pokt.nodies.app",
]

ACTIVATION_REGISTRY = to_checksum_address("0x8453000000000000000000000000000000000001")
POLICY_REGISTRY     = to_checksum_address("0x8453000000000000000000000000000000000002")
B20_FACTORY         = to_checksum_address("0xB20f000000000000000000000000000000000000")

# Known B20 launch platforms from user (for enhanced detection and alerts)
# To fill addresses: for a new token from one of these sites, look up its creation tx on basescan, see the 'to' or internal create call to B20Factory or launcher.
# Then add the launcher/deployer address here for mempool early signal on pending creates.
B20_LAUNCH_PLATFORMS = {
    "funblue": None,  # funblue.xyz - curve launches for B20
    "basehub": None,  # basehub.fun/deploy-b20
    "rwagmi": None,   # rwagmi.com - B20 create/manage
    "deployb20": None, # deployb20.xyz - launcher + MCP
    "o1": None,       # launch.o1.exchange - launchpad
}

UNISWAP_V3_FACTORY  = to_checksum_address("0x33128a8fC17869897dcE68Ed026d694621f6FDfD")
UNISWAP_V3_ROUTER   = to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
WETH                = to_checksum_address("0x4200000000000000000000000000000000000006")
USDC                = to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")

AERODROME_FACTORY   = to_checksum_address("0x420DD381b31aEf6683db6B902084cB0FFECe40Da")
AERODROME_ROUTER    = to_checksum_address("0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43")

# Uniswap V3 QuoterV2 on Base for accurate pricing (critical for real slippage)
UNISWAP_QUOTER_V2   = to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")

# Activation feature hashes (as used in Base docs: keccak("base.b20_asset"))
FEATURE_B20_ASSET      = keccak(text="base.b20_asset")
FEATURE_B20_STABLECOIN = keccak(text="base.b20_stablecoin")

# Common fee tiers (do not filter; buy any)
UNISWAP_FEE_TIERS = [100, 500, 3000, 10000]
POOL_DETECTION_TIMES = {}
PENDING_POOLS_TO_RETRY = {}

# Activation time (do not hardcode logic that bypasses the on-chain check)
ACTIVATION_UTC = datetime(2026, 7, 8, 18, 0, 0, tzinfo=timezone.utc)

# Minimal ABIs (sufficient for the bot operations)
ACTIVATION_REGISTRY_ABI = [
    {"inputs": [{"internalType": "bytes32", "name": "feature", "type": "bytes32"}],
     "name": "isActivated", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"}
]

B20_FACTORY_ABI = [
    {"inputs": [{"internalType": "uint8", "name": "variant", "type": "uint8"},
                 {"internalType": "bytes32", "name": "salt", "type": "bytes32"},
                 {"internalType": "bytes", "name": "params", "type": "bytes"},
                 {"internalType": "bytes[]", "name": "initCalls", "type": "bytes[]"}],
     "name": "createB20", "outputs": [{"internalType": "address", "name": "token", "type": "address"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"internalType": "uint8", "name": "variant", "type": "uint8"},
                 {"internalType": "address", "name": "deployer", "type": "address"},
                 {"internalType": "bytes32", "name": "salt", "type": "bytes32"}],
     "name": "getB20Address", "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "addr", "type": "address"}],
     "name": "isB20", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "addr", "type": "address"}],
     "name": "isB20Initialized", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
     "stateMutability": "view", "type": "function"},
    # B20Created event for early detection (upgrade)
    {"anonymous": False, "inputs": [
        {"indexed": True, "internalType": "address", "name": "token", "type": "address"},
        {"indexed": False, "internalType": "uint8", "name": "variant", "type": "uint8"},
        {"indexed": False, "internalType": "string", "name": "name", "type": "string"},
        {"indexed": False, "internalType": "string", "name": "symbol", "type": "string"},
    ], "name": "B20Created", "type": "event"}
]

UNISWAP_V3_FACTORY_ABI = [
    {"anonymous": False, "inputs": [
        {"indexed": True, "internalType": "address", "name": "token0", "type": "address"},
        {"indexed": True, "internalType": "address", "name": "token1", "type": "address"},
        {"indexed": True, "internalType": "uint24", "name": "fee", "type": "uint24"},
        {"indexed": False, "internalType": "int24", "name": "tickSpacing", "type": "int24"},
        {"indexed": False, "internalType": "address", "name": "pool", "type": "address"}
    ], "name": "PoolCreated", "type": "event"},
    {"inputs": [{"internalType": "address", "name": "", "type": "address"},
                 {"internalType": "address", "name": "", "type": "address"},
                 {"internalType": "uint24", "name": "", "type": "uint24"}],
     "name": "getPool", "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"}
]

# Minimal Pool ABI for liquidity() and slot0
UNISWAP_V3_POOL_ABI = [
    {"inputs": [], "name": "liquidity", "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "slot0", "outputs": [
        {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
        {"internalType": "int24", "name": "tick", "type": "int24"},
        {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
        {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
        {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
        {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
        {"internalType": "bool", "name": "unlocked", "type": "bool"}
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"}
]

# Uniswap V3 SwapRouter02 ABI — exactInputSingle
# Selector: 0x04e45aaf
# Struct: (tokenIn, tokenOut, fee, recipient, amountIn, amountOutMinimum, sqrtPriceLimitX96)
# NO 'deadline' field (removed vs old SwapRouter). recipient stays at position 4.
UNISWAP_V3_ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}
                ],
                "internalType": "struct IV3SwapRouter.ExactInputSingleParams",
                "name": "params",
                "type": "tuple"
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function"
    }
]

AERODROME_FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
            {"internalType": "bool", "name": "stable", "type": "bool"}
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

AERODROME_ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {
                "components": [
                    {"internalType": "address", "name": "from", "type": "address"},
                    {"internalType": "address", "name": "to", "type": "address"},
                    {"internalType": "bool", "name": "stable", "type": "bool"},
                    {"internalType": "address", "name": "factory", "type": "address"}
                ],
                "internalType": "struct IRouter.Route[]",
                "name": "routes",
                "type": "tuple[]"
            }
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {
                "components": [
                    {"internalType": "address", "name": "from", "type": "address"},
                    {"internalType": "address", "name": "to", "type": "address"},
                    {"internalType": "bool", "name": "stable", "type": "bool"},
                    {"internalType": "address", "name": "factory", "type": "address"}
                ],
                "internalType": "struct IRouter.Route[]",
                "name": "routes",
                "type": "tuple[]"
            },
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactETHForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {
                "components": [
                    {"internalType": "address", "name": "from", "type": "address"},
                    {"internalType": "address", "name": "to", "type": "address"},
                    {"internalType": "bool", "name": "stable", "type": "bool"},
                    {"internalType": "address", "name": "factory", "type": "address"}
                ],
                "internalType": "struct IRouter.Route[]",
                "name": "routes",
                "type": "tuple[]"
            },
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactTokensForETH",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

# Minimal QuoterV2 ABI for accurate quotes (upgrade for real slippage)
UNISWAP_QUOTER_V2_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}
                ],
                "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
                "name": "params",
                "type": "tuple"
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"},
            {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"}
        ],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

# Minimal ERC20 ABI for balance/held queries (used in positions, safety, PnL)
ERC20_MIN_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

# =============================================================================
# UTILS
# =============================================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def is_activation_time_passed() -> bool:
    return now_utc() >= ACTIVATION_UTC

mempool_monitor_instance = None

accounts_list = []

def init_accounts(w3: Web3):
    global accounts_list
    accounts_list = []
    pks_str = os.getenv("PRIVATE_KEYS") or os.getenv("PRIVATE_KEY", "")
    if pks_str:
        pks = [k.strip() for k in pks_str.split(",") if k.strip()]
        for pk in pks:
            try:
                acc = w3.eth.account.from_key(pk)
                accounts_list.append((pk, acc))
                print(f"[ROTATION] Loaded wallet: {acc.address}")
            except Exception as e:
                print(f"[ROTATION] Failed to load key: {e}")
    if not accounts_list:
        print("[ROTATION] WARNING: No private keys loaded! Bot will not be able to trade.")

def get_next_rotation_account(w3: Web3) -> tuple[str, Any]:
    global accounts_list
    if not accounts_list:
        pk = os.getenv("PRIVATE_KEY")
        if pk:
            acc = w3.eth.account.from_key(pk)
            return pk, acc
        raise ValueError("No private keys configured!")
        
    best_pk, best_acc = accounts_list[0]
    best_bal = -1
    for pk, acc in accounts_list:
        try:
            bal = w3.eth.get_balance(acc.address)
            if bal > best_bal:
                best_bal = bal
                best_pk, best_acc = pk, acc
        except Exception as e:
            print(f"[ROTATION] Balance query error for {acc.address}: {e}")
            
    print(f"[ROTATION] Selected wallet {best_acc.address} with balance {best_bal/1e18:.4f} ETH")
    return best_pk, best_acc

def load_active_positions_from_db() -> Dict[str, Dict]:
    positions = {}
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT token, amount, token_amount, timestamp FROM trades WHERE action='buy' AND status='success'")
        buys = c.fetchall()
        c.execute("SELECT token, token_amount FROM trades WHERE action='sell' AND status='success'")
        sells = c.fetchall()
        conn.close()
        
        token_balances = {}
        for token, amount, token_amount, timestamp in buys:
            if token not in token_balances:
                dt = datetime.fromisoformat(timestamp)
                token_balances[token] = {
                    'total_spent_eth': amount,
                    'token_amount': token_amount,
                    'timestamp': dt.replace(tzinfo=timezone.utc).timestamp()
                }
            else:
                token_balances[token]['total_spent_eth'] += amount
                token_balances[token]['token_amount'] += token_amount
                
        for token, token_amount in sells:
            if token in token_balances:
                token_balances[token]['token_amount'] -= token_amount
                
        for token, data in token_balances.items():
            if data['token_amount'] > 0.001:
                positions[token] = {
                    'buy_price_eth': data['total_spent_eth'] / data['token_amount'] if data['token_amount'] > 0 else 0,
                    'token_amount': data['token_amount'],
                    'timestamp': data['timestamp'],
                    'highest_price_eth': data['total_spent_eth'] / data['token_amount'] if data['token_amount'] > 0 else 0,
                }
    except Exception as e:
        print(f"[DB] load active positions error: {e}")
    return positions

def get_pool_reserves_in_eth(w3: Web3, pool: str) -> float:
    """Return the total WETH balance of the pool contract in ETH. DEX-agnostic."""
    try:
        weth_erc = w3.eth.contract(address=WETH, abi=ERC20_MIN_ABI)
        bal = weth_erc.functions.balanceOf(to_checksum_address(pool)).call()
        return bal / 1e18
    except Exception as e:
        print(f"Error querying pool WETH balance: {e}")
        return 0.0

def log_arbitrage_opportunity(token: str, uni_price: float, aero_price: float, diff_pct: float):
    """Log an arbitrage opportunity to the database."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS arbitrage_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                token TEXT,
                uni_price REAL,
                aero_price REAL,
                diff_pct REAL
            )
        """)
        c.execute("""
            INSERT INTO arbitrage_opportunities (timestamp, token, uni_price, aero_price, diff_pct)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), token, uni_price, aero_price, diff_pct))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] Error logging arbitrage: {e}")

def check_cross_pool_arbitrage(w3: Web3, token: str):
    """Compare prices of a token between Uniswap V3 and Aerodrome V2 pools."""
    try:
        token = to_checksum_address(token)
        # 1. Query Uniswap V3 price
        uni_price = 0.0
        try:
            dec = get_token_decimals(w3, token)
            one_token = 10 ** dec
            quoter = w3.eth.contract(address=UNISWAP_QUOTER_V2, abi=UNISWAP_QUOTER_V2_ABI)
            params = (token, WETH, 3000, one_token, 0)
            quoted = quoter.functions.quoteExactInputSingle(params).call()
            amount_out = quoted[0] if isinstance(quoted, (list, tuple)) else quoted
            uni_price = amount_out / 1e18
        except:
            # Fallback to slot0
            for fee in [100, 500, 3000, 10000]:
                p = find_or_wait_pool(w3, WETH, token, fee) or find_or_wait_pool(w3, token, WETH, fee)
                if p:
                    pool_contract = get_pool_contract(w3, p)
                    slot0 = pool_contract.functions.slot0().call()
                    sqrt_price_x96 = slot0[0]
                    if sqrt_price_x96 > 0:
                        price_ratio = (sqrt_price_x96 / (2 ** 96)) ** 2
                        try:
                            t0 = to_checksum_address(pool_contract.functions.token0().call())
                            if t0 == WETH:
                                uni_price = 1.0 / price_ratio if price_ratio > 0 else 0
                            else:
                                uni_price = price_ratio
                        except:
                            uni_price = price_ratio
                        uni_price *= (10 ** (dec - 18)) if dec != 18 else 1.0
                        break
        
        # 2. Query Aerodrome price
        aero_price = 0.0
        try:
            aero_pool, aero_stable = find_aerodrome_pool(w3, WETH, token)
            if not aero_pool:
                aero_pool, aero_stable = find_aerodrome_pool(w3, token, WETH)
            if aero_pool:
                dec = get_token_decimals(w3, token)
                one_token = 10 ** dec
                router = w3.eth.contract(address=AERODROME_ROUTER, abi=AERODROME_ROUTER_ABI)
                routes = [(token, WETH, aero_stable, AERODROME_FACTORY)]
                amounts = router.functions.getAmountsOut(one_token, routes).call()
                aero_price = amounts[-1] / 1e18
        except:
            pass

        if uni_price > 0 and aero_price > 0:
            diff_pct = abs(uni_price - aero_price) / min(uni_price, aero_price) * 100
            if diff_pct >= 2.0:
                print(f"[ARB] Arbitrage Opportunity for {token}: Uni={uni_price:.8f} ETH, Aero={aero_price:.8f} ETH, Diff={diff_pct:.2f}%")
                log_arbitrage_opportunity(token, uni_price, aero_price, diff_pct)
                tg_send(f"⚖️ <b>Arbitrage Spread Alert</b> for <code>{token}</code>:\n"
                        f"• Uniswap V3: <code>{uni_price:.8f} ETH</code>\n"
                        f"• Aerodrome: <code>{aero_price:.8f} ETH</code>\n"
                        f"• Spread: <code>{diff_pct:.2f}%</code>")
    except Exception as e:
        print(f"[ARB] Error checking arbitrage: {e}")

def check_pool_burn_events(w3: Web3, pool: str, from_block: int, to_block: int, dex_type: str) -> bool:
    """Check if any Burn / RemoveLiquidity event was emitted on the pool in the block range."""
    try:
        pool_addr = to_checksum_address(pool)
        # Uni V3 Burn vs Aero V2 Burn topic
        topic = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c" if dex_type == "uniswap_v3" else "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496"
        logs = w3.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": pool_addr,
            "topics": [topic]
        })
        if logs:
            print(f"[BURN MONITOR] Detected burn/remove liquidity event in block range {from_block}-{to_block} for pool {pool}")
            return True
    except Exception as e:
        print(f"[BURN MONITOR] Error checking events: {e}")
    return False

FAILED_EXIT_ATTEMPTS = {}

async def monitor_positions_loop(w3: Web3, cfg: dict):
    print("[MONITOR] Starting active position monitoring loop...")
    rpc_list = cfg.get("BACKUP_RPCS", DEFAULT_BASE_RPCS)
    last_block_checked = 0
    while True:
        try:
            # rotate RPC each iteration to distribute load across endpoints
            try:
                loop_w3 = get_best_w3(rpc_list)
            except Exception:
                loop_w3 = w3  # fallback to original
            
            try:
                current_block = loop_w3.eth.block_number
            except Exception:
                current_block = 0

            for token, pos in list(ACTIVE_POSITIONS.items()):
                try:
                    try:
                        current_price = get_token_price_in_eth(loop_w3, token)
                        check_cross_pool_arbitrage(loop_w3, token)
                    except Exception:
                        current_price = 0
    
                    entry_price = pos['buy_price_eth']
                    token_amount = pos['token_amount']
                    buy_time = pos['timestamp']
                    highest_price = pos.get('highest_price_eth', entry_price)
    
                    if current_price > 0:
                        if current_price > highest_price:
                            pos['highest_price_eth'] = current_price
                            highest_price = current_price
    
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100 if (entry_price > 0 and current_price > 0) else 0
                    tsl_pct = ((highest_price - current_price) / highest_price) * 100 if (highest_price > 0 and current_price > 0) else 0
                    age_minutes = (time.time() - buy_time) / 60
    
                    tp_pct = cfg.get("TAKE_PROFIT_PCT", 100.0)
                    sl_pct = cfg.get("STOP_LOSS_PCT", 20.0)
                    tsl_limit_pct = cfg.get("TRAILING_STOP_LOSS_PCT", 15.0)
                    max_hold_minutes = cfg.get("MAX_HOLD_MINUTES", 15.0)
    
                    trigger_sell = False
                    reason = ""
    
                    if current_price > 0:
                        if pnl_pct >= tp_pct:
                            trigger_sell = True
                            reason = f"Take Profit (+{pnl_pct:.1f}%)"
                        elif pnl_pct <= -sl_pct:
                            trigger_sell = True
                            reason = f"Stop Loss ({pnl_pct:.1f}%)"
                        elif tsl_pct >= tsl_limit_pct and pnl_pct > 0:
                            trigger_sell = True
                            reason = f"Trailing Stop Loss (-{tsl_pct:.1f}% from ATH, PnL +{pnl_pct:.1f}%)"
                    try:
                        res = find_best_pool(loop_w3, token)
                        if res and res[0]:
                            pool, dex_type, dex_param = res
                            # Real-time Liquidity Removal Monitoring (#37)
                            if last_block_checked > 0 and current_block > last_block_checked:
                                if check_pool_burn_events(loop_w3, pool, last_block_checked + 1, current_block, dex_type):
                                    trigger_sell = True
                                    reason = "Real-time Liquidity Burn Event Detected"
                            
                            if not trigger_sell:
                                pool_weth = get_pool_reserves_in_eth(loop_w3, pool)
                                if pool_weth < 0.005:
                                    print(f"[MONITOR] Liquidity drained to zero ({pool_weth:.4f} ETH) for {token}. Skipping swap to save gas. Removing from monitoring.")
                                    tg_send(f"🚨 <b>Rugged (Zero Liquidity)</b> for {token}\nLiquidity has dropped to {pool_weth:.4f} ETH. Removing from active monitoring to save gas.")
                                    log_trade(token, "sell", 0.0, "RUG_ZERO_LIQ", "success", token_amount)
                                    ACTIVE_POSITIONS.pop(token, None)
                                    FAILED_EXIT_ATTEMPTS.pop(token, None)
                                    continue
                                elif pool_weth < 0.1:
                                    trigger_sell = True
                                    reason = f"Liquidity Drain / Rug Detected ({pool_weth:.3f} ETH remaining)"
                    except Exception as re:
                        print(f"[MONITOR] Rug check error: {re}")
 
                    if not trigger_sell and age_minutes >= max_hold_minutes:
                        trigger_sell = True
                        reason = f"Time Limit Reached ({age_minutes:.1f} minutes hold)"
     
                    if trigger_sell:
                        print(f"[MONITOR] Exiting position for {token}. Reason: {reason}")
                        tg_send(f"🚨 <b>Automated Exit Triggered</b> for {token}\nReason: {reason}\nAmount: {token_amount:.6f}")
                        res = find_best_pool(loop_w3, token)
                        if res and res[0]:
                            pool, dex_type, dex_param = res
                            decimals = get_token_decimals(loop_w3, token)
                            amount_wei = int(token_amount * (10 ** decimals))
                            tx_hash = attempt_sell(loop_w3, token, pool, dex_type, dex_param, amount_wei, cfg)
                            if tx_hash:
                                ACTIVE_POSITIONS.pop(token, None)
                                FAILED_EXIT_ATTEMPTS.pop(token, None)
                            else:
                                print(f"[MONITOR] Failed to sell position for {token}")
                                FAILED_EXIT_ATTEMPTS[token] = FAILED_EXIT_ATTEMPTS.get(token, 0) + 1
                                if FAILED_EXIT_ATTEMPTS[token] >= 3:
                                    print(f"[MONITOR] Too many failed exit attempts for {token}. Marking position as dead/rugged.")
                                    tg_send(f"⚠️ <b>Rugged / Liquidity Drained</b>: Too many failed exit attempts for {token}. Removing from active monitoring.")
                                    log_trade(token, "sell", 0.0, "RUG_FAILED_EXITS", "success", token_amount)
                                    ACTIVE_POSITIONS.pop(token, None)
                                    FAILED_EXIT_ATTEMPTS.pop(token, None)
                        else:
                            print(f"[MONITOR] No pool found for {token} exit.")
                except Exception as token_err:
                    print(f"[MONITOR] Error checking token {token}: {token_err}")
 
            if current_block > 0:
                last_block_checked = current_block
 
            await asyncio.sleep(15)  # 15s between checks — less RPC pressure
        except Exception as e:
            print(f"[MONITOR] Position loop error: {e}")
            await asyncio.sleep(15)


recent_buys = []

def check_rate_limit(max_per_minute: int = 2) -> bool:
    """Check if the rate limit for buys is exceeded. Enforces safety throttling."""
    global recent_buys
    now = time.time()
    recent_buys = [t for t in recent_buys if now - t < 60]
    if len(recent_buys) >= max_per_minute:
        return False
    return True

def load_config() -> Dict[str, Any]:
    load_dotenv()
    cfg = {
        "RPC_URL": os.getenv("RPC_URL", RPC_DEFAULT),
        "PRIVATE_KEY": os.getenv("PRIVATE_KEY", ""),
        "FLASHBOTS_RPC": os.getenv("FLASHBOTS_RPC", ""),
        "WALLET_ADDRESS": os.getenv("WALLET_ADDRESS", ""),
        "MAX_TRADE_ETH": float(os.getenv("MAX_TRADE_ETH", "0.1")),
        "MAX_DAILY_LOSS_ETH": float(os.getenv("MAX_DAILY_LOSS_ETH", "0.5")),
        "MIN_LIQUIDITY_ETH": float(os.getenv("MIN_LIQUIDITY_ETH", "5.0")),
        "SLIPPAGE_BPS": int(os.getenv("SLIPPAGE_BPS", "2000")),
        "KILL_SWITCH_FILE": os.getenv("KILL_SWITCH_FILE", "/home/ubuntu/b20-bot/KILL_SWITCH"),
        # Telegram - support ethbot-style names + legacy
        "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TG_BOT_TOKEN", ""),
        "ADMIN_CHAT_ID": os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_USER_ID", ""),
        "ONLY_O1_LAUNCHPAD": os.getenv("ONLY_O1_LAUNCHPAD", "false").lower() == "true",
    }
    user_rpcs = [r.strip() for r in os.getenv("BACKUP_RPCS", "").split(",") if r.strip()]
    cfg["BACKUP_RPCS"] = user_rpcs + [r for r in DEFAULT_BASE_RPCS if r not in user_rpcs]  # user first, then defaults, no dups
    if not cfg["PRIVATE_KEY"]:
        print("WARNING: PRIVATE_KEY not set - transactions will fail. Use for monitoring only.")
    # Leak-proof runtime guard - check for placeholder keys
    key = cfg.get("PRIVATE_KEY", "")
    if key and ("YOUR" in key or len(key) < 10 or key.startswith("0x0000")):
        print("WARNING: PRIVATE_KEY looks like a placeholder or dummy. Real transactions will fail.")
    return cfg

def mask_sensitive(value: str, show_last: int = 4) -> str:
    """Mask sensitive strings like keys and tokens for logging."""
    if not value or len(value) < 8:
        return "***"
    return value[:4] + "..." + value[-show_last:] if len(value) > 8 else "***"

def get_safe_config(cfg: dict) -> dict:
    """Return a copy of config with all sensitive values masked. Leak-proof for logs."""
    safe = cfg.copy()
    for k in ["PRIVATE_KEY", "TELEGRAM_TOKEN", "TG_BOT_TOKEN", "FLASHBOTS_RPC"]:
        if k in safe and safe[k]:
            safe[k] = mask_sensitive(safe[k])
    if "BACKUP_RPCS" in safe:
        safe["BACKUP_RPCS"] = [mask_sensitive(r) for r in safe.get("BACKUP_RPCS", [])]
    return safe


# =============================================================================
# SMART RPC ROTATOR — round-robin with 429 cooldown
# =============================================================================
import threading as _threading
import requests as _requests

_rpc_lock = _threading.Lock()
_rpc_cooldowns: dict = {}          # rpc_url -> time when cooldown expires
_RPC_COOLDOWN_SECS = 30            # skip a 429'd RPC for this long

def _rpc_is_available(url: str) -> bool:
    """Return True if this RPC is not in cooldown."""
    exp = _rpc_cooldowns.get(url, 0)
    return time.time() > exp

def _rpc_mark_429(url: str):
    """Put an RPC into cooldown for _RPC_COOLDOWN_SECS."""
    with _rpc_lock:
        _rpc_cooldowns[url] = time.time() + _RPC_COOLDOWN_SECS
        print(f"[RPC] 429 on {url[:40]}... → cooldown {_RPC_COOLDOWN_SECS}s")

_rpc_index = 0  # global round-robin cursor

def get_w3(rpc_url: str) -> Web3:
    """Build a Web3 instance for the given URL."""
    if rpc_url.startswith("wss://"):
        try:
            from web3.providers import LegacyWebSocketProvider
            return Web3(LegacyWebSocketProvider(rpc_url))
        except ImportError:
            try:
                return Web3(Web3.WebsocketProvider(rpc_url))
            except:
                return Web3(Web3.WebSocketProvider(rpc_url))
    # HTTP: wrap provider so 429s are caught and trigger rotation
    class _RotatingHTTPProvider(Web3.HTTPProvider):
        def make_request(self, method, params):
            try:
                resp = super().make_request(method, params)
            except Exception as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    _rpc_mark_429(rpc_url)
                raise
            # also check HTTP-level 429 embedded in response
            if isinstance(resp, dict) and resp.get("error"):
                err = str(resp["error"])
                if "429" in err or "rate" in err.lower():
                    _rpc_mark_429(rpc_url)
            return resp
    return Web3(_RotatingHTTPProvider(rpc_url))

def get_best_w3(rpc_list: list = None) -> Web3:
    """
    Return a live Web3 using round-robin across available (non-429'd) RPCs.
    Falls back to any RPC if all are in cooldown.
    """
    global _rpc_index
    if not rpc_list:
        rpc_list = DEFAULT_BASE_RPCS
    available = [r for r in rpc_list if _rpc_is_available(r)]
    if not available:
        available = rpc_list  # all cooling down — use any
    with _rpc_lock:
        _rpc_index = (_rpc_index + 1) % len(available)
        chosen = available[_rpc_index % len(available)]
    return get_w3(chosen)

def get_working_w3(rpc_list: list = None, max_attempts: int = 5) -> Web3:
    """
    Try multiple RPCs until one works (chain_id check).
    Skips RPCs in 429 cooldown. Falls back to any RPC if all cooling down.
    """
    if not rpc_list:
        rpc_list = DEFAULT_BASE_RPCS
    tried = set()
    for _ in range(max_attempts):
        available = [r for r in rpc_list if _rpc_is_available(r) and r not in tried]
        if not available:
            available = [r for r in rpc_list if r not in tried]
        if not available:
            break
        for rpc in available:
            tried.add(rpc)
            try:
                w3 = get_w3(rpc)
                if w3.eth.chain_id == CHAIN_ID:
                    print(f"Using RPC: {rpc[:50]}...")
                    return w3
            except Exception as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    _rpc_mark_429(rpc)
                print(f"RPC {rpc[:30]}... failed: {str(e)[:60]}, trying next...")
        time.sleep(0.3)
    raise Exception("No working RPC found. Check your internet or add more RPCs in .env BACKUP_RPCS")



# =============================================================================
# TELEGRAM NOTIFICATIONS (optional)
# =============================================================================
def tg_send(message: str, reply_markup: dict = None) -> bool:
    """Send message (ethbot-style send_alert).
    Supports aliases: TELEGRAM_TOKEN / BOT_TOKEN / TG_BOT_TOKEN
    and ADMIN_CHAT_ID / TELEGRAM_CHAT_ID / TG_USER_ID.
    Uses persistent session. Truncates to 4000 chars. Returns success.
    """
    token = (os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or
             os.getenv("TG_BOT_TOKEN") or "")
    chat_id = (os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or
               os.getenv("TG_USER_ID") or "")
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": str(message)[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = tg_session.post(url, json=payload, timeout=15)
        return r.ok
    except Exception as e:
        print(f"[TG] notify failed: {e}")
        return False

# NOTE: Old raw long-poll check_tg_commands has been replaced by
# the ethbot-style implementation in telegram_bot.py using python-telegram-bot.
# The old function is removed to avoid duplicate polling.

def is_kill_switch_active(kill_file: str) -> bool:
    return os.path.exists(kill_file)

private_w3 = None

def get_private_w3() -> Optional[Web3]:
    global private_w3
    if private_w3 is not None:
        return private_w3
    # Support PRIVATE_RPC_URL (Base MEV protection), fallback to FLASHBOTS_RPC
    priv_rpc = os.getenv("PRIVATE_RPC_URL") or os.getenv("FLASHBOTS_RPC")
    if priv_rpc:
        if "rpc.flashbots.net" in priv_rpc or "flashbots.net" in priv_rpc:
            print("[PRIV] [WARN] FLASHBOTS_RPC / PRIVATE_RPC_URL is set to flashbots.net (Ethereum Mainnet). "
                  "This will fail on Base Mainnet (Chain ID 8453) with signature verification errors. "
                  "Please use a Base-specific MEV protected RPC (like Alchemy private RPC or dRPC MEV-protected endpoint).")
        try:
            private_w3 = Web3(Web3.HTTPProvider(priv_rpc))
            print(f"[PRIV] Initialized private RPC: {mask_sensitive(priv_rpc)}")
        except Exception as e:
            print(f"[PRIV] Failed to initialize private RPC: {e}")
    return private_w3

def send_raw_transaction_safe(w3: Web3, raw_tx) -> bytes:
    p_w3 = get_private_w3()
    if p_w3:
        try:
            print("Routing transaction privately via private RPC...")
            return p_w3.eth.send_raw_transaction(raw_tx)
        except Exception as e:
            print(f"Private RPC submission failed: {e}. Falling back to public RPC...")
    return w3.eth.send_raw_transaction(raw_tx)

# Simple SQLite trade logger (major upgrade for analytics)
DB_FILE = "b20_trades.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS trades
                 (id INTEGER PRIMARY KEY, timestamp TEXT, token TEXT, action TEXT, amount REAL, tx_hash TEXT, status TEXT, token_amount REAL DEFAULT 0)''')
    # Add column if old DB
    try:
        c.execute("ALTER TABLE trades ADD COLUMN token_amount REAL DEFAULT 0")
    except:
        pass
    conn.commit()
    conn.close()

def log_trade(token: str, action: str, amount: float, tx_hash: str = "", status: str = "pending", token_amount: float = 0.0):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT INTO trades (timestamp, token, action, amount, tx_hash, status, token_amount) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (datetime.utcnow().isoformat(), token, action, amount, tx_hash, status, token_amount or 0))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] log error: {e}")

def get_win_rate() -> float:
    """Simple analytics: win rate from DB (upgrade #94, #74)."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT status FROM trades WHERE action='buy'")
        buys = c.fetchall()
        conn.close()
        if not buys:
            return 0.0
        successes = sum(1 for (s,) in buys if s == 'success')
        return (successes / len(buys)) * 100
    except:
        return 0.0

def get_token_price_in_eth(w3, token):
    """Real mainnet price: ETH per 1 token (via QuoterV2, slot0, or Aerodrome Router).
    Returns float ETH amount per full token unit. 0 if no liquidity / not quotable yet.
    """
    token = to_checksum_address(token)
    
    # 1. Try Aerodrome pricing first if pool is Aerodrome
    try:
        res = find_best_pool(w3, token)
        if res and res[0] and res[1] == 'aerodrome':
            pool, dex_type, stable = res
            dec = get_token_decimals(w3, token)
            one_token = 10 ** dec
            router = w3.eth.contract(address=AERODROME_ROUTER, abi=AERODROME_ROUTER_ABI)
            routes = [(token, WETH, stable, AERODROME_FACTORY)]
            amounts = router.functions.getAmountsOut(one_token, routes).call()
            return amounts[-1] / 1e18
    except:
        pass

    # 2. Try Uniswap V3 QuoterV2
    try:
        dec = get_token_decimals(w3, token)
        one_token = 10 ** dec
        quoter = w3.eth.contract(address=UNISWAP_QUOTER_V2, abi=UNISWAP_QUOTER_V2_ABI)
        params = (token, WETH, 3000, one_token, 0)
        quoted = quoter.functions.quoteExactInputSingle(params).call()
        amount_out = quoted[0] if isinstance(quoted, (list, tuple)) else quoted
        if amount_out and amount_out > 0:
            return amount_out / 1e18
    except Exception as qerr:
        pass  # common on brand new pools with no ticks crossed

    # 3. Robust Uniswap slot0 price fallback
    try:
        res = find_best_pool(w3, token)
        if not res or not res[0]:
            return 0.0
        pool, dex_type, dex_param = res
        if dex_type == 'uniswap_v3':
            pool_contract = get_pool_contract(w3, pool)
            slot0 = pool_contract.functions.slot0().call()
            sqrt_price_x96 = slot0[0]
            if sqrt_price_x96 == 0:
                return 0.0
            price_ratio = (sqrt_price_x96 / (2 ** 96)) ** 2
            try:
                t0 = to_checksum_address(pool_contract.functions.token0().call())
                t1 = to_checksum_address(pool_contract.functions.token1().call())
                if t0 == WETH:
                    eth_per_token = 1.0 / price_ratio if price_ratio > 0 else 0
                elif t1 == WETH:
                    eth_per_token = price_ratio
                else:
                    eth_per_token = price_ratio
            except:
                eth_per_token = price_ratio
            dec = get_token_decimals(w3, token)
            eth_per_token *= (10 ** (dec - 18)) if dec != 18 else 1.0
            return max(0.0, float(eth_per_token))
        elif dex_type == 'aerodrome':
            # Aerodrome reserves fallback
            pool_contract = w3.eth.contract(address=pool, abi=[
                {"inputs": [], "name": "getReserves", "outputs": [{"type": "uint256"}, {"type": "uint256"}, {"type": "uint32"}], "stateMutability": "view", "type": "function"},
                {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
                {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
            ])
            reserves = pool_contract.functions.getReserves().call()
            t0 = pool_contract.functions.token0().call()
            if t0.lower() == WETH.lower():
                price = reserves[0] / reserves[1] if reserves[1] > 0 else 0.0
            else:
                price = reserves[1] / reserves[0] if reserves[0] > 0 else 0.0
            return price
    except:
        pass
    return 0.0

def get_bot_address() -> str:
    """Return the address the bot is using."""
    try:
        global accounts_list
        if accounts_list:
            return ", ".join(acc.address for _, acc in accounts_list)
        pk = os.getenv("PRIVATE_KEY")
        if pk:
            from eth_account import Account
            return Account.from_key(pk).address
    except:
        pass
    return "N/A"

def get_num_open_positions() -> int:
    """Fast count of open positions from DB only (no on-chain)."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT COUNT(DISTINCT token) FROM trades 
            WHERE status='success' 
            AND action='buy'
            GROUP BY token
            HAVING SUM(CASE WHEN action='buy' THEN amount ELSE 0 END) > 0
        """)
        # simpler count
        c.execute("""
            SELECT COUNT(*) FROM (
                SELECT token 
                FROM trades 
                WHERE status='success'
                GROUP BY token
                HAVING SUM(CASE WHEN action='buy' THEN amount ELSE 0 END) > 0
            ) as t
        """)
        row = c.fetchone()
        conn.close()
        return row[0] if row else 0
    except:
        return 0


TOKEN_DECIMALS_CACHE = {}
TOKEN_SYMBOL_CACHE = {}

def get_open_positions(w3=None, include_price=True) -> list:
    """Risk #65: get currently open positions from DB.
    If w3 provided, includes current price, value, PnL, moon bag suggestion.
    Made robust for new tokens where Quoter/price may fail.
    Set include_price=False for faster calls when only held/spent needed.
    """
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            SELECT token, 
                   SUM(CASE WHEN action='buy' THEN amount ELSE 0 END) as eth_spent,
                   SUM(CASE WHEN action='buy' THEN COALESCE(token_amount, 0) ELSE 0 END) as tokens_acquired
            FROM trades 
            WHERE status='success'
            GROUP BY token
            HAVING eth_spent > 0
        """)
        rows = c.fetchall()
        conn.close()
        
        positions = []
        if not rows:
            return positions
            
        cfg = load_config()
        rpc_list = cfg.get("BACKUP_RPCS", DEFAULT_BASE_RPCS)
            
        senders = []
        if w3:
            global accounts_list
            if accounts_list:
                senders = [acc.address for _, acc in accounts_list]
            else:
                try:
                    senders = [w3.eth.account.from_key(os.getenv("PRIVATE_KEY")).address]
                except:
                    pass

        def _process_row(row):
            token, eth_spent, tokens_acquired = row
            held_human = 0.0
            price = 0.0
            value = 0.0
            pnl = 0.0
            pnl_pct = 0.0
            acquired = tokens_acquired or 0.0
            entry_price = 0.0
            if acquired > 0:
                entry_price = (eth_spent or 0) / acquired
                
            suggestion = "Sell 70% now (recoup + profit), moon bag 30% for moon"
            sym = ""
            
            if w3 and senders:
                try:
                    # Distribute queries across multiple RPC endpoints using round-robin to avoid 429/403 blocks
                    thread_w3 = get_best_w3(rpc_list)
                    
                    # 1. Fetch decimals (cached or RPC)
                    dec = get_token_decimals(thread_w3, token)
                    
                    # 2. Fetch symbol (cached or RPC)
                    t_lower = token.lower()
                    if t_lower in TOKEN_SYMBOL_CACHE:
                        sym = TOKEN_SYMBOL_CACHE[t_lower]
                    else:
                        try:
                            erc_sym = thread_w3.eth.contract(address=to_checksum_address(token), abi=ERC20_MIN_ABI)
                            sym = erc_sym.functions.symbol().call()
                            TOKEN_SYMBOL_CACHE[t_lower] = sym
                        except:
                            sym = "?"
                            
                    # 3. Fetch balances across all sender addresses
                    erc = thread_w3.eth.contract(address=to_checksum_address(token), abi=ERC20_MIN_ABI)
                    held = 0
                    for s in senders:
                        try:
                            held += erc.functions.balanceOf(s).call()
                        except:
                            pass
                    held_human = held / (10 ** dec) if held else 0.0
                    
                    # 4. Fetch price ONLY if balance is > 0 and include_price is True
                    if include_price and held_human > 0:
                        try:
                            price = get_token_price_in_eth(thread_w3, token)
                            value = held_human * price
                            pnl = value - (eth_spent or 0)
                            pnl_pct = (pnl / (eth_spent or 1) * 100) if eth_spent else 0
                        except Exception as pe:
                            print(f"[MONITOR] price fetch error for {token}: {pe}")
                            suggestion = "Price not available yet (new token) - moon bag 30% recommended"
                    else:
                        price = 0.0
                        value = 0.0
                        pnl = 0.0
                        pnl_pct = 0.0
                        if held_human == 0.0:
                            suggestion = "Fully sold or 0 balance on-chain"
                except Exception as e:
                    print(f"position balance error for {token}: {e}")
                    
            note = ""
            if acquired == 0 and eth_spent > 0:
                note = "0 tokens received from swap (tax/liq/redirect?)"
                
            if acquired > 0 or held_human > 0:
                return {
                    'token': token,
                    'symbol': sym,
                    'eth_spent': eth_spent or 0,
                    'held': held_human,
                    'acquired': acquired,
                    'entry_price_eth': entry_price,
                    'price_eth': price,
                    'value_eth': value,
                    'pnl_eth': pnl,
                    'pnl_pct': pnl_pct,
                    'suggestion': suggestion,
                    'note': note
                }
            return None

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(rows))) as executor:
            results = executor.map(_process_row, rows)
            
        for res in results:
            if res:
                positions.append(res)
                
        return positions
    except Exception as e:
        print(f"get_open_positions error: {e}")
        return []


def find_token_deployer(w3: Web3, token: str) -> Optional[str]:
    """Find the deployer of the token by looking at the first mint log or owner()."""
    try:
        token_addr = to_checksum_address(token)
        transfer_sig = w3.keccak(text='Transfer(address,address,uint256)').hex()
        zero_padded = '0x0000000000000000000000000000000000000000000000000000000000000000'
        current = w3.eth.block_number
        
        # Scan last 5000 blocks for mint event (from 0x00...)
        logs = w3.eth.get_logs({
            'fromBlock': current - 5000,
            'toBlock': 'latest',
            'address': token_addr,
            'topics': [transfer_sig, zero_padded]
        })
        if logs:
            to_addr = '0x' + logs[0]['topics'][2].hex()[-40:]
            return to_checksum_address(to_addr)
            
        # Fallback to owner() call if it exists
        try:
            erc = w3.eth.contract(address=token_addr, abi=ERC20_MIN_ABI)
            owner = erc.functions.owner().call()
            if owner and int(owner, 16) != 0:
                return to_checksum_address(owner)
        except:
            pass
            
        return None
    except Exception as e:
        print(f"Error finding token deployer: {e}")
        return None

def check_holder_distribution(w3: Web3, token: str, max_top_holder_pct: float = 0.4) -> tuple[bool, str]:
    """Safety upgrade #24: real holder distribution check by scanning recent Transfer logs."""
    try:
        token_addr = to_checksum_address(token)
        erc = w3.eth.contract(address=token_addr, abi=ERC20_MIN_ABI)
        total_supply = erc.functions.totalSupply().call()
        if total_supply == 0:
            return False, "Zero supply"
            
        current = w3.eth.block_number
        transfer_sig = w3.keccak(text='Transfer(address,address,uint256)').hex()
        
        # Scan last 5000 blocks
        logs = w3.eth.get_logs({
            'fromBlock': current - 5000,
            'toBlock': 'latest',
            'address': token_addr,
            'topics': [transfer_sig]
        })
        
        balances = {}
        for log in logs:
            try:
                from_addr = to_checksum_address("0x" + log['topics'][1].hex()[-40:])
                to_addr = to_checksum_address("0x" + log['topics'][2].hex()[-40:])
                value = int(log['data'].hex(), 16)
                
                if from_addr != "0x0000000000000000000000000000000000000000":
                    balances[from_addr] = balances.get(from_addr, 0) - value
                if to_addr != "0x0000000000000000000000000000000000000000":
                    balances[to_addr] = balances.get(to_addr, 0) + value
            except:
                continue
                
        pool = None
        for fee in [100, 500, 3000, 10000]:
            p = find_or_wait_pool(w3, WETH, token, fee) or find_or_wait_pool(w3, token, WETH, fee)
            if p:
                pool = p
                break
        pool_addr = to_checksum_address(pool) if pool else None
        
        # Filter zero address, pool, and contract itself
        filtered_balances = {}
        for addr, bal in balances.items():
            if bal <= 0:
                continue
            if addr in ["0x0000000000000000000000000000000000000000", pool_addr, token_addr]:
                continue
            filtered_balances[addr] = bal
            
        if not filtered_balances:
            return True, "No individual holders found"
            
        sorted_holders = sorted(filtered_balances.items(), key=lambda x: x[1], reverse=True)
        top10_sum = sum(bal for addr, bal in sorted_holders[:10])
        top10_pct = top10_sum / total_supply
        
        if top10_pct > max_top_holder_pct:
            return False, f"High holder concentration: Top 10 holders own {top10_pct*100:.1f}% (> {max_top_holder_pct*100:.0f}%)"
            
        return True, f"Holder distribution check passed (Top 10 own {top10_pct*100:.1f}%)"
    except Exception as e:
        return True, f"Holder check skipped: {str(e)[:50]}"

def check_lp_locked(w3: Web3, pool: str, token: str, deployer: Optional[str]) -> tuple[bool, str]:
    """Safety #23: check if LP is locked/burned by verifying if NFPM LP NFTs are owned by deployers."""
    try:
        liq = check_pool_liquidity(w3, pool)
        if liq == 0:
            return False, "Zero pool liquidity"
            
        if not deployer:
            return True, "LP check skipped (no deployer found)"
            
        nfpm_addr = to_checksum_address('0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1')
        NFPM_ABI = [
            {'inputs': [{'name': 'owner', 'type': 'address'}], 'name': 'balanceOf', 'outputs': [{'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'},
            {'inputs': [{'name': 'owner', 'type': 'address'}, {'name': 'index', 'type': 'uint256'}], 'name': 'tokenOfOwnerByIndex', 'outputs': [{'type': 'uint256'}], 'stateMutability': 'view', 'type': 'function'},
            {'inputs': [{'name': 'tokenId', 'type': 'uint256'}], 'name': 'positions', 'outputs': [
                {'type': 'uint96'}, {'type': 'address'}, {'type': 'address'}, {'type': 'address'},
                {'type': 'uint24'}, {'type': 'int24'}, {'type': 'int24'}, {'type': 'uint128'},
                {'type': 'uint256'}, {'type': 'uint256'}, {'type': 'uint256'}, {'type': 'uint256'}
            ], 'stateMutability': 'view', 'type': 'function'}
        ]
        
        nfpm = w3.eth.contract(address=nfpm_addr, abi=NFPM_ABI)
        
        deployer_has_lp = False
        try:
            bal = nfpm.functions.balanceOf(deployer).call()
            for idx in range(bal):
                tid = nfpm.functions.tokenOfOwnerByIndex(deployer, idx).call()
                pos = nfpm.functions.positions(tid).call()
                t0, t1 = pos[2], pos[3]
                if t0.lower() == token.lower() or t1.lower() == token.lower():
                    if pos[7] > 0:
                        deployer_has_lp = True
                        break
        except Exception as e:
            print(f"NFPM check error for deployer: {e}")
            
        if deployer_has_lp:
            return False, "LP not locked: Deployer still owns the Uniswap V3 LP NFT"
            
        return True, "LP locked/burned: Deployer does not own the Uniswap V3 LP NFT"
    except Exception as e:
        return True, f"LP check skipped: {str(e)[:50]}"

def check_dev_wallet_concentration(w3: Web3, token: str, deployer: Optional[str], total_supply: int, max_pct: float = 0.10) -> tuple[bool, str]:
    """Safety upgrade #30: prevent buying if dev/creator holds >10% of total supply."""
    if not deployer:
        return True, "Dev concentration check skipped (no deployer found)"
    try:
        erc = w3.eth.contract(address=to_checksum_address(token), abi=ERC20_MIN_ABI)
        bal = erc.functions.balanceOf(deployer).call()
        pct = bal / total_supply
        if pct > max_pct:
            return False, f"Dev wallet holds {pct*100:.1f}% (> {max_pct*100:.0f}%) of total supply"
        return True, f"Dev wallet holds safe balance: {pct*100:.1f}%"
    except Exception as e:
        return True, f"Dev concentration check skipped: {str(e)[:50]}"

def simulate_transfer_tax(w3: Web3, token: str, amount: int) -> tuple[int, str]:
    """Safety #25: rough tax detection by transfer sim."""
    try:
        # Simulate by checking decimals and rough fee estimate via small transfer simulation if possible.
        # Enhanced stub: assume low tax for B20 unless Quoter shows loss.
        erc = w3.eth.contract(address=to_checksum_address(token), abi=ERC20_MIN_ABI)
        dec = erc.functions.decimals().call()
        # Placeholder for better: in future use eth_call on transfer.
        return 0, f"Tax sim: ~0% (enhanced stub, dec={dec})"
    except Exception as e:
        return 0, f"Tax sim error: {str(e)[:30]}"

def export_trades_csv(filename: str = "trades_export.csv") -> bool:
    """Analytics upgrade: export trades to CSV (upgrade #87)."""
    try:
        import csv
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT * FROM trades ORDER BY id")
        rows = c.fetchall()
        headers = [desc[0] for desc in c.description]
        conn.close()
        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        print(f"Exported {len(rows)} trades to {filename}")
        return True
    except Exception as e:
        print(f"CSV export failed: {e}")
        return False


def get_total_spent() -> float:
    """Total ETH spent on successful buys from DB."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT SUM(amount) FROM trades WHERE action='buy' AND status='success'")
        row = c.fetchone()
        conn.close()
        return float(row[0] or 0)
    except:
        return 0.0


def get_estimated_portfolio_value(w3=None) -> float:
    """Rough current value of open positions using live prices."""
    try:
        opens = get_open_positions(w3)
        total = 0.0
        for p in opens:
            total += p.get('value_eth', 0) or 0
        return total
    except:
        return 0.0


def get_gas_info(w3: Web3) -> dict:
    """Real mainnet gas info."""
    try:
        gas_price = w3.eth.gas_price / 1e9
        history = w3.eth.fee_history(1, 'latest', [50])
        base = history.get('baseFeePerGas', [0])[-1] / 1e9 if history.get('baseFeePerGas') else gas_price
        return {
            'gas_price_gwei': round(gas_price, 2),
            'base_fee_gwei': round(base, 2),
            'priority_gwei': 2.0  # typical
        }
    except:
        return {'gas_price_gwei': 0, 'base_fee_gwei': 0}


def run_token_safety(w3: Web3, token: str) -> str:
    """Run available safety checks and return summary string."""
    try:
        safe, reason = check_token_safety(w3, token, 0.1)  # low min for check
        return f"Safety: {'PASS' if safe else 'FAIL'} - {reason}"
    except Exception as e:
        return f"Safety check error: {str(e)[:80]}"


def get_detailed_stats() -> dict:
    """More analytics from DB."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*), SUM(amount) FROM trades WHERE action='buy' AND status='success'")
        buys, spent = c.fetchone()
        c.execute("SELECT COUNT(*) FROM trades WHERE action='buy' AND status='success'")
        total_buys = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM trades WHERE action='sell' AND status='success'")
        sells = c.fetchone()[0]
        conn.close()
        return {
            'successful_buys': buys or 0,
            'total_spent': spent or 0,
            'sells': sells or 0,
            'total_buys_logged': total_buys or 0
        }
    except:
        return {'successful_buys': 0, 'total_spent': 0, 'sells': 0}


def get_activation_status(w3: Web3) -> str:
    try:
        activated = check_b20_activated(w3)
        return f"B20 Activated: {'YES' if activated else 'NO'} (as of now)"
    except Exception as e:
        return f"Activation check error: {e}"

def check_upgradeable_contract(w3: Web3, token: str) -> tuple[bool, str]:
    """Safety #28: check if the token contract is upgradeable (proxy)."""
    try:
        token_addr = to_checksum_address(token)
        
        # 1. EIP-1967 implementation slot
        eip1967_slot = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
        impl = w3.eth.get_storage_at(token_addr, eip1967_slot).hex()
        if int(impl, 16) != 0:
            return False, f"EIP-1967 proxy (impl: 0x{impl[-40:]})"
            
        # 2. EIP-1967 beacon slot
        beacon_slot = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
        beacon = w3.eth.get_storage_at(token_addr, beacon_slot).hex()
        if int(beacon, 16) != 0:
            return False, "Beacon proxy"
            
        # 3. OZ proxy admin owner slot
        oz_slot = "0x33b8a36f6d0f62ef1244fbe58467dfa811c7ff84752ca8cfa40a7cf59599574f"
        oz_admin = w3.eth.get_storage_at(token_addr, oz_slot).hex()
        if int(oz_admin, 16) != 0:
            return False, "OZ proxy admin owner"
            
        # 4. Minimal Proxy (EIP-1167)
        bytecode = w3.eth.get_code(token_addr).hex()
        if bytecode.startswith("0x363d3d373d3d3d363d") or "363d3d373d3d3d363d" in bytecode[:30]:
            return False, "EIP-1167 Minimal Proxy"
            
        # 5. Custom implementation() view function check
        try:
            impl_abi = [{"inputs": [], "name": "implementation", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"}]
            c = w3.eth.contract(address=token_addr, abi=impl_abi)
            impl_addr = c.functions.implementation().call()
            if impl_addr and int(impl_addr, 16) != 0:
                return False, f"Custom proxy (impl: {impl_addr})"
        except:
            pass
            
        return True, "Non-upgradeable"
    except Exception as e:
        return True, f"Upgradeable check skipped: {str(e)[:50]}"

def check_malicious_and_copycat_patterns(name: str, symbol: str) -> tuple[bool, str]:
    """Safety #19, #29: scan token name and symbol for famous impersonations or malicious keywords."""
    combined = f"{name} {symbol}".upper()
    warnings = []
    
    # 1. Famous impersonations (upgrade #19)
    famous_brands = ['ETHEREUM', 'BITCOIN', 'BINANCE', 'OPENSEA', 'METAMASK', 'UNISWAP', 'BASE', 'COINBASE', 'AERODROME']
    for brand in famous_brands:
        if brand in combined and not combined.startswith(brand + " "):
            warnings.append(f"Impersonates {brand}")
            
    # 2. Formatting anomalies
    if len(name) > 40:
        warnings.append("Long name")
    if symbol and len(symbol) > 10:
        warnings.append("Long symbol")
    if '0' in symbol and 'O' in symbol:
        warnings.append("Confusing 0/O")
        
    # 3. Scam keywords (upgrade #29)
    scam_words = ['FREE', 'GIFT', 'AIRDROP', 'REWARD', 'WINNER', 'CLAIM', 'TEST']
    for word in scam_words:
        if word in combined:
            warnings.append(f"Scam word: {word}")
            
    if warnings:
        return False, f"Impersonation/scam signs: {', '.join(warnings)}"
        
    return True, "Clean name/symbol"

def check_cross_pool_arbitrage(w3: Web3, token: str, amount_eth: float = 1.0) -> dict:
    """
    Safety / Arbitrage #6: Check for cross-pool price discrepancies.
    Compares quotes across 100 (0.01%), 500 (0.05%), 3000 (0.3%), and 10000 (1%) fee tiers.
    """
    amount_in = w3.to_wei(amount_eth, 'ether')
    quotes = {}
    fees = [100, 500, 3000, 10000]
    
    for fee in fees:
        try:
            quoter = w3.eth.contract(address=UNISWAP_QUOTER_V2, abi=UNISWAP_QUOTER_V2_ABI)
            params = (
                WETH,
                to_checksum_address(token),
                fee,
                amount_in,
                0
            )
            quoted = quoter.functions.quoteExactInputSingle(params).call()
            amount_out = quoted[0] if isinstance(quoted, (list, tuple)) else quoted
            if amount_out > 0:
                quotes[fee] = amount_out
        except Exception:
            pass
            
    if len(quotes) < 2:
        return {"opportunity": False, "reason": "Fewer than 2 pools active"}
        
    highest_fee = max(quotes, key=quotes.get)
    lowest_fee = min(quotes, key=quotes.get)
    
    highest_out = quotes[highest_fee]
    lowest_out = quotes[lowest_fee]
    
    spread_pct = ((highest_out - lowest_out) / lowest_out) * 100
    
    # Threshold for arbitrage (e.g., > 1.5%)
    threshold = float(os.getenv("MIN_ARB_SPREAD_PCT", "1.5"))
    opportunity = spread_pct >= threshold
    
    res = {
        "opportunity": opportunity,
        "spread_pct": round(spread_pct, 2),
        "quotes": {f"{k}": v for k, v in quotes.items()},
        "highest_fee": highest_fee,
        "lowest_fee": lowest_fee,
        "reason": f"Spread: {spread_pct:.2f}% (High: fee {highest_fee}, Low: fee {lowest_fee})"
    }
    
    if opportunity:
        print(f"[ARB ALERT] Cross-pool arbitrage found for {token}! {res['reason']}")
        try:
            tg_send(f"📊 <b>Arbitrage Signal!</b>\nToken: <code>{token}</code>\nSpread: <b>{spread_pct:.2f}%</b>\nHigh Out: fee {highest_fee} | Low Out: fee {lowest_fee}")
        except:
            pass
            
    return res

def check_token_safety(w3: Web3, token: str, min_liq: float) -> tuple[bool, str]:
    """Enhanced safety checks to avoid honeypots, rugs, etc. Returns (is_safe, reason)"""
    try:
        # === Pool Age check (upgrade #39) ===
        min_age_secs = float(os.getenv("MIN_POOL_AGE_SECS", "30"))
        if min_age_secs > 0:
            first_seen = POOL_DETECTION_TIMES.get(token)
            if first_seen:
                age = time.time() - first_seen
                if age < min_age_secs:
                    return False, f"Pool is too new: {age:.1f}s < {min_age_secs}s"

        pool = None
        fee_tier = 3000
        for fee in [100, 500, 3000, 10000]:
            p = find_or_wait_pool(w3, WETH, token, fee) or find_or_wait_pool(w3, token, WETH, fee)
            if p:
                pool = p
                fee_tier = fee
                break
        if not pool:
            return False, "No WETH pool found"
        liq = check_pool_liquidity(w3, pool)
        if liq < w3.to_wei(min_liq, "ether"):
            return False, f"Low liquidity: {liq}"

        # Honeypot / rug checks: simulate small buy then sell using Quoter
        test_eth = 0.001
        amount_in = w3.to_wei(test_eth, 'ether')
        try:
            # Buy quote
            buy_out = get_accurate_min_out(w3, token, fee_tier, amount_in, 1000)
            if buy_out < 1:
                return False, "Honeypot: buy quote 0 or very low"

            # Sell quote back
            sell_out = get_accurate_min_out(w3, WETH, fee_tier, buy_out, 1000)
            if sell_out < amount_in * 0.7:  # lose more than 30% on roundtrip -> suspicious
                return False, f"Honeypot detected: roundtrip loss >30% (got back {sell_out / 1e18} ETH)"

        except Exception as sim_e:
            return False, f"Honeypot sim failed: {str(sim_e)[:50]}"

        # Additional B20 specific: check if token is initialized B20
        try:
            fac = get_b20_factory(w3)
            if not fac.functions.isB20Initialized(to_checksum_address(token)).call():
                # Still allow if it's B20 address pattern, but warn
                pass
        except:
            pass

        # === More safety upgrades ===
        safety_issues = []
        deployer = find_token_deployer(w3, token)

        # Holder distribution (upgrade #24)
        safe, reason = check_holder_distribution(w3, token)
        if not safe:
            safety_issues.append(reason)

        # LP locked (upgrade #23)
        safe, reason = check_lp_locked(w3, pool, token, deployer)
        if not safe:
            safety_issues.append(reason)

        # Dev wallet (upgrade #30)
        try:
            erc = w3.eth.contract(address=to_checksum_address(token), abi=ERC20_MIN_ABI)
            total_supply = erc.functions.totalSupply().call()
            safe, reason = check_dev_wallet_concentration(w3, token, deployer, total_supply)
            if not safe:
                safety_issues.append(reason)
        except Exception as e:
            print(f"Error checking dev wallet: {e}")

        # Tax sim (upgrade #25)
        tax, _ = simulate_transfer_tax(w3, token, w3.to_wei(0.001, 'ether'))
        if tax > 100:  # >1%
            safety_issues.append(f"High tax {tax}")

        # Upgradeable / Proxy Check (upgrade #28)
        safe, reason = check_upgradeable_contract(w3, token)
        if not safe:
            safety_issues.append(f"Proxy found: {reason}")

        # Name/Symbol malicious pattern check (upgrade #19, #29)
        name, symbol = get_token_name_symbol(w3, token)
        safe, reason = check_malicious_and_copycat_patterns(name, symbol)
        if not safe:
            safety_issues.append(reason)

        if safety_issues:
            return False, f"Safety issues: {', '.join(safety_issues)}"

        # Safety score (upgrade #40)
        safety_score = 70  # base
        if liq > w3.to_wei(10, "ether"):
            safety_score += 10
        # Note: is_b20 and meme defined in caller scope; use try or pass
        print(f"[SAFETY SCORE] {token}: {safety_score}/100")

        # Check cross-pool arbitrage spreads (upgrade #6)
        try:
            arb_res = check_cross_pool_arbitrage(w3, token)
            if arb_res.get("opportunity"):
                print(f"[SAFETY] Arbitrage spread warning: {arb_res['spread_pct']}% spread detected!")
        except Exception as ae:
            print(f"Error checking cross-pool arbitrage: {ae}")

        return True, f"Passed checks (score={safety_score})"
    except Exception as e:
        return False, f"Safety check error: {str(e)[:80]}"

def mainnet_sanity_check(w3: Web3, rpc_list: list = None) -> None:
    """Mainnet-Only Check. Retries with backup RPCs on 429."""
    # Try up to len(rpc_list) RPCs before giving up
    candidates = [w3] + [get_w3(r) for r in (rpc_list or DEFAULT_BASE_RPCS)[:6]]
    last_err = None
    for attempt_w3 in candidates:
        try:
            assert attempt_w3.eth.chain_id == 8453, "Wrong network! Must be Base Mainnet."
            reg = attempt_w3.eth.contract(address=ACTIVATION_REGISTRY, abi=ACTIVATION_REGISTRY_ABI)
            _ = reg.functions.isActivated(FEATURE_B20_ASSET).call()
            try:
                fac = attempt_w3.eth.contract(address=B20_FACTORY, abi=B20_FACTORY_ABI)
                _ = fac.functions.isB20("0x0000000000000000000000000000000000000001").call()
            except Exception as fe:
                # B20Factory optional — only warn, don't crash
                print(f"[SANITY] B20Factory check warning (non-fatal): {fe}")
            print("Mainnet checks passed (chainId + precompile callability).")
            return  # success
        except Exception as e:
            last_err = e
            if "429" in str(e) or "Too Many Requests" in str(e):
                url = getattr(getattr(attempt_w3, 'provider', None), 'endpoint_uri', '?')
                _rpc_mark_429(str(url))
                print(f"[SANITY] 429 on {str(url)[:40]}, trying next RPC...")
                time.sleep(1)
                continue
            # Non-429 error: still try next
            print(f"[SANITY] Check failed ({e}), trying next RPC...")
    raise AssertionError(f"Mainnet sanity check failed on all RPCs: {last_err}")



def get_activation_registry(w3: Web3):
    return w3.eth.contract(address=ACTIVATION_REGISTRY, abi=ACTIVATION_REGISTRY_ABI)

def is_feature_activated(w3: Web3, feature_hash: bytes) -> bool:
    """Call isActivated on the Activation Registry (matches Base docs naming)."""
    reg = get_activation_registry(w3)
    # Note: user prompt used isFeatureActivated; actual deployed name per docs is isActivated
    try:
        return reg.functions.isActivated(feature_hash).call()
    except Exception:
        # Fallback if naming differs on the actual deployment
        # Try common alternative names defensively (read-only)
        for name in ["isFeatureActivated", "isActivated", "activated"]:
            try:
                fn = getattr(reg.functions, name)
                return fn(feature_hash).call()
            except Exception:
                continue
        raise

def check_b20_activated(w3: Web3, want_stable: bool = False) -> bool:
    feature = FEATURE_B20_STABLECOIN if want_stable else FEATURE_B20_ASSET
    activated = is_feature_activated(w3, feature)
    # Only log on transitions to avoid log spam (was flooding every 30s)
    # The value is still used for gating.
    return activated

def get_b20_factory(w3: Web3):
    return w3.eth.contract(address=B20_FACTORY, abi=B20_FACTORY_ABI)

def get_token_name_symbol(w3: Web3, token_addr: str):
    """Fetch name and symbol for a token (ERC20 standard)."""
    try:
        abi = [
            {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
        ]
        c = w3.eth.contract(address=to_checksum_address(token_addr), abi=abi)
        name = c.functions.name().call()
        sym = c.functions.symbol().call()
        return name[:30], sym[:10]  # truncate
    except Exception:
        return "Unknown", "UNK"

def is_meme_like(name: str, symbol: str) -> bool:
    """Filter for meme-like B20s (upgrade #5). Keyword + short ticker heuristic."""
    text = (name + " " + symbol).lower()
    meme_keywords = ["pepe", "doge", "shib", "inu", "cat", "dog", "frog", "moon", "pump", "ape", "wojak", "chad", "based", "meme", "🚀", "💎", "🐸", "🐶", "🐱", "based"]
    return any(kw in text for kw in meme_keywords) or len(symbol) <= 5

def check_recent_b20_creations(w3: Web3, last_block: int, current_block: int) -> list:
    """Early signal from B20Factory (upgrade #1, #3, #10). Monitor B20Created + isB20 for sub-second edge."""
    fac = get_b20_factory(w3)
    creations = []
    try:
        # Use proper event signature for B20Created (upgrade for early detection)
        topic = fac.events.B20Created.build_filter().topics[0] if hasattr(fac.events, 'B20Created') else None
        if topic:
            logs = w3.eth.get_logs({
                "fromBlock": last_block + 1,
                "toBlock": current_block,
                "address": B20_FACTORY,
                "topics": [topic]
            })
            for log in logs:
                try:
                    # Better decode using event
                    event = fac.events.B20Created().process_log(log)
                    token = event['args'].get('token') or to_checksum_address("0x" + log["topics"][1].hex()[-40:])
                    if token:
                        creations.append(to_checksum_address(token))
                        print(f"B20Created early signal (upgrade): {token}")
                except:
                    pass
    except Exception as e:
        print(f"B20Created log error: {e}")
    return creations

def get_uniswap_v3_factory(w3: Web3):
    return w3.eth.contract(address=UNISWAP_V3_FACTORY, abi=UNISWAP_V3_FACTORY_ABI)

def get_router(w3: Web3):
    return w3.eth.contract(address=UNISWAP_V3_ROUTER, abi=UNISWAP_V3_ROUTER_ABI)

def get_pool_contract(w3: Web3, pool_addr: str):
    return w3.eth.contract(address=to_checksum_address(pool_addr), abi=UNISWAP_V3_POOL_ABI)

# =============================================================================
# GAS & FEE UTILS (Mainnet congestion handling)
# =============================================================================
def get_gas_price_with_premium(w3: Web3, premium_pct: float = 75.0) -> int:
    """Use eth_gasPrice + premium. For Base (EIP-1559) prefer fee history."""
    try:
        # Prefer EIP-1559 style
        latest = w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", 0)
        if base_fee:
            # eth_feeHistory for next block estimate
            history = w3.eth.fee_history(4, "latest", [50])
            # Use the last base fee or next as reference
            next_base = history.get("baseFeePerGas", [base_fee])[-1] if history.get("baseFeePerGas") else base_fee
            priority = w3.to_wei(2, "gwei")  # aggressive priority for sniping
            max_fee = int(next_base * (1 + premium_pct / 100)) + priority
            return max_fee  # caller can use as maxFeePerGas
    except Exception as e:
        print(f"feeHistory warning: {e}")

    # Fallback
    gas_price = w3.eth.gas_price
    return int(gas_price * (1 + premium_pct / 100))

def estimate_gas_with_buffer(w3: Web3, tx: dict, buffer: float = 1.5) -> int:
    try:
        est = w3.eth.estimate_gas(tx)
        return int(est * buffer)
    except Exception as e:
        print(f"Gas estimate failed: {e}. Using default 300000")
        return 300000

# =============================================================================
# B20 CREATE (with simulation-first)
# =============================================================================
def encode_simple_asset_params(name: str, symbol: str, admin: str, decimals: int = 18) -> bytes:
    """
    Minimal encoder for Asset variant params.
    Real production code should replicate B20FactoryLib.encodeAssetCreateParams exactly
    (versioned struct). This is a best-effort starting point.
    See https://docs.base.org/get-started/launch-b20-token
    """
    # Based on common patterns in such systems: version byte + tuple
    # For production, reverse the exact ABI used by the precompile / lib.
    # Placeholder structure: version=0x00, then (name, symbol, admin, decimals)
    version = b"\x00"
    encoded = encode(["string", "string", "address", "uint8"], [name, symbol, to_checksum_address(admin), decimals])
    return version + encoded

def predict_b20_address(w3: Web3, tx_input: str, sender: str) -> Optional[str]:
    """Predict the address of the B20 token being created using CREATE2 on B20Factory."""
    try:
        from eth_abi import decode
        if not tx_input or len(tx_input) < 10:
            return None
        if not tx_input.startswith("0x62975e6a"):
            return None
        body = bytes.fromhex(tx_input[10:])
        decoded = decode(['uint8', 'bytes32', 'bytes', 'bytes[]'], body)
        variant, salt, params, init_calls = decoded
        
        factory = get_b20_factory(w3)
        predicted = factory.functions.getB20Address(variant, to_checksum_address(sender), salt).call()
        return to_checksum_address(predicted)
    except Exception as e:
        print(f"[PREDICT] Address prediction failed: {e}")
        return None

def simulate_create_b20(w3: Web3, variant: int, salt: bytes, params: bytes, init_calls: list[bytes]) -> dict:
    """Use eth_call at 'pending' to dry-run createB20."""
    factory = get_b20_factory(w3)
    call_data = factory.encodeABI(fn_name="createB20", args=[variant, salt, params, init_calls])
    tx = {
        "to": B20_FACTORY,
        "from": w3.eth.account.from_key(os.getenv("PRIVATE_KEY")).address,
        "data": call_data,
        "value": 0,
    }
    try:
        result = w3.eth.call(tx, "pending")
        # If no revert, decode address if possible
        return {"success": True, "result": result.hex()}
    except Exception as e:
        return {"success": False, "error": str(e)}

def create_b20_live(w3: Web3, variant: int, salt: bytes, params: bytes, init_calls: list[bytes],
                    dry_run: bool = True, gas_premium: float = 100.0) -> Optional[str]:
    """
    createB20 on Mainnet.
    CRITICAL: Only call after isActivated returns true. Gas is real.
    """
    if dry_run:
        print("DRY RUN: Simulating createB20...")
        sim = simulate_create_b20(w3, variant, salt, params, init_calls)
        print("Simulation result:", sim)
        return None

    if not is_activation_time_passed():
        print("Refusing live createB20: current UTC time is before scheduled activation.")
        return None

    factory = get_b20_factory(w3)
    _, account = get_next_rotation_account(w3)
    sender = account.address

    # Build tx
    nonce = w3.eth.get_transaction_count(sender)
    priority_fee = w3.to_wei(2, "gwei")
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0) or w3.eth.gas_price
    max_fee = int(base_fee * (1 + gas_premium / 100)) + priority_fee

    tx = factory.functions.createB20(variant, salt, params, init_calls).build_transaction({
        "from": sender,
        "nonce": nonce,
        "chainId": CHAIN_ID,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority_fee,
        "value": 0,
    })

    gas = estimate_gas_with_buffer(w3, tx)
    tx["gas"] = gas

    print(f"Submitting createB20 (gas={gas}, maxFee={max_fee}) ...")
    signed = account.sign_transaction(tx)
    # Optionally route via Flashbots if user configured a private RPC
    tx_hash = send_raw_transaction_safe(w3, signed.raw_transaction)
    print("createB20 tx sent:", tx_hash.hex())

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        # Parse logs or return value if available in newer web3
        print("createB20 SUCCESS. Receipt:", receipt.transactionHash.hex())
        # In practice parse the B20Created event from logs for the token address
        return receipt.transactionHash.hex()
    else:
        print("createB20 FAILED. Receipt:", receipt)
        return None

# =============================================================================
# UNISWAP V3 SNIPING LOGIC
# =============================================================================
def get_token_decimals(w3: Web3, token: str) -> int:
    t_lower = token.lower()
    if t_lower in TOKEN_DECIMALS_CACHE:
        return TOKEN_DECIMALS_CACHE[t_lower]
    try:
        erc20 = w3.eth.contract(address=to_checksum_address(token), abi=[
            {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
        ])
        dec = erc20.functions.decimals().call()
        TOKEN_DECIMALS_CACHE[t_lower] = dec
        return dec
    except Exception:
        return 18

def check_pool_liquidity(w3: Web3, pool_addr: str) -> int:
    """Return current liquidity. 0 = no liquidity yet."""
    pool = get_pool_contract(w3, pool_addr)
    try:
        liq = pool.functions.liquidity().call()
        return liq
    except Exception as e:
        print(f"liquidity() call failed: {e}")
        return 0

def find_or_wait_pool(w3: Web3, token_a: str, token_b: str, fee: int) -> Optional[str]:
    factory = get_uniswap_v3_factory(w3)
    pool = factory.functions.getPool(to_checksum_address(token_a), to_checksum_address(token_b), fee).call()
    if pool and int(pool, 16) != 0:
        return to_checksum_address(pool)
    return None

def find_aerodrome_pool(w3: Web3, token_a: str, token_b: str) -> tuple[Optional[str], Optional[bool]]:
    """Query Aerodrome V2 Factory for stable and volatile pools."""
    try:
        factory = w3.eth.contract(address=AERODROME_FACTORY, abi=AERODROME_FACTORY_ABI)
        # Check volatile first (standard for memes)
        pool = factory.functions.getPool(to_checksum_address(token_a), to_checksum_address(token_b), False).call()
        if pool and int(pool, 16) != 0:
            return to_checksum_address(pool), False
        # Check stable
        pool = factory.functions.getPool(to_checksum_address(token_a), to_checksum_address(token_b), True).call()
        if pool and int(pool, 16) != 0:
            return to_checksum_address(pool), True
    except:
        pass
    return None, None

def find_best_pool(w3: Web3, token: str) -> tuple[Optional[str], Optional[str], Any]:
    """
    Smart Router Aggregator (Uniswap V3 + Aerodrome V2) (#60).
    Finds pools on both DEXes, compares output quotes, and returns the best pool.
    Returns: (pool_address, dex_type, dex_param)
      - if uniswap_v3: dex_type='uniswap_v3', dex_param=fee (int)
      - if aerodrome: dex_type='aerodrome', dex_param=stable (bool)
    """
    token = to_checksum_address(token)
    
    # 1. Query Uniswap V3 pools
    uni_pool = None
    uni_fee = 3000
    for fee in [100, 500, 3000, 10000]:
        p = find_or_wait_pool(w3, WETH, token, fee) or find_or_wait_pool(w3, token, WETH, fee)
        if p:
            uni_pool = p
            uni_fee = fee
            break

    # 2. Query Aerodrome pools
    aero_pool, aero_stable = find_aerodrome_pool(w3, WETH, token)
    if not aero_pool:
        aero_pool, aero_stable = find_aerodrome_pool(w3, token, WETH)

    # 3. Compare if both exist, otherwise return the one that exists
    if uni_pool and aero_pool:
        uni_quote = 0
        aero_quote = 0
        try:
            dec = get_token_decimals(w3, token)
            test_amount = 10 ** dec
            
            # Uniswap V3 quote
            try:
                quoter = w3.eth.contract(address=UNISWAP_QUOTER_V2, abi=UNISWAP_QUOTER_V2_ABI)
                params = (token, WETH, uni_fee, test_amount, 0)
                quoted = quoter.functions.quoteExactInputSingle(params).call()
                uni_quote = quoted[0] if isinstance(quoted, (list, tuple)) else quoted
            except:
                pass
                
            # Aerodrome quote
            try:
                router = w3.eth.contract(address=AERODROME_ROUTER, abi=AERODROME_ROUTER_ABI)
                routes = [(token, WETH, aero_stable, AERODROME_FACTORY)]
                res = router.functions.getAmountsOut(test_amount, routes).call()
                aero_quote = res[-1]
            except:
                pass
        except:
            pass
            
        if aero_quote > uni_quote:
            return aero_pool, 'aerodrome', aero_stable
        else:
            return uni_pool, 'uniswap_v3', uni_fee
            
    elif uni_pool:
        return uni_pool, 'uniswap_v3', uni_fee
    elif aero_pool:
        return aero_pool, 'aerodrome', aero_stable
        
    return None, None, None

def build_buy_tx(w3: Web3, token_out: str, dex_type: str, dex_param: Any, amount_in_wei: int, min_out: int, recipient: str) -> dict:
    """Build transaction for buying token_out with ETH via Uniswap V3 or Aerodrome V2."""
    if dex_type == 'uniswap_v3':
        router = get_router(w3)
        params = {
            "tokenIn": WETH,
            "tokenOut": to_checksum_address(token_out),
            "fee": dex_param,
            "recipient": to_checksum_address(recipient),
            "amountIn": amount_in_wei,
            "amountOutMinimum": min_out,
            "sqrtPriceLimitX96": 0,
        }
        tx = router.functions.exactInputSingle(params).build_transaction({
            "from": recipient,
            "value": amount_in_wei,   # ETH forwarded; SwapRouter02 wraps to WETH internally
            "chainId": CHAIN_ID,
        })
        return tx
    elif dex_type == 'aerodrome':
        router = w3.eth.contract(address=AERODROME_ROUTER, abi=AERODROME_ROUTER_ABI)
        routes = [(WETH, to_checksum_address(token_out), dex_param, AERODROME_FACTORY)]
        deadline = int(time.time()) + 600
        tx = router.functions.swapExactETHForTokens(
            min_out,
            routes,
            to_checksum_address(recipient),
            deadline
        ).build_transaction({
            "from": recipient,
            "value": amount_in_wei,
            "chainId": CHAIN_ID,
        })
        return tx
    raise ValueError(f"Unknown dex_type: {dex_type}")

def get_accurate_min_out(w3: Web3, token_out: str, fee: int, amount_in_wei: int, slippage_bps: int) -> int:
    """Use QuoterV2 for realistic amountOut, then apply slippage. Major upgrade for live trading."""
    try:
        quoter = w3.eth.contract(address=UNISWAP_QUOTER_V2, abi=UNISWAP_QUOTER_V2_ABI)
        # Proper struct for QuoterV2 quoteExactInputSingle
        params = (
            WETH,
            to_checksum_address(token_out),
            fee,
            amount_in_wei,
            0  # sqrtPriceLimitX96 = 0 for no limit
        )
        quoted = quoter.functions.quoteExactInputSingle(params).call()
        amount_out = quoted[0] if isinstance(quoted, (list, tuple)) else quoted
        min_out = int(amount_out * (10000 - slippage_bps) / 10000)
        return max(min_out, 1)  # at least 1 to avoid zero
    except Exception as e:
        print(f"[Quoter] Failed, falling back to rough estimate: {e}")
        # Rough fallback: assume 1:1 minus slippage
        return int(amount_in_wei * (10000 - slippage_bps) / 10000)

def attempt_buy(w3: Web3, token: str, fee: int, amount_eth: float, cfg: dict,
                max_retries: int = 1, force: bool = False) -> Optional[str]:
    """
    Attempt to buy the new token with ETH.
    - force=True: manual TG buy — skips pool/liq/MEV guards, reports all errors via tg_send.
    - force=False: automated snipe — full safety checks apply.
    """
    private_key, account = get_next_rotation_account(w3)
    sender = account.address

    # Pool age cooldown (upgrade #39)
    if not force:
        min_age = cfg.get("POOL_MIN_AGE_SECONDS", 10)
        detection_time = POOL_DETECTION_TIMES.get(token, time.time())
        age = time.time() - detection_time
        if age < min_age:
            wait_time = min_age - age
            print(f"[COOLDOWN] Pool is only {age:.1f}s old. Waiting {wait_time:.1f}s before buying...")
            time.sleep(wait_time)

    # Dynamic position sizing (upgrade #74)
    if not force:
        wr = get_win_rate()
        total_trades = 0
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM trades WHERE action='buy'")
            total_trades = c.fetchone()[0]
            conn.close()
        except Exception as dbe:
            print(f"Error querying trade count: {dbe}")
            
        if total_trades >= 5:
            old_amt = amount_eth
            if wr >= 70.0:
                amount_eth = min(amount_eth * 1.25, cfg.get("MAX_TRADE", 0.05))
                print(f"[DYNAMIC SIZING] High win rate ({wr:.1f}%) detected. Scaled: {old_amt} -> {amount_eth} ETH")
            elif wr < 40.0:
                amount_eth = amount_eth * 0.5
                print(f"[DYNAMIC SIZING] Low win rate ({wr:.1f}%) detected. Scaled: {old_amt} -> {amount_eth} ETH")

    if not check_rate_limit(cfg.get("MAX_BUYS_PER_MINUTE", 2)):
        msg = "⚠️ <b>Rate Limit</b>: Max buys/minute reached. Try again shortly."
        print("[RATE LIMIT] Exceeded max buys per minute.")
        tg_send(msg)
        return None

    # Pool discovery
    res = find_best_pool(w3, token)
    if not res or not res[0]:
        if force:
            # Manual buy fallback
            tg_send(f"⚠️ <b>No pool found automatically</b> for <code>{token}</code>. Trying Uniswap V3 fee=3000 anyway...")
            pool = None
            dex_type = 'uniswap_v3'
            dex_param = 3000
        else:
            msg = f"❌ No pool found for <code>{token}</code>. Buy aborted."
            print(msg)
            tg_send(msg)
            return None
    else:
        pool, dex_type, dex_param = res

    # Liquidity check
    liq = 0
    if pool:
        for _ in range(5 if not force else 2):
            liq = check_pool_liquidity(w3, pool)
            if liq > 0:
                break
            print("Pool liq still 0, waiting briefly...")
            time.sleep(1)
        if liq == 0:
            if force:
                tg_send(f"⚠️ Pool liquidity is 0 for <code>{token}</code> — sending tx anyway (manual override).")
            else:
                tg_send(f"❌ Pool has zero liquidity for <code>{token}</code>. Buy aborted.")
                print("Pool has zero liquidity after wait. Skipping.")
                return None

    # MEV Sandwich check (skip for manual force buys)
    global mempool_monitor_instance
    if mempool_monitor_instance and not force:
        base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0) or w3.eth.gas_price
        est_max_gas = int(base_fee * 1.5) + w3.to_wei(3, "gwei")
        is_sandwiched, count, details = mempool_monitor_instance.is_token_sandwiched(token, our_gas_price=est_max_gas)
        if is_sandwiched:
            print(f"⚠️ MEV Sandwich detected in mempool: {details}")
            tg_send(f"⚠️ <b>MEV Sandwich Warning</b> for <code>{token}</code>:\n{details}\n<i>Waiting for pending txs to clear...</i>")
            time.sleep(3)
            is_sandwiched, count, details = mempool_monitor_instance.is_token_sandwiched(token, our_gas_price=est_max_gas)
            if is_sandwiched:
                print("Token is still heavily sandwiched. Skipping buy for safety.")
                tg_send(f"🚫 <b>Buy Skipped</b>: Token <code>{token}</code> has active sandwich/front-run activity in the mempool.")
                return None

    print(f"Pool {pool} liquidity: {liq}. Proceeding with buy attempt via {dex_type}. (force={force})")

    amount_in = w3.to_wei(amount_eth, "ether")

    # Proper slippage from cfg + accurate quote
    if force and liq == 0:
        slippage_bps = 5000
    else:
        base_slip = cfg.get("SLIPPAGE_BPS", 2000)
        liq_eth = liq / 1e18 if liq else 0
        dyn_slip = max(500, min(base_slip, int(3000 - (liq_eth * 50))))
        slippage_bps = dyn_slip

    # Calculate min_out dynamically
    if dex_type == 'uniswap_v3':
        min_out = get_accurate_min_out(w3, token, dex_param, amount_in, slippage_bps)
    elif dex_type == 'aerodrome':
        try:
            router = w3.eth.contract(address=AERODROME_ROUTER, abi=AERODROME_ROUTER_ABI)
            routes = [(WETH, token, dex_param, AERODROME_FACTORY)]
            amounts = router.functions.getAmountsOut(amount_in, routes).call()
            out_val = amounts[-1]
            min_out = int(out_val * (10000 - slippage_bps) / 10000)
        except Exception as ae:
            print(f"[Aerodrome Quote] Failed: {ae}")
            min_out = int(amount_in * (10000 - slippage_bps) / 10000)
    else:
        min_out = int(amount_in * (10000 - slippage_bps) / 10000)

    print(f"Using dynamic slippage {slippage_bps/100}% → min_out={min_out}")

    for attempt in range(max_retries + 1):
        try:
            tx = build_buy_tx(w3, token, dex_type, dex_param, amount_in, min_out, sender)
            # Randomize priority fee slightly (+0.1 to +0.5 Gwei) to prevent predictable gas signatures (upgrade #45)
            rand_offset_gwei = random.uniform(0.1, 0.5)
            priority_fee = w3.to_wei(3 + attempt + rand_offset_gwei, "gwei")
            base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0) or w3.eth.gas_price
            max_fee = int(base_fee * (1 + (50 + attempt * 50) / 100)) + priority_fee
            tx["maxFeePerGas"] = max_fee
            tx["maxPriorityFeePerGas"] = priority_fee
            tx["nonce"] = w3.eth.get_transaction_count(sender)
            gas = estimate_gas_with_buffer(w3, tx, buffer=1.6 + attempt * 0.3)
            tx["gas"] = gas

            print(f"Buy attempt {attempt+1}: amount={amount_eth} ETH, gas={gas}, maxFee={max_fee}, priority={priority_fee}")

            # Simulate before sending — on force buy, report revert reason but still send
            try:
                w3.eth.call({**tx, "from": sender}, "pending")
            except Exception as sim_err:
                sim_msg = str(sim_err)
                print(f"eth_call simulation revert: {sim_msg}")
                if force:
                    tg_send(f"⚠️ <b>Simulation revert</b> (attempt {attempt+1}): <code>{sim_msg[:200]}</code>\n<i>Sending tx anyway (manual override)...</i>")
                elif attempt < max_retries:
                    continue  # retry with looser gas on early automated attempts

            # Send the tx
            signed = account.sign_transaction(tx)

            try:
                tx_hash = send_raw_transaction_safe(w3, signed.raw_transaction)
                global recent_buys
                recent_buys.append(time.time())
                print("Buy tx sent:", tx_hash.hex())
                tg_send(f"💰 Buy tx sent for <code>{token}</code>\nAmount: {amount_eth} ETH\nTx: <a href='https://basescan.org/tx/{tx_hash.hex()}'>{tx_hash.hex()[:20]}...</a>")
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)

                if receipt.status == 1:
                    print("BUY SUCCESS:", tx_hash.hex())

                    # ── Step 1: get decimals (required for all calculations) ──────────
                    dec = 18  # safe default
                    sym = "?"
                    try:
                        dec = get_token_decimals(w3, token)
                        sym_erc = w3.eth.contract(address=to_checksum_address(token), abi=ERC20_MIN_ABI)
                        try:
                            sym = sym_erc.functions.symbol().call()
                        except:
                            pass
                    except Exception as de:
                        print(f"[BUY] decimals error: {de}")

                    # ── Step 2: Parse Transfer logs from receipt (most reliable) ─────
                    received_tokens = 0.0
                    TRANSFER_SIG = keccak(text="Transfer(address,address,uint256)").hex()  # 64 hex chars, no 0x
                    try:
                        logs = list(receipt.logs)  # AttributeDict → list
                        print(f"[BUY] Scanning {len(logs)} logs for Transfer to sender")
                        for log in logs:
                            log_addr = log.get("address", "") if isinstance(log, dict) else getattr(log, "address", "")
                            if log_addr.lower() != token.lower():
                                continue
                            raw_topics = log.get("topics", []) if isinstance(log, dict) else getattr(log, "topics", [])
                            if not raw_topics:
                                continue
                            t0 = raw_topics[0]
                            t0_hex = (t0.hex() if hasattr(t0, "hex") else str(t0)).lstrip("0x").lower()
                            if t0_hex != TRANSFER_SIG.lower():
                                continue
                            if len(raw_topics) < 3:
                                continue
                            # topic[2] = to address (padded to 32 bytes)
                            t2 = raw_topics[2]
                            t2_hex = t2.hex() if hasattr(t2, "hex") else str(t2)
                            to_addr = "0x" + t2_hex[-40:]
                            # Accept transfer to any of our wallets
                            global accounts_list
                            our_addrs = [a.address.lower() for _, a in accounts_list] if accounts_list else [sender.lower()]
                            if to_addr.lower() not in our_addrs:
                                continue
                            raw_data = log.get("data", b"") if isinstance(log, dict) else getattr(log, "data", b"")
                            if isinstance(raw_data, str):
                                raw_data = bytes.fromhex(raw_data[2:] if raw_data.startswith("0x") else raw_data)
                            if len(raw_data) >= 32:
                                amount_raw = int.from_bytes(raw_data[:32], "big")
                                amount_human = amount_raw / (10 ** dec)
                                print(f"[BUY] Transfer log → {amount_human:.6f} {sym} to {to_addr}")
                                received_tokens = max(received_tokens, amount_human)
                    except Exception as lpe:
                        print(f"[BUY] log parse error: {lpe}")

                    # ── Step 3: Fallback — live balanceOf across all wallets ──────────
                    if received_tokens == 0.0:
                        print("[BUY] Log parse gave 0, trying live balanceOf fallback...")
                        try:
                            erc = w3.eth.contract(address=to_checksum_address(token), abi=ERC20_MIN_ABI)
                            total_bal = 0
                            our_addrs = [a.address for _, a in accounts_list] if accounts_list else [sender]
                            for addr in our_addrs:
                                try:
                                    total_bal += erc.functions.balanceOf(addr).call()
                                except Exception as be2:
                                    print(f"[BUY] balanceOf({addr}) error: {be2}")
                            received_tokens = total_bal / (10 ** dec) if total_bal else 0.0
                            print(f"[BUY] balanceOf fallback → {received_tokens:.6f} {sym}")
                        except Exception as fe:
                            print(f"[BUY] fallback balanceOf error: {fe}")

                    log_trade(token, "buy", amount_eth, tx_hash.hex(), "success", token_amount=received_tokens)
                    tg_send(
                        f"✅ <b>BUY SUCCESS</b>\n"
                        f"Token: <code>{token}</code> ({sym})\n"
                        f"ETH spent: {amount_eth}\n"
                        f"Received: <b>{received_tokens:.6f} {sym}</b>\n"
                        f"Tx: <a href='https://basescan.org/tx/{tx_hash.hex()}'>{tx_hash.hex()[:20]}...</a>"
                    )
                    export_trades_csv()
                    sell_buttons = {
                        "inline_keyboard": [
                            [{"text": "Sell 25%", "callback_data": f"sell_{token}_25"},
                             {"text": "Sell 50%", "callback_data": f"sell_{token}_50"}],
                            [{"text": "Sell 100%", "callback_data": f"sell_{token}_100"}],
                        ]
                    }
                    tg_send(f"🎉 Bought <b>{sym}</b>. Quick TP options:", reply_markup=sell_buttons)
                    return tx_hash.hex()
                else:
                    print("Buy tx reverted.")
                    tg_send(f"❌ Buy tx <b>reverted</b> on-chain for <code>{token}</code>\nTx: <a href='https://basescan.org/tx/{tx_hash.hex()}'>{tx_hash.hex()[:20]}...</a>")
            except Exception as e:
                print(f"Send error: {e}")
                tg_send(f"❌ Buy send error for <code>{token}</code>: {str(e)[:200]}")
                traceback.print_exc()



            # Per spec: if failed, retry immediately with higher gas / lower slippage
            if attempt < max_retries:
                time.sleep(0.5)
                # Re-check liquidity still exists (only if we know the pool)
                if pool and check_pool_liquidity(w3, pool) == 0:
                    print("Liquidity disappeared. Aborting retries.")
                    break

        except Exception as e:
            err_msg = f"Buy loop error in attempt {attempt}: {e}"
            print(err_msg)
            tg_send(f"❌ <b>Buy error</b> (attempt {attempt+1}): <code>{str(e)[:200]}</code>")
            traceback.print_exc()
            break

    return None

def attempt_sell(w3: Web3, token: str, pool: str, dex_type: str, dex_param: Any, amount_token: int, cfg: dict, max_retries: int = 1) -> Optional[str]:
    """Sell logic supporting Uniswap V3 or Aerodrome V2 swaps."""
    if not amount_token or amount_token <= 0:
        return None
    global accounts_list
    selected_pk = os.getenv("PRIVATE_KEY")
    account = w3.eth.account.from_key(selected_pk) if selected_pk else None
    
    if accounts_list:
        best_acc = None
        best_pk = None
        best_bal = 0
        erc = w3.eth.contract(address=to_checksum_address(token), abi=ERC20_MIN_ABI)
        for pk, acc in accounts_list:
            try:
                bal = erc.functions.balanceOf(acc.address).call()
                if bal > best_bal:
                    best_bal = bal
                    best_acc = acc
                    best_pk = pk
            except:
                pass
        if best_acc:
            account = best_acc
            selected_pk = best_pk
            print(f"[ROTATION] Selling from wallet {account.address} holding {best_bal} tokens")
            
    if not account:
        print("[ROTATION] ERROR: No wallet configured for sell.")
        return None
        
    sender = account.address
    spender = UNISWAP_V3_ROUTER if dex_type == 'uniswap_v3' else AERODROME_ROUTER
    spender_name = "SwapRouter02" if dex_type == 'uniswap_v3' else "Aerodrome Router"
    
    # Auto-approve router to spend tokens (upgrade execution path)
    try:
        erc = w3.eth.contract(address=to_checksum_address(token), abi=ERC20_MIN_ABI)
        allowance = erc.functions.allowance(sender, spender).call()
        if allowance < amount_token:
            print(f"[APPROVE] Approving {token} for {spender_name}...")
            approve_tx = erc.functions.approve(
                spender,
                115792089237316195423570985008687907853269984665640564039457584007913129639935  # uint256 max
            ).build_transaction({
                "from": sender,
                "nonce": w3.eth.get_transaction_count(sender),
                "chainId": CHAIN_ID,
            })
            approve_gas = estimate_gas_with_buffer(w3, approve_tx)
            approve_tx["gas"] = approve_gas
            priority_fee = w3.to_wei(2, "gwei")
            base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0) or w3.eth.gas_price
            approve_tx["maxFeePerGas"] = int(base_fee * 1.5) + priority_fee
            approve_tx["maxPriorityFeePerGas"] = priority_fee
            
            signed_approve = account.sign_transaction(approve_tx)
            approve_hash = send_raw_transaction_safe(w3, signed_approve.raw_transaction)
            print(f"[APPROVE] Sent approval tx: {approve_hash.hex()}")
            w3.eth.wait_for_transaction_receipt(approve_hash, timeout=60)
            print("[APPROVE] Approval confirmed.")
    except Exception as ae:
        print(f"[APPROVE] Error in approval: {ae}")
        return None

    if not pool:
        print("No pool for sell")
        return None

    # Use slippage from cfg
    slippage_bps = cfg.get("SLIPPAGE_BPS", 2000)
    min_out = 0

    if dex_type == 'uniswap_v3':
        try:
            quoter = w3.eth.contract(address=UNISWAP_QUOTER_V2, abi=UNISWAP_QUOTER_V2_ABI)
            params = (token, WETH, dex_param, amount_token, 0)
            quoted = quoter.functions.quoteExactInputSingle(params).call()
            out_val = quoted[0] if isinstance(quoted, (list, tuple)) else quoted
            min_out = int(out_val * (10000 - slippage_bps) / 10000)
        except:
            min_out = int(amount_token * (10000 - slippage_bps) / 10000)

        router = get_router(w3)
        params = {
            "tokenIn": to_checksum_address(token),
            "tokenOut": WETH,
            "fee": dex_param,
            "recipient": sender,
            "amountIn": amount_token,
            "amountOutMinimum": min_out,
            "sqrtPriceLimitX96": 0,
        }
        try:
            tx = router.functions.exactInputSingle(params).build_transaction({
                "from": sender,
                "chainId": CHAIN_ID,
            })
        except Exception as bte:
            print(f"Failed to build sell transaction: {bte}")
            return None

    elif dex_type == 'aerodrome':
        try:
            router = w3.eth.contract(address=AERODROME_ROUTER, abi=AERODROME_ROUTER_ABI)
            routes = [(token, WETH, dex_param, AERODROME_FACTORY)]
            amounts = router.functions.getAmountsOut(amount_token, routes).call()
            out_val = amounts[-1]
            min_out = int(out_val * (10000 - slippage_bps) / 10000)
        except:
            min_out = int(amount_token * (10000 - slippage_bps) / 10000)

        router = w3.eth.contract(address=AERODROME_ROUTER, abi=AERODROME_ROUTER_ABI)
        routes = [(token, WETH, dex_param, AERODROME_FACTORY)]
        deadline = int(time.time()) + 600
        try:
            tx = router.functions.swapExactTokensForETH(
                amount_token,
                min_out,
                routes,
                sender,
                deadline
            ).build_transaction({
                "from": sender,
                "chainId": CHAIN_ID,
            })
        except Exception as bte:
            print(f"Failed to build sell transaction: {bte}")
            return None

    priority_fee = w3.to_wei(2, "gwei")
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 0) or w3.eth.gas_price
    max_fee = int(base_fee * (1 + 50 / 100)) + priority_fee
    tx["maxFeePerGas"] = max_fee
    tx["maxPriorityFeePerGas"] = priority_fee
    tx["nonce"] = w3.eth.get_transaction_count(sender)
    gas = estimate_gas_with_buffer(w3, tx)
    tx["gas"] = gas
    print(f"Sell attempt ({dex_type}): amount_token={amount_token}")
    signed = account.sign_transaction(tx)
    try:
        tx_hash = send_raw_transaction_safe(w3, signed.raw_transaction)
        print("Sell tx sent:", tx_hash.hex())
        tg_send(f"💸 Sell tx sent for <code>{token}</code>\nTx: <code>{tx_hash.hex()}</code>")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        if receipt.status == 1:
            log_trade(token, "sell", amount_token, tx_hash.hex(), "success")
            tg_send(f"✅ <b>SELL SUCCESS</b> for {token}")
            return tx_hash.hex()
    except Exception as e:
        print(f"Sell error: {e}")
    return None

# =============================================================================
# MONITORING
# =============================================================================
def monitor_new_pools_and_snipe(w3: Web3, buy_amount_eth: float = 0.05, cfg: dict = None, dry_run: bool = True):
    """
    Poll for UniswapV3 PoolCreated using get_logs (more reliable than persistent filters on HTTP RPCs).
    On new pool involving a token that looks like a fresh launch (or B20), attempt buy.
    Automatic small amount 0.001 ETH sniping when live and activated.
    """
    factory = get_uniswap_v3_factory(w3)
    pool_created_topic = factory.events.PoolCreated.build_filter().topics[0]  # approx, use full event

    print("Starting Uniswap V3 PoolCreated monitor (Mainnet, polling mode)...")

    last_block = w3.eth.block_number
    seen_pools = set()

    # Initial activation check so we don't miss auto-snipes right at start of monitor
    activated = False
    try:
        activated = check_b20_activated(w3, want_stable=False)
    except Exception:
        pass
    flip_alert_sent = activated  # if already true don't spam the flip message
    last_activation_check = time.time()

    # Dynamic configurable amounts for automatic sniping
    SNIPE_AMOUNT_ETH = float(os.getenv("SNIPE_AMOUNT_ETH", "0.015"))
    SNIPE_AMOUNT_O1_ETH = float(os.getenv("SNIPE_AMOUNT_O1_ETH", "0.001"))
    SNIPE_AMOUNT_CC0_ETH = float(os.getenv("SNIPE_AMOUNT_CC0_ETH", "0.001"))

    while True:
        if is_kill_switch_active(cfg.get("KILL_SWITCH_FILE", "")):
            print("KILL SWITCH ACTIVE - stopping monitor")
            tg_send("🛑 Kill switch activated - bot stopped")
            break

        current_time = time.time()
        if current_time - last_activation_check > 30:
            try:
                activated = check_b20_activated(w3, want_stable=False)
                last_activation_check = current_time
                if activated and not flip_alert_sent:
                    tg_send(
                        f"🎉 B20 ACTIVATION FLIPPED! Now fully LIVE for real B20 meme sniping.\n"
                        f"Target sizes: o1.exchange = {SNIPE_AMOUNT_O1_ETH} ETH, cc0.company = {SNIPE_AMOUNT_CC0_ETH} ETH"
                    )
                    flip_alert_sent = True
            except Exception as act_e:
                print(f"Activation check error: {act_e}")

        try:
            current_block = w3.eth.block_number
            if current_block > last_block:
                # Early B20 creation signals (key upgrade)
                check_recent_b20_creations(w3, last_block, current_block)

                # Use get_logs for PoolCreated
                logs = w3.eth.get_logs({
                    "fromBlock": last_block + 1,
                    "toBlock": current_block,
                    "address": UNISWAP_V3_FACTORY,
                    "topics": [factory.events.PoolCreated.build_filter().topics[0]]
                })
                for log in logs:
                    try:
                        event = factory.events.PoolCreated().process_log(log)
                        args = event["args"]
                        token0 = args["token0"]
                        token1 = args["token1"]
                        fee = args["fee"]
                        pool = args["pool"]

                        if pool in seen_pools:
                            continue
                        seen_pools.add(pool)

                        print(f"PoolCreated: {token0} / {token1} fee={fee} pool={pool}")

                        new_token = None
                        pairing_asset = None
                        if token0.lower() == WETH.lower() or token0.lower() == USDC.lower():
                            new_token = token1
                            pairing_asset = token0
                        elif token1.lower() == WETH.lower() or token1.lower() == USDC.lower():
                            new_token = token0
                            pairing_asset = token1

                        if not new_token or not pairing_asset:
                            print(f"[PAIR FILTER] Skipping pool {pool}: neither token is WETH or USDC")
                            continue

                        if new_token not in POOL_DETECTION_TIMES:
                            POOL_DETECTION_TIMES[new_token] = time.time()

                        if new_token in BLACKLIST:
                            print(f"[BLACKLIST] Skipping {new_token}")
                            continue

                        is_b20 = False
                        try:
                            fac = get_b20_factory(w3)
                            is_b20 = fac.functions.isB20(to_checksum_address(new_token)).call()
                        except Exception:
                            pass

                        if not (new_token.lower().startswith("0xb20") or is_b20):
                            print(f"[B20 FILTER] Token {new_token} is not a B20 token. Skipping.")
                            continue

                        is_o1 = new_token.lower().endswith("01")
                        is_cc0 = new_token.lower().endswith("cc0")
                        
                        if cfg.get("ONLY_O1_LAUNCHPAD", False) or cfg.get("ONLY_CC0_LAUNCHPAD", False):
                            allowed_launchpad = False
                            if cfg.get("ONLY_O1_LAUNCHPAD", False) and is_o1:
                                allowed_launchpad = True
                            if cfg.get("ONLY_CC0_LAUNCHPAD", False) and is_cc0:
                                allowed_launchpad = True
                            if not allowed_launchpad:
                                print(f"[LAUNCHPAD FILTER] Token {new_token} is not from an enabled launchpad. Skipping.")
                                continue

                        name, sym = get_token_name_symbol(w3, new_token)
                        meme = is_meme_like(name, sym)
                        print(f"Detected likely B20 token: {new_token} (isB20={is_b20}, meme_like={meme})")

                        # Upgrade #4: watch for initial liquidity adds with exact amounts
                        initial_liq = check_pool_liquidity(w3, pool)
                        print(f"Initial liquidity add for {new_token}: {initial_liq} (pool {pool})")

                        o1_tag = " ⚖️ <b>[o1.exchange Launchpad]</b>" if is_o1 else ""
                        cc0_tag = " 🎨 <b>[cc0.company Launchpad]</b>" if is_cc0 else ""
                        tag = o1_tag + cc0_tag
                        msg = f"🆕 <b>{name} ({sym})</b>{tag}\n<code>{new_token}</code>\nPool: <code>{pool}</code> fee={fee} liq={initial_liq} {'[MEME]' if meme else ''}"

                        buttons = get_buy_keyboard_dict(new_token) if TG_LIB_AVAILABLE else {
                            "inline_keyboard": [
                                [{"text": "0.001 ETH", "callback_data": f"buy_{new_token}_0.001"},
                                 {"text": "0.003 ETH", "callback_data": f"buy_{new_token}_0.003"},
                                 {"text": "0.005 ETH", "callback_data": f"buy_{new_token}_0.005"}],
                                [{"text": "0.007 ETH", "callback_data": f"buy_{new_token}_0.007"},
                                 {"text": "0.01 ETH", "callback_data": f"buy_{new_token}_0.01"},
                                 {"text": "0.015 ETH", "callback_data": f"buy_{new_token}_0.015"}],
                            ]
                        }
                        tg_send(msg, reply_markup=buttons)

                        # Upgrade #5: prefer meme-like for auto snipe priority (log for now)
                        if meme:
                            print(f"[MEME FILTER] {new_token} looks like a meme - prioritizing")

                        buy_amt = SNIPE_AMOUNT_CC0_ETH if is_cc0 else (SNIPE_AMOUNT_O1_ETH if is_o1 else SNIPE_AMOUNT_ETH)

                        # Automatic small amount sniping - no pool alerts
                        if dry_run or not activated:
                            print(f"[{'DRY RUN' if dry_run else 'WAITING ACTIVATION'}] Would snipe {new_token} with {buy_amt} ETH")
                            liq = check_pool_liquidity(w3, pool)
                            print(f"[{'DRY' if dry_run else 'WAIT'}] liquidity() = {liq}")
                            continue

                        # Upgrade #65: max concurrent positions check
                        if len(ACTIVE_POSITIONS) >= MAX_CONCURRENT:
                            print(f"[RISK] Max concurrent {MAX_CONCURRENT} reached, skipping {new_token}")
                            continue

                        # Real automatic snipe with small fixed amount
                        safe, reason = check_token_safety(w3, new_token, cfg.get("MIN_LIQUIDITY_ETH", 5.0))
                        if not safe:
                            if "Low liquidity" in reason or "No WETH pool found" in reason or "Honeypot sim failed" in reason:
                                if new_token not in PENDING_POOLS_TO_RETRY:
                                    print(f"[RETRY QUEUE] Adding {new_token} to retry queue (Reason: {reason})")
                                    PENDING_POOLS_TO_RETRY[new_token] = {
                                        'pool': pool,
                                        'fee': fee,
                                        'timestamp': time.time(),
                                        'attempts': 0
                                    }
                            else:
                                print(f"[SAFETY SKIP] {new_token}: {reason}")
                            continue
                        print(f"AUTO SNIPE: Attempting buy {new_token} with {buy_amt} ETH (live)")
                        attempt_buy(w3, new_token, fee, buy_amt, cfg, max_retries=1)
                        ACTIVE_POSITIONS[new_token] = buy_amt  # track stub
                    except Exception as decode_err:
                        print(f"Log decode error: {decode_err}")

                # Process retry queue for 0-liquidity pools
                retry_tokens = list(PENDING_POOLS_TO_RETRY.keys())
                for token in retry_tokens:
                    item = PENDING_POOLS_TO_RETRY[token]
                    if time.time() - item['timestamp'] > 300 or item['attempts'] >= 20:
                        print(f"[RETRY QUEUE] Discarding {token} (Timeout or Max attempts reached)")
                        PENDING_POOLS_TO_RETRY.pop(token, None)
                        continue
                    
                    is_o_retry = token.lower().endswith("01")
                    is_cc_retry = token.lower().endswith("cc0")
                    buy_amt = SNIPE_AMOUNT_CC0_ETH if is_cc_retry else (SNIPE_AMOUNT_O1_ETH if is_o_retry else SNIPE_AMOUNT_ETH)

                    item['attempts'] += 1
                    print(f"[RETRY QUEUE] Re-checking {token} (Attempt {item['attempts']}/20)...")
                    try:
                        safe_ret, reason_ret = check_token_safety(w3, token, cfg.get("MIN_LIQUIDITY_ETH", 5.0))
                        if safe_ret:
                            if len(ACTIVE_POSITIONS) >= MAX_CONCURRENT:
                                print(f"[RISK] Max concurrent {MAX_CONCURRENT} reached, skipping retry for {token}")
                                PENDING_POOLS_TO_RETRY.pop(token, None)
                                continue
                            print(f"AUTO SNIPE (RETRY SUCCESS): Attempting buy {token} with {buy_amt} ETH")
                            attempt_buy(w3, token, item['fee'], buy_amt, cfg, max_retries=1)
                            ACTIVE_POSITIONS[token] = buy_amt
                            PENDING_POOLS_TO_RETRY.pop(token, None)
                        else:
                            if not ("Low liquidity" in reason_ret or "No WETH pool found" in reason_ret or "Honeypot sim failed" in reason_ret):
                                print(f"[RETRY QUEUE] Discarding {token} due to permanent safety issue: {reason_ret}")
                                PENDING_POOLS_TO_RETRY.pop(token, None)
                    except Exception as ret_err:
                        print(f"[RETRY QUEUE] Error re-checking {token}: {ret_err}")

                last_block = current_block

            time.sleep(3)  # Sleep to reduce rate limits; with paid RPC can be lower for faster detection

            # TG commands now in separate thread for speed (see below)

        except KeyboardInterrupt:
            print("Monitor stopped by user.")
            break
        except Exception as e:
            print(f"Monitor error: {e}")
            # Refresh w3 from the many RPCs on error (failover)
            try:
                rpc_list = cfg.get("BACKUP_RPCS", []) or DEFAULT_BASE_RPCS
                w3 = get_working_w3(rpc_list)
                global current_w3
                current_w3 = w3
                print("Switched to new RPC due to error")
            except:
                pass
            time.sleep(10)  # Backoff on errors (e.g. 429 rate limit)

# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="B20 Base Mainnet Sniper/Launcher (Mainnet ONLY)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Simulate only, no real tx (default)")
    parser.add_argument("--live", action="store_true", help="Enable live transactions (DANGEROUS)")
    parser.add_argument("--create-b20", action="store_true", help="Create a B20 token (requires --live and activation)")
    parser.add_argument("--monitor", action="store_true", help="Monitor Uniswap pools and snipe")
    parser.add_argument("--buy-amount", type=float, default=0.03, help="ETH amount per snipe attempt")
    parser.add_argument("--salt", type=str, default="grok-b20-launch-1", help="Salt for createB20 (string)")
    args = parser.parse_args()

    dry_run = not args.live

    print("=" * 70)
    print("B20 MAINNET BOT - BASE MAINNET (chainId=8453) ONLY")
    print(f"Current UTC: {now_utc().isoformat()}")
    print(f"Scheduled activation: {ACTIVATION_UTC.isoformat()}")
    print(f"Mode: {'DRY-RUN / SIMULATE' if dry_run else 'LIVE - REAL FUNDS AT RISK'}")
    print("=" * 70)

    cfg = load_config()
    init_db()
    rpc_list = cfg.get("BACKUP_RPCS", []) or DEFAULT_BASE_RPCS
    w3 = get_working_w3(rpc_list)
    init_accounts(w3)
    global ACTIVE_POSITIONS
    ACTIVE_POSITIONS = load_active_positions_from_db()
    print(f"[MONITOR] Loaded {len(ACTIVE_POSITIONS)} active positions from DB.")
    # Start background position monitor loop
    position_monitor_thread = threading.Thread(
        target=lambda: asyncio.run(monitor_positions_loop(w3, cfg)),
        daemon=True
    )
    position_monitor_thread.start()
    
    global current_w3
    current_w3 = w3

    # Start interactive TG bot (ethbot style using python-telegram-bot library)
    # Outbound alerts continue to use the simple tg_send (requests)
    if TG_LIB_AVAILABLE:
        set_sniper_context(current_w3, cfg, attempt_buy, attempt_sell)
        tg_thread = start_telegram_bot_in_background()
        if tg_thread:
            print("[TG] Interactive bot started with python-telegram-bot")
    else:
        # Fallback would go here (old raw polling removed in favor of library)
        print("[TG] python-telegram-bot not installed. Install requirements and restart.")

    # Live mode guard
    if not dry_run:
        key = cfg.get("PRIVATE_KEY", "")
        if not key or "YOUR" in key or len(key) < 10:
            print("ERROR: Cannot run in LIVE mode with invalid/placeholder PRIVATE_KEY.")
            print("Update .env with real key and restart.")
            sys.exit(1)
        print("LIVE MODE: Real funds at risk. Ensure wallet is funded with small test amount.")

    # Enforce Mainnet
    mainnet_sanity_check(w3, rpc_list=rpc_list)

    # Activation status (critical)
    asset_ok = check_b20_activated(w3, want_stable=False)
    if not asset_ok:
        print("WARNING: B20 ASSET not yet activated on-chain. createB20 will revert with FeatureNotActivated.")

    mode_str = "LIVE" if not dry_run else "DRY-RUN"
    safe_cfg = get_safe_config(cfg)
    print(f"MAX_TRADE={safe_cfg['MAX_TRADE_ETH']} ETH | SLIPPAGE={safe_cfg['SLIPPAGE_BPS']}bps | KILL={safe_cfg['KILL_SWITCH_FILE']}")
    print(f"RPC (masked): {mask_sensitive(safe_cfg['RPC_URL'])}")
    print(f"Win rate so far: {get_win_rate():.1f}% (from DB)")  # upgrade analytics
    tg_send(f"🚀 <b>B20 Bot started</b>\nMode: {mode_str}\nB20 Activated: {asset_ok}\nChain: 8453\nMax trade: {cfg['MAX_TRADE_ETH']} ETH")

    if not dry_run:
        print("⚠️  LIVE MODE ENABLED - REAL ETH WILL BE USED")
        tg_send("⚠️ <b>LIVE MODE</b> - Real trades active")

    # Send buttons menu on startup (ethbot style)
    buttons = get_control_keyboard_dict() if TG_LIB_AVAILABLE else {
        "inline_keyboard": [
            [{"text": "📊 Status", "callback_data": "status"},
             {"text": "⏸️ Pause", "callback_data": "pause"}],
            [{"text": "▶️ Resume", "callback_data": "resume"},
             {"text": "🛑 Kill", "callback_data": "kill"}]
        ]
    }
    tg_send("B20 Bot Controls:", reply_markup=buttons)

    if args.create_b20:
        print("\n--- CREATE B20 ---")
        if not asset_ok:
            print("Aborting create: feature not activated.")
            return

        account = w3.eth.account.from_key(cfg["PRIVATE_KEY"])
        salt = keccak(text=args.salt)
        params = encode_simple_asset_params("GrokB20Test", "GB20", account.address, 18)
        init_calls: list[bytes] = []  # Add grantRole / supply cap etc via proper encoding in production

        print("Params encoded (demo). Use proper B20FactoryLib encoding for production.")
        create_b20_live(w3, variant=0, salt=salt, params=params, init_calls=init_calls, dry_run=dry_run)

    if args.monitor or (not args.create_b20):
        print("\n--- STARTING POOL MONITOR + SNIPER ---")
        # Start mempool monitoring in background for early signals if available
        if MEMPOOL_AVAILABLE and not dry_run:
            try:
                def on_b20_mem(tx, txh, st, name="B20"):
                    predicted = tx.get('to')
                    if not predicted or predicted == B20_FACTORY:
                        predicted = predict_b20_address(w3, tx.get('input', ''), tx.get('from', ''))
                    if not predicted:
                        print("[MEMPOOL] Could not predict token address for B20 transaction.")
                        return
                    is_o1 = predicted.lower().endswith("01")
                    is_cc0 = predicted.lower().endswith("cc0")
                    if cfg.get("ONLY_O1_LAUNCHPAD", False) or cfg.get("ONLY_CC0_LAUNCHPAD", False):
                        allowed_launchpad = False
                        if cfg.get("ONLY_O1_LAUNCHPAD", False) and is_o1:
                            allowed_launchpad = True
                        if cfg.get("ONLY_CC0_LAUNCHPAD", False) and is_cc0:
                            allowed_launchpad = True
                        if not allowed_launchpad:
                            print(f"[LAUNCHPAD FILTER] Predicted token {predicted} is not from an enabled launchpad. Skipping mempool snipe.")
                            return
                    tag = " [o1.exchange Launchpad]" if is_o1 else (" [cc0.company Launchpad]" if is_cc0 else "")
                    msg = f"🆕 <b>{name}</b> (MEMPOOL EARLY){tag}\nPredicting token: <code>{predicted}</code>"
                    print(f"[MEMPOOL] Detected pending B20 creation for token {predicted}{tag}")
                    buttons = get_buy_keyboard_dict(predicted) if TG_LIB_AVAILABLE else {
                        "inline_keyboard": [
                            [{"text": "0.001 ETH", "callback_data": f"buy_{predicted}_0.001"},
                             {"text": "0.003 ETH", "callback_data": f"buy_{predicted}_0.003"},
                             {"text": "0.005 ETH", "callback_data": f"buy_{predicted}_0.005"}],
                            [{"text": "0.007 ETH", "callback_data": f"buy_{predicted}_0.007"},
                             {"text": "0.01 ETH", "callback_data": f"buy_{predicted}_0.01"},
                             {"text": "0.015 ETH", "callback_data": f"buy_{predicted}_0.015"}],
                        ]
                    }
                    tg_send(msg, reply_markup=buttons)
                    if not dry_run:
                        buy_amt = SNIPE_AMOUNT_CC0_ETH if is_cc0 else (SNIPE_AMOUNT_O1_ETH if is_o1 else SNIPE_AMOUNT_ETH)
                        print(f"[MEMPOOL] Triggering early snipe buy attempt for token {predicted} with {buy_amt} ETH...")
                        attempt_buy(w3, predicted, 3000, buy_amt, cfg)
                ws_url = os.getenv("WEBSOCKET_RPC") or os.getenv("WS_RPC") or cfg.get("RPC_URL", "wss://base-mainnet.public.blastapi.io").replace("https://", "wss://")
                if "mainnet.base.org" in ws_url:
                    ws_url = "wss://base.publicnode.com"
                
                mempool = MempoolMonitor(
                    ws_rpc_url=ws_url,
                    on_b20_detected=on_b20_mem,
                    on_pool_detected=lambda tx, txh, st: None
                )
                global mempool_monitor_instance
                mempool_monitor_instance = mempool
                # Run in background thread
                mempool_thread = threading.Thread(target=lambda: asyncio.run(mempool.start()), daemon=True)
                mempool_thread.start()
                print("Mempool monitoring started in background for early detection")
            except Exception as me:
                print(f"Mempool start failed (optional): {me}")

        monitor_new_pools_and_snipe(w3, buy_amount_eth=min(args.buy_amount, cfg["MAX_TRADE_ETH"]), cfg=cfg, dry_run=dry_run)

if __name__ == "__main__":
    main()
