#!/usr/bin/env python3
"""
Enhanced Execution Module for B20 Bot
======================================
Advanced execution features:
- Dynamic slippage calculation based on liquidity
- Multi-path buying (parallel fee tier quotes)
- QuoterV2 integration for accurate amountOutMinimum
- EIP-1559 dynamic gas calculation
- Flashbots private RPC support
- Retry logic with increasing gas
"""

import os
from typing import Optional, Dict, Tuple, List
from web3 import Web3
from eth_utils import to_checksum_address
from eth_abi import encode
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Advanced execution with optimization."""

    UNISWAP_QUOTER_V2_ABI = [
        {
            "inputs": [{"type": "bytes"}],
            "name": "quoteExactInputSingle",
            "outputs": [
                {"type": "uint256"},  # amountOut
                {"type": "uint160"},  # sqrtPriceX96After
                {"type": "uint32"},   # initializedTicksCrossed
                {"type": "uint256"}   # gasEstimate
            ],
            "type": "function",
            "stateMutability": "nonpayable"
        }
    ]

    ROUTER_ABI = [
        {
            "inputs": [
                {"type": "address"},  # tokenIn
                {"type": "address"},  # tokenOut
                {"type": "uint24"},   # fee
                {"type": "address"},  # recipient
                {"type": "uint256"},  # deadline
                {"type": "uint256"},  # amountIn
                {"type": "uint256"},  # amountOutMinimum
                {"type": "uint160"}   # sqrtPriceLimitX96
            ],
            "name": "exactInputSingle",
            "outputs": [{"type": "uint256"}],
            "type": "function",
            "stateMutability": "payable"
        }
    ]

    def __init__(
        self,
        w3: Web3,
        quoter_v2: str,
        router: str,
        weth: str,
        private_key: str = "",
        use_flashbots: bool = False
    ):
        self.w3 = w3
        self.quoter_v2 = to_checksum_address(quoter_v2)
        self.router = to_checksum_address(router)
        self.weth = to_checksum_address(weth)
        self.private_key = private_key
        self.use_flashbots = use_flashbots
        self.account = w3.eth.account.from_key(private_key) if private_key else None

    # =========== DYNAMIC SLIPPAGE ===========
    def calculate_dynamic_slippage(
        self,
        amount_in_wei: int,
        pool_liquidity_eth: float,
        volatility_score: float = 0.5
    ) -> int:
        """
        Calculate dynamic slippage based on:
        - Position size relative to liquidity
        - Estimated volatility
        
        Returns: slippage in basis points (e.g., 2000 = 20%)
        """
        # Base slippage: 0.5% (Uniswap fee)
        base_slippage_bps = 50
        
        # Impact cost: larger trades have more impact
        # If buying 5% of pool: add 2% slippage
        # If buying 10% of pool: add 5% slippage
        amount_eth = self.w3.from_wei(amount_in_wei, "ether")
        
        if pool_liquidity_eth > 0:
            pool_impact_percent = (amount_eth / pool_liquidity_eth) * 100
            impact_bps = int(pool_impact_percent * 20)  # Scale: 1% pool = 20 bps
        else:
            impact_bps = 500  # Default high slippage if no liquidity data
        
        # Volatility adjustment (0.5 = moderate = +50 bps, 1.0 = high = +200 bps)
        volatility_bps = int(volatility_score * 200)
        
        # Total slippage with safety margin
        total_slippage_bps = base_slippage_bps + impact_bps + volatility_bps
        
        # Cap at reasonable max (50%)
        return min(total_slippage_bps, 5000)

    # =========== MULTI-PATH BUYING ===========
    async def get_best_quote(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        fee_tiers: List[int] = None
    ) -> Tuple[int, int, str]:
        """
        Get best quote across multiple fee tiers in parallel.
        Returns: (best_amount_out, best_fee_tier, reason)
        """
        if fee_tiers is None:
            fee_tiers = [500, 3000, 10000]  # 0.05%, 0.30%, 1.00%
        
        best_amount = 0
        best_fee = fee_tiers[0]
        
        for fee in fee_tiers:
            try:
                amount_out = self._quote_exact_input_single(
                    to_checksum_address(token_in),
                    to_checksum_address(token_out),
                    fee,
                    amount_in
                )
                
                if amount_out > best_amount:
                    best_amount = amount_out
                    best_fee = fee
                    logger.info(f"Fee {fee}: {amount_out} out")
            
            except Exception as e:
                logger.warning(f"Quote failed for fee {fee}: {e}")
        
        return (best_amount, best_fee, f"Best fee: {best_fee}")

    def _quote_exact_input_single(
        self,
        token_in: str,
        token_out: str,
        fee: int,
        amount_in: int
    ) -> int:
        """Get quote from QuoterV2."""
        try:
            quoter = self.w3.eth.contract(
                address=self.quoter_v2,
                abi=self.UNISWAP_QUOTER_V2_ABI
            )
            
            # Encode params
            params = self.w3.codec.encode(
                ['address', 'address', 'uint24', 'uint256', 'uint160'],
                [token_in, token_out, fee, amount_in, 0]
            )
            
            result = quoter.functions.quoteExactInputSingle(params).call()
            return result[0]  # amountOut
        
        except Exception as e:
            logger.error(f"QuoterV2 error: {e}")
            return 0

    # =========== GAS OPTIMIZATION ===========
    def calculate_optimal_gas_price(self, aggressive: bool = False) -> Dict[str, int]:
        """
        Calculate optimal gas price using EIP-1559.
        Returns: {maxPriorityFeePerGas, maxFeePerGas, gasLimit}
        """
        try:
            fee_history = self.w3.eth.fee_history(10, 'pending')
            
            base_fee = int(fee_history['baseFeePerGas'][-1])
            priority_fees = []
            
            for rewards in fee_history['reward']:
                if rewards:
                    priority_fees.append(max(rewards))
            
            avg_priority = int(sum(priority_fees) / len(priority_fees)) if priority_fees else 0
            
            # Aggressive: use 150% of base + 200% of priority
            # Normal: use 110% of base + 150% of priority
            multiplier_base = 1.5 if aggressive else 1.1
            multiplier_priority = 2.0 if aggressive else 1.5
            
            max_fee = int(base_fee * multiplier_base + avg_priority * multiplier_priority)
            max_priority = int(avg_priority * multiplier_priority)
            gas_limit = 300000  # Typical swap
            
            return {
                "maxPriorityFeePerGas": max_priority,
                "maxFeePerGas": max_fee,
                "gasLimit": gas_limit
            }
        
        except Exception as e:
            logger.error(f"Gas calculation error: {e}")
            # Default fallback
            return {
                "maxPriorityFeePerGas": self.w3.to_wei(2, "gwei"),
                "maxFeePerGas": self.w3.to_wei(10, "gwei"),
                "gasLimit": 300000
            }

    # =========== TRANSACTION CONSTRUCTION ===========
    def build_swap_tx(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        amount_out_min: int,
        fee_tier: int = 3000,
        recipient: Optional[str] = None,
        deadline_seconds: int = 60
    ) -> Dict:
        """Build a swap transaction."""
        if not recipient:
            recipient = self.account.address if self.account else token_in
        
        recipient = to_checksum_address(recipient)
        token_in = to_checksum_address(token_in)
        token_out = to_checksum_address(token_out)
        
        deadline = int(time.time()) + deadline_seconds
        
        # Build function call
        router = self.w3.eth.contract(address=self.router, abi=self.ROUTER_ABI)
        
        fn = router.functions.exactInputSingle((
            token_in,
            token_out,
            fee_tier,
            recipient,
            deadline,
            amount_in,
            amount_out_min,
            0  # sqrtPriceLimitX96
        ))
        
        return fn

    def send_raw_transaction_safe(self, raw_tx) -> bytes:
        """Submit transaction privately via Flashbots/private RPC if configured, otherwise fall back to public RPC."""
        private_rpc = os.getenv("PRIVATE_RPC_URL") or os.getenv("FLASHBOTS_RPC")
        if (self.use_flashbots or os.getenv("USE_PRIVATE_RPC", "false").lower() == "true") and private_rpc:
            try:
                masked_rpc = private_rpc[:4] + "..." + private_rpc[-4:] if len(private_rpc) > 8 else "***"
                logger.info(f"Routing swap transaction privately via: {masked_rpc}...")
                private_w3 = Web3(Web3.HTTPProvider(private_rpc))
                return private_w3.eth.send_raw_transaction(raw_tx)
            except Exception as e:
                logger.warning(f"Private RPC submission failed: {e}. Falling back to public RPC...")
        return self.w3.eth.send_raw_transaction(raw_tx)

    def escalate_transaction_priority(self, base_gas_price: Dict, multiplier: float = 1.5) -> Dict[str, int]:
        """Escalate EIP-1559 gas fees to beat competing mempool transactions."""
        escalated_max_priority = int(base_gas_price.get("maxPriorityFeePerGas", 2000000000) * multiplier)
        escalated_max_fee = int(base_gas_price.get("maxFeePerGas", 10000000000) * multiplier)
        return {
            "maxPriorityFeePerGas": escalated_max_priority,
            "maxFeePerGas": escalated_max_fee,
            "gasLimit": base_gas_price.get("gasLimit", 300000)
        }

    async def execute_swap(
        self,
        fn,
        gas_price: Dict = None,
        retry_count: int = 3
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Execute swap with retry logic.
        Returns: (success, reason, tx_hash)
        """
        if not self.account:
            return (False, "No private key configured", None)
        
        if gas_price is None:
            gas_price = self.calculate_optimal_gas_price()
        
        for attempt in range(retry_count):
            try:
                # Estimate gas
                gas = fn.estimate_gas({
                    "from": self.account.address,
                    "maxPriorityFeePerGas": gas_price["maxPriorityFeePerGas"],
                    "maxFeePerGas": gas_price["maxFeePerGas"]
                })
                
                # Build transaction
                tx = fn.build_transaction({
                    "from": self.account.address,
                    "nonce": self.w3.eth.get_transaction_count(self.account.address),
                    "maxPriorityFeePerGas": gas_price["maxPriorityFeePerGas"],
                    "maxFeePerGas": gas_price["maxFeePerGas"],
                    "gas": int(gas * 1.2)  # 20% buffer
                })
                
                # Sign and send
                signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
                tx_hash = self.send_raw_transaction_safe(signed.rawTransaction)
                
                logger.info(f"Swap sent: {tx_hash.hex()}")
                return (True, "Swap executed", tx_hash.hex())
            
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"Attempt {attempt+1} failed: {error_msg}")
                
                # Retry with higher gas if underpriced
                if "underpriced" in error_msg.lower():
                    gas_price["maxPriorityFeePerGas"] = int(gas_price["maxPriorityFeePerGas"] * 1.5)
                    gas_price["maxFeePerGas"] = int(gas_price["maxFeePerGas"] * 1.5)
                
                if attempt < retry_count - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    return (False, f"Failed after {retry_count} attempts: {error_msg[:50]}", None)
        
        return (False, "Unknown error", None)

    # =========== POSITION ENTRY ===========
    def simulate_buy_roundtrip(
        self,
        token: str,
        buy_amount_eth: float,
        slippage_percent: float = 3.0
    ) -> Dict[str, any]:
        """
        Simulate full roundtrip (buy + sell) for cost analysis.
        """
        result = {
            "buy_amount_eth": buy_amount_eth,
            "slippage_percent": slippage_percent,
            "estimated_tokens": 0,
            "estimated_sell_eth": 0,
            "estimated_loss_eth": 0,
            "estimated_loss_percent": 0,
            "total_gas_eth": 0
        }
        
        # In real implementation, would use quoter here
        # This is simplified
        
        return result
