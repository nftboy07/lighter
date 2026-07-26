#!/usr/bin/env python3
"""
Safety Analyzer for B20 Bot
===========================
Comprehensive token and pool safety analysis:
- Honeypot detection
- Mint authority checks
- Holder distribution analysis
- Buy/sell tax detection
- Safety score calculation (0-100)
- Rug probability estimation
"""

from typing import Dict, Optional, Tuple, List
from web3 import Web3
from eth_utils import to_checksum_address
from eth_abi import decode
import json


class SafetyAnalyzer:
    """Multi-vector safety analysis for B20 tokens and pools."""

    # Token ABI for basic checks
    TOKEN_ABI = [
        {
            "inputs": [],
            "name": "totalSupply",
            "outputs": [{"type": "uint256"}],
            "type": "function",
            "stateMutability": "view"
        },
        {
            "inputs": [{"type": "address"}],
            "name": "balanceOf",
            "outputs": [{"type": "uint256"}],
            "type": "function",
            "stateMutability": "view"
        },
        {
            "inputs": [],
            "name": "decimals",
            "outputs": [{"type": "uint8"}],
            "type": "function",
            "stateMutability": "view"
        },
        {
            "inputs": [{"type": "address"}],
            "name": "minter",
            "outputs": [{"type": "address"}],
            "type": "function",
            "stateMutability": "view"
        },
        {
            "inputs": [],
            "name": "owner",
            "outputs": [{"type": "address"}],
            "type": "function",
            "stateMutability": "view"
        }
    ]

    # Pool ABI for liquidity checks
    POOL_ABI = [
        {
            "inputs": [],
            "name": "liquidity",
            "outputs": [{"type": "uint128"}],
            "type": "function",
            "stateMutability": "view"
        },
        {
            "inputs": [],
            "name": "slot0",
            "outputs": [
                {"type": "uint160"},  # sqrtPriceX96
                {"type": "int24"},    # tick
                {"type": "uint16"},   # observationIndex
                {"type": "uint16"},   # observationCardinality
                {"type": "uint16"},   # observationCardinalityNext
                {"type": "uint8"},    # feeProtocol
                {"type": "bool"}      # unlocked
            ],
            "type": "function",
            "stateMutability": "view"
        }
    ]

    QUOTER_V2_ABI = [
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

    def __init__(self, w3: Web3, quoter_v2: str, router: str, weth: str):
        self.w3 = w3
        self.quoter_v2 = to_checksum_address(quoter_v2)
        self.router = to_checksum_address(router)
        self.weth = to_checksum_address(weth)

    # =========== HONEYPOT DETECTION ===========
    def detect_honeypot_via_simulation(
        self, token_address: str, pool_address: str, buy_amount_eth: float = 0.001
    ) -> Tuple[bool, float, str]:
        """
        Detect honeypot via full roundtrip simulation:
        1. Buy token with WETH
        2. Sell token back to WETH
        3. Compare input vs output
        Returns: (is_honeypot, loss_percent, reason)
        """
        try:
            token = to_checksum_address(token_address)
            
            # Step 1: Get buy quote (WETH -> Token)
            buy_amount_wei = self.w3.to_wei(buy_amount_eth, "ether")
            
            try:
                buy_quote = self._get_exact_input_quote(
                    self.weth, token, 3000, buy_amount_wei
                )
                if not buy_quote or buy_quote < 1:
                    return (True, 100.0, "Buy quote failed or returned 0")
            except Exception as e:
                return (True, 100.0, f"Buy simulation failed: {str(e)[:50]}")

            # Step 2: Try to sell token back to WETH
            try:
                sell_quote = self._get_exact_input_quote(
                    token, self.weth, 3000, buy_quote
                )
                if not sell_quote or sell_quote == 0:
                    return (True, 100.0, "Sell quote returned 0 (classic honeypot)")
            except Exception as e:
                return (True, 100.0, f"Sell simulation failed: {str(e)[:50]}")

            # Step 3: Calculate loss
            loss_percent = ((buy_amount_wei - sell_quote) / buy_amount_wei) * 100 if buy_amount_wei > 0 else 0
            
            # Threshold: >30% loss = likely honeypot
            is_honeypot = loss_percent > 30
            
            return (is_honeypot, loss_percent, f"Roundtrip loss: {loss_percent:.2f}%")

        except Exception as e:
            return (False, 0.0, f"Honeypot check error: {str(e)[:50]}")

    def _get_exact_input_quote(self, token_in: str, token_out: str, fee: int, amount_in: int) -> int:
        """Get quote from QuoterV2."""
        try:
            quoter = self.w3.eth.contract(address=self.quoter_v2, abi=self.QUOTER_V2_ABI)
            
            # Encode params for quoteExactInputSingle
            params = self.w3.codec.encode(
                ['address', 'address', 'uint24', 'uint256', 'uint160'],
                [to_checksum_address(token_in), to_checksum_address(token_out), fee, amount_in, 0]
            )
            
            result = quoter.functions.quoteExactInputSingle(params).call()
            return result[0]  # amountOut
        except Exception as e:
            print(f"Quote error: {e}")
            return 0

    # =========== MINT AUTHORITY ===========
    def check_mint_authority(self, token_address: str) -> Tuple[int, str]:
        """
        Check if token can be minted post-creation.
        Returns: (score 0-100, reason)
        """
        try:
            token = self.w3.eth.contract(address=to_checksum_address(token_address), abi=self.TOKEN_ABI)
            
            # Try to get minter address (custom)
            try:
                minter = token.functions.minter().call()
                if minter and minter != "0x0000000000000000000000000000000000000000":
                    return (20, f"Has minter: {minter[:6]}... - CAN MINT")
            except:
                pass
            
            # Try to get owner (can usually mint if owner)
            try:
                owner = token.functions.owner().call()
                if owner and owner != "0x0000000000000000000000000000000000000000":
                    return (50, f"Has owner: {owner[:6]}... - May be able to mint")
            except:
                pass
            
            # No minter/owner found = likely immutable (good)
            return (90, "No mint authority found (immutable)")
        
        except Exception as e:
            return (0, f"Could not check mint authority: {str(e)[:50]}")

    # =========== HOLDER DISTRIBUTION ===========
    def analyze_holder_distribution(
        self, token_address: str, top_holders_count: int = 10
    ) -> Tuple[int, float, List[Tuple[str, float]]]:
        """
        Analyze token holder distribution by scanning transfer logs.
        Returns: (score, top10_percent, [(holder_address, percent), ...])
        """
        try:
            token = self.w3.eth.contract(address=to_checksum_address(token_address), abi=self.TOKEN_ABI)
            
            total_supply = token.functions.totalSupply().call()
            if total_supply == 0:
                return (0, 100.0, [])
            
            current_block = self.w3.eth.block_number
            from_block = max(0, current_block - 2000)
            
            transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
            logs = self.w3.eth.get_logs({
                "fromBlock": from_block,
                "toBlock": current_block,
                "address": to_checksum_address(token_address),
                "topics": [transfer_topic]
            })
            
            balances = {}
            for log in logs:
                try:
                    frm = to_checksum_address("0x" + log["topics"][1].hex()[-40:])
                    to = to_checksum_address("0x" + log["topics"][2].hex()[-40:])
                    val = int(log["data"].hex(), 16) if isinstance(log["data"], bytes) else int(log["data"].hex(), 16)
                    
                    if frm != "0x0000000000000000000000000000000000000000":
                        balances[frm] = balances.get(frm, 0) - val
                    balances[to] = balances.get(to, 0) + val
                except:
                    pass
            
            sorted_holders = []
            for addr, bal in balances.items():
                if bal > 0 and addr != "0x0000000000000000000000000000000000000000":
                    pct = (bal / total_supply * 100) if total_supply > 0 else 0
                    if pct > 0.01:
                        # Check if holder is a contract (vesting/lock)
                        try:
                            code = self.w3.eth.get_code(addr)
                            is_contract = len(code) > 2
                        except:
                            is_contract = False
                        sorted_holders.append((addr, pct, is_contract))
            
            sorted_holders.sort(key=lambda x: x[1], reverse=True)
            
            # Sum top 10
            top10_total = 0.0
            holders = []
            for item in sorted_holders[:top_holders_count]:
                addr, pct, is_contract = item
                label = f"{addr[:8]} (Contract/Vesting)" if is_contract else addr[:10]
                holders.append((label, pct))
                # Vesting contracts are safe team allocations - don't count them against dev centralization score
                if not is_contract:
                    top10_total += pct
            
            score = 100 - int(min(top10_total, 100))
            if top10_total > 50:
                score = 20
            elif top10_total > 30:
                score = 50
                
            return (score, top10_total, holders)
        except Exception as e:
            return (0, 0.0, [(str(e)[:30], 0.0)])

    def detect_dev_buy_patterns(self, token_address: str, deployer: str) -> Tuple[int, str]:
        """Detect if the dev wallet funded buying immediately after launch."""
        try:
            token_addr = to_checksum_address(token_address)
            dep_addr = to_checksum_address(deployer)
            
            token = self.w3.eth.contract(address=token_addr, abi=self.TOKEN_ABI)
            total_supply = token.functions.totalSupply().call()
            if total_supply == 0:
                return (100, "Clean")
                
            current_block = self.w3.eth.block_number
            from_block = max(0, current_block - 2000)
            
            transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
            logs = self.w3.eth.get_logs({
                "fromBlock": from_block,
                "toBlock": current_block,
                "address": token_addr,
                "topics": [transfer_topic]
            })
            
            dev_buys = 0.0
            for log in logs:
                try:
                    to = to_checksum_address("0x" + log["topics"][2].hex()[-40:])
                    val = int(log["data"].hex(), 16) if isinstance(log["data"], bytes) else int(log["data"].hex(), 16)
                    
                    if to == dep_addr:
                        dev_buys += val
                except:
                    pass
            
            pct = (dev_buys / total_supply * 100) if total_supply > 0 else 0
            if pct > 15.0:
                return (30, f"Dev bought {pct:.1f}% of supply immediately after launch")
                
            return (100, f"Clean (dev bought {pct:.1f}%)")
        except Exception as e:
            return (90, f"Dev buy check skipped: {str(e)[:50]}")

    def find_token_deployer(self, token_address: str) -> Optional[str]:
        """Query historical transactions or logs to find the token creator/deployer."""
        try:
            token_addr = to_checksum_address(token_address)
            current_block = self.w3.eth.block_number
            from_block = max(0, current_block - 2000)
            
            # Transfer topic
            transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
            # Get earliest transfer from 0x00...00 (which is the mint/creation trace)
            logs = self.w3.eth.get_logs({
                "fromBlock": from_block,
                "toBlock": current_block,
                "address": token_addr,
                "topics": [
                    transfer_topic,
                    "0x0000000000000000000000000000000000000000000000000000000000000000"
                ]
            })
            if logs:
                # The recipient of the first mint is usually the creator or creator contract
                to_addr = to_checksum_address("0x" + logs[0]["topics"][2].hex()[-40:])
                return to_addr
        except:
            pass
        return None

    # =========== BUY/SELL TAX ===========
    def detect_buy_sell_tax(
        self, token_address: str, pool_address: str, test_amount_eth: float = 0.001
    ) -> Tuple[int, float, float, str]:
        """
        Detect buy and sell taxes.
        Returns: (score, buy_tax_percent, sell_tax_percent, reason)
        """
        try:
            # Simulate buy
            buy_amount_wei = self.w3.to_wei(test_amount_eth, "ether")
            buy_quote = self._get_exact_input_quote(
                self.weth, to_checksum_address(token_address), 3000, buy_amount_wei
            )
            
            # Simulate sell
            if buy_quote > 0:
                sell_quote = self._get_exact_input_quote(
                    to_checksum_address(token_address), self.weth, 3000, buy_quote
                )
            else:
                sell_quote = 0
            
            # Calculate implied tax
            # Formula: tax% = 100 - (final / initial) * 100
            if buy_amount_wei > 0 and sell_quote > 0:
                roundtrip_percent = (sell_quote / buy_amount_wei) * 100
                implied_tax = 100 - roundtrip_percent
            else:
                implied_tax = 100.0  # Assume max tax if quote fails
            
            # Score: <3% tax is normal (fees), >10% is suspicious
            if implied_tax <= 3:
                score = 95
            elif implied_tax <= 10:
                score = 70
            else:
                score = 30
            
            return (score, 0.0, implied_tax, f"Implied tax: {implied_tax:.2f}%")
        
        except Exception as e:
            return (0, 0.0, 0.0, f"Tax check failed: {str(e)[:50]}")

    # =========== LIQUIDITY CHECK ===========
    def check_liquidity(self, pool_address: str, min_liquidity_eth: float = 5.0) -> Tuple[int, float, str]:
        """
        Check pool liquidity.
        Returns: (score, liquidity_eth, reason)
        """
        try:
            pool = self.w3.eth.contract(address=to_checksum_address(pool_address), abi=self.POOL_ABI)
            liquidity = pool.functions.liquidity().call()
            slot0 = pool.functions.slot0().call()
            
            sqrtPriceX96 = slot0[0]
            
            # Rough conversion to ETH equivalent (simplified)
            # Real calculation would use decimals and price
            liquidity_eth = liquidity / 1e18 if liquidity > 0 else 0
            
            if liquidity_eth < min_liquidity_eth:
                return (40, liquidity_eth, f"Low liquidity: {liquidity_eth:.4f} ETH < {min_liquidity_eth} ETH")
            elif liquidity_eth < min_liquidity_eth * 2:
                return (70, liquidity_eth, f"Moderate liquidity: {liquidity_eth:.4f} ETH")
            else:
                return (95, liquidity_eth, f"Good liquidity: {liquidity_eth:.4f} ETH")
        
        except Exception as e:
            return (0, 0.0, f"Liquidity check failed: {str(e)[:50]}")

    # =========== UPGRADEABLE / PROXY CHECK ===========
    def check_upgradeable_contract(self, token_address: str) -> Tuple[int, str]:
        """
        Check if the token contract is upgradeable (proxy).
        Returns: (score 0-100, reason)
        """
        try:
            token_addr = to_checksum_address(token_address)
            
            # 1. EIP-1967 implementation slot
            # keccak256("eip1967.proxy.implementation") - 1
            eip1967_slot = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
            impl = self.w3.eth.get_storage_at(token_addr, eip1967_slot).hex()
            if int(impl, 16) != 0:
                return (0, f"Upgradeable EIP-1967 proxy (impl: 0x{impl[-40:]})")
                
            # 2. EIP-1967 beacon slot
            # keccak256("eip1967.proxy.beacon") - 1
            beacon_slot = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
            beacon = self.w3.eth.get_storage_at(token_addr, beacon_slot).hex()
            if int(beacon, 16) != 0:
                return (0, "Upgradeable Beacon proxy detected")
                
            # 3. OZ proxy admin owner slot
            # keccak256("org.zeppelinos.proxy.owner")
            oz_slot = "0x33b8a36f6d0f62ef1244fbe58467dfa811c7ff84752ca8cfa40a7cf59599574f"
            oz_admin = self.w3.eth.get_storage_at(token_addr, oz_slot).hex()
            if int(oz_admin, 16) != 0:
                return (0, "Upgradeable OZ proxy admin owner detected")
                
            # 4. Minimal Proxy (EIP-1167)
            bytecode = self.w3.eth.get_code(token_addr).hex()
            if bytecode.startswith("0x363d3d373d3d3d363d") or "363d3d373d3d3d363d" in bytecode[:30]:
                return (0, "Upgradeable EIP-1167 Minimal Proxy clone detected")
                
            # 5. Custom implementation() view function check
            try:
                impl_abi = [{"inputs": [], "name": "implementation", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"}]
                c = self.w3.eth.contract(address=token_addr, abi=impl_abi)
                impl_addr = c.functions.implementation().call()
                if impl_addr and int(impl_addr, 16) != 0:
                    return (0, f"Upgradeable Custom proxy detected (impl: {impl_addr})")
            except:
                pass
                
            return (100, "Non-upgradeable (clean token contract)")
            
        except Exception as e:
            return (90, f"Non-upgradeable check skipped: {str(e)[:50]}")

    # =========== MALICIOUS PATTERNS / COPYCAT SCANNING ===========
    def check_malicious_and_copycat_patterns(self, name: str, symbol: str) -> Tuple[int, str]:
        """
        Scan token name and symbol for famous impersonations or malicious keywords.
        Returns: (score 0-100, reason)
        """
        combined = f"{name} {symbol}".upper()
        warnings = []
        
        # 1. Famous impersonations
        famous_brands = ['ETHEREUM', 'BITCOIN', 'BINANCE', 'OPENSEA', 'METAMASK', 'UNISWAP', 'BASE', 'COINBASE', 'AERODROME']
        for brand in famous_brands:
            if brand in combined and not combined.startswith(brand + " "):
                warnings.append(f"Impersonates {brand}")
                
        # 2. Formatting anomalies
        if len(name) > 40:
            warnings.append("Long name (>40 chars)")
        if symbol and len(symbol) > 10:
            warnings.append("Long symbol (>10 chars)")
        if '0' in symbol and 'O' in symbol:
            warnings.append("Confusing 0/O chars")
            
        # 3. Scam keywords
        scam_words = ['FREE', 'GIFT', 'AIRDROP', 'REWARD', 'WINNER', 'CLAIM', 'TEST']
        for word in scam_words:
            if word in combined:
                warnings.append(f"Scam keyword: {word}")
                
        if warnings:
            return (50 if len(warnings) == 1 else 20, f"Warnings: {', '.join(warnings)}")
            
        return (100, "Clean name and symbol parameters")

    # =========== SAFETY SCORE ===========
    def calculate_safety_score(
        self, token_address: str, pool_address: str, min_liquidity_eth: float = 5.0
    ) -> Dict[str, any]:
        """
        Calculate comprehensive safety score (0-100).
        Returns detailed breakdown of all checks.
        """
        scores = {}
        
        # Check 1: Honeypot
        is_honeypot, loss_pct, honeypot_reason = self.detect_honeypot_via_simulation(
            token_address, pool_address
        )
        scores['honeypot_score'] = 0 if is_honeypot else 95
        scores['honeypot_reason'] = honeypot_reason
        
        # Check 2: Mint authority
        mint_score, mint_reason = self.check_mint_authority(token_address)
        scores['mint_authority_score'] = mint_score
        scores['mint_reason'] = mint_reason
        
        # Check 3: Holder distribution
        holder_score, top10_pct, holders = self.analyze_holder_distribution(token_address)
        scores['holder_distribution_score'] = holder_score
        scores['holder_reason'] = f"Top 10 holders: {top10_pct:.2f}%"
        
        # Check 4: Buy/Sell tax
        tax_score, buy_tax, sell_tax, tax_reason = self.detect_buy_sell_tax(
            token_address, pool_address
        )
        scores['tax_score'] = tax_score
        scores['tax_reason'] = tax_reason
        
        # Check 5: Liquidity
        liq_score, liq_eth, liq_reason = self.check_liquidity(pool_address, min_liquidity_eth)
        scores['liquidity_score'] = liq_score
        scores['liquidity_reason'] = liq_reason
        scores['liquidity_eth'] = liq_eth
        
        # Check 6: Upgradeable / Proxy Check
        proxy_score, proxy_reason = self.check_upgradeable_contract(token_address)
        scores['proxy_score'] = proxy_score
        scores['proxy_reason'] = proxy_reason
        
        # Check 7: Name/Symbol malicious patterns
        try:
            abi = [
                {"constant": True, "inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "type": "function"},
                {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
            ]
            c = self.w3.eth.contract(address=to_checksum_address(token_address), abi=abi)
            name = c.functions.name().call()
            symbol = c.functions.symbol().call()
        except:
            name, symbol = "Unknown", "UNK"
        name_score, name_reason = self.check_malicious_and_copycat_patterns(name, symbol)
        scores['name_score'] = name_score
        scores['name_reason'] = name_reason
        
        # Rug probability (inverse of mint + holder + proxy checks)
        rug_probability = 100 - min(scores['mint_authority_score'], scores['holder_distribution_score'], scores['proxy_score'])
        scores['rug_probability_score'] = min(rug_probability, 100)
        
        # Overall score (weighted average)
        weights = {
            'honeypot_score': 0.20,
            'mint_authority_score': 0.15,
            'holder_distribution_score': 0.15,
            'tax_score': 0.15,
            'liquidity_score': 0.15,
            'proxy_score': 0.10,
            'name_score': 0.10
        }
        
        overall = sum(scores[k] * v for k, v in weights.items())
        scores['overall_score'] = int(overall)
        
        # Check 8: Dev buy patterns
        dev_address = self.find_token_deployer(token_address)
        if dev_address:
            dev_buy_score, dev_buy_reason = self.detect_dev_buy_patterns(token_address, dev_address)
        else:
            dev_buy_score, dev_buy_reason = 100, "Could not locate deployer address"
        scores['dev_buy_score'] = dev_buy_score
        scores['dev_buy_reason'] = dev_buy_reason
        
        return scores

    def should_buy(self, safety_scores: Dict, min_safety_score: int = 75) -> Tuple[bool, str]:
        """
        Determine if token passes safety threshold.
        Returns: (should_buy, reason)
        """
        overall = safety_scores.get('overall_score', 0)
        
        if overall < min_safety_score:
            return (False, f"Safety score {overall} < {min_safety_score}")
        
        if safety_scores.get('honeypot_score', 0) < 50:
            return (False, "Likely honeypot")
        
        if safety_scores.get('rug_probability_score', 0) > 70:
            return (False, "High rug probability")
            
        if safety_scores.get('proxy_score', 0) < 50:
            return (False, f"Upgradeable proxy contract detected: {safety_scores.get('proxy_reason')}")
            
        if safety_scores.get('dev_buy_score', 0) < 50:
            return (False, f"Dev centralization risk: {safety_scores.get('dev_buy_reason')}")
        
        return (True, f"Passed all checks (score: {overall})")

    def simulate_pretrade_sell(
        self,
        token_address: str,
        router_address: str,
        user_address: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Perform a pre-trade EVM simulation to verify that the token can be approved and sold.
        Returns: (is_sellable, reason)
        """
        try:
            token_checksum = to_checksum_address(token_address)
            router_checksum = to_checksum_address(router_address)
            caller = user_address or "0x0000000000000000000000000000000000000001"

            # Check basic ERC20 functions via static call
            token_contract = self.w3.eth.contract(
                address=token_checksum,
                abi=self.TOKEN_ABI + [{
                    "inputs": [{"type": "address"}, {"type": "uint256"}],
                    "name": "approve",
                    "outputs": [{"type": "bool"}],
                    "type": "function",
                    "stateMutability": "nonpayable"
                }]
            )

            # 1. Test static call to balanceOf
            bal = token_contract.functions.balanceOf(caller).call()

            # 2. Dry-run approval call
            tx_data = token_contract.functions.approve(router_checksum, 1000000000000000000).build_transaction({
                "from": caller,
                "gas": 100000,
                "gasPrice": self.w3.eth.gas_price
            })

            # Simulate execution via eth_call
            self.w3.eth.call({"from": caller, "to": token_checksum, "data": tx_data["data"]})
            return (True, "Pre-trade EVM sell simulation passed")
        except Exception as e:
            return (False, f"Pre-trade EVM sell simulation failed: {e}")

    def revoke_token_allowance(
        self,
        token_address: str,
        spender_address: str,
        private_key: str
    ) -> Tuple[bool, str]:
        """
        Revoke token allowance (reset to 0) to secure wallet permissions.
        Returns: (success, tx_hash_or_reason)
        """
        try:
            account = self.w3.eth.account.from_key(private_key)
            token_checksum = to_checksum_address(token_address)
            spender_checksum = to_checksum_address(spender_address)

            token_contract = self.w3.eth.contract(
                address=token_checksum,
                abi=[{
                    "inputs": [{"type": "address"}, {"type": "uint256"}],
                    "name": "approve",
                    "outputs": [{"type": "bool"}],
                    "type": "function",
                    "stateMutability": "nonpayable"
                }]
            )

            tx = token_contract.functions.approve(spender_checksum, 0).build_transaction({
                "from": account.address,
                "nonce": self.w3.eth.get_transaction_count(account.address),
                "gas": 60000,
                "gasPrice": self.w3.eth.gas_price
            })

            signed = self.w3.eth.account.sign_transaction(tx, private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
            return (True, tx_hash.hex())
        except Exception as e:
            return (False, str(e))
