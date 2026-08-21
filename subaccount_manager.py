#!/usr/bin/env python3
"""
Multi-Subaccount Strategy Sharding & Collateral Rebalancer for zkLighter
========================================================================
Shards trading operations across dedicated subaccounts:
1. Subaccount #737649 (Sniper): Dedicated for high-speed news & catalyst sniping + manual quick orders.
2. Subaccount MM (Market Maker / Points Farming): Dedicated for continuous two-sided quoting & volume farming.
3. Subaccount Arb (Cross-DEX Arbitrage / Funding Harvester): Dedicated for price-lag arbitrage & funding rate harvesting.

Provides:
- Multi-subaccount routing for order dispatch
- Real-time collateral & margin tracking across subaccounts
- Automated collateral drift calculation and rebalancing recommendations
- Transfer execution helpers
- Institutional summary & status reports for Telegram & monitoring
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import aiohttp

logger = logging.getLogger("SubaccountManager")


class SubaccountRole(str, Enum):
    """Trading strategy role assigned to a zkLighter subaccount."""
    SNIPER = "SNIPER"                  # Catalyst / News Sniping & One-Tap Manual Quick Trades
    MARKET_MAKER = "MARKET_MAKER"      # Avellaneda-Stoikov Quoter & Robinhood Points Farmer
    ARBITRAGE = "ARBITRAGE"            # Cross-DEX Hyperliquid Arbitrage & Funding Harvester
    TREASURY = "TREASURY"              # Reserve / Collateral Buffer Pool


@dataclass
class SubaccountProfile:
    """Configuration profile for a specific subaccount shard."""
    role: SubaccountRole
    account_index: int
    name: str
    description: str
    api_key_index: int = 5
    target_allocation_pct: float = 40.0  # Target % of total collateral pool
    min_collateral_usd: float = 1.0      # Minimum floor before alert
    max_leverage: float = 10.0
    enabled: bool = True


@dataclass
class SubaccountState:
    """Live telemetry and collateral state for a subaccount shard."""
    account_index: int
    role: SubaccountRole
    name: str
    collateral_usd: float = 0.0
    available_margin_usd: float = 0.0
    allocated_margin_usd: float = 0.0
    margin_utilization_pct: float = 0.0
    active_positions_count: int = 0
    pending_orders_count: int = 0
    total_volume_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    status: str = "Active"
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_index": self.account_index,
            "role": self.role.value,
            "name": self.name,
            "collateral_usd": round(self.collateral_usd, 4),
            "available_margin_usd": round(self.available_margin_usd, 4),
            "allocated_margin_usd": round(self.allocated_margin_usd, 4),
            "margin_utilization_pct": round(self.margin_utilization_pct, 2),
            "active_positions_count": self.active_positions_count,
            "pending_orders_count": self.pending_orders_count,
            "total_volume_usd": round(self.total_volume_usd, 2),
            "unrealized_pnl_usd": round(self.unrealized_pnl_usd, 4),
            "realized_pnl_usd": round(self.realized_pnl_usd, 4),
            "status": self.status,
            "last_updated": self.last_updated,
        }


@dataclass
class RebalanceRecommendation:
    """Actionable collateral transfer recommendation between subaccounts."""
    from_account_index: int
    from_role: SubaccountRole
    from_name: str
    to_account_index: int
    to_role: SubaccountRole
    to_name: str
    amount_usd: float
    current_ratio_from: float
    target_ratio_from: float
    current_ratio_to: float
    target_ratio_to: float
    reason: str
    urgency: str = "MEDIUM"  # LOW, MEDIUM, HIGH, CRITICAL
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_account_index": self.from_account_index,
            "from_role": self.from_role.value,
            "from_name": self.from_name,
            "to_account_index": self.to_account_index,
            "to_role": self.to_role.value,
            "to_name": self.to_name,
            "amount_usd": round(self.amount_usd, 4),
            "current_ratio_from": round(self.current_ratio_from, 4),
            "target_ratio_from": round(self.target_ratio_from, 4),
            "current_ratio_to": round(self.current_ratio_to, 4),
            "target_ratio_to": round(self.target_ratio_to, 4),
            "reason": self.reason,
            "urgency": self.urgency,
            "timestamp": self.timestamp,
        }


class SubaccountManager:
    """
    Institutional Multi-Subaccount Strategy Sharding & Portfolio Rebalancer.
    Manages routing, collateral isolation, and automated rebalancing.
    """

    DEFAULT_SNIPER_INDEX = 737649
    DEFAULT_MM_INDEX = 737650
    DEFAULT_ARB_INDEX = 737651

    def __init__(
        self,
        profiles: Optional[Dict[SubaccountRole, SubaccountProfile]] = None,
        base_url: Optional[str] = None,
        wallet_address: Optional[str] = None,
    ):
        self.base_url = (base_url or os.getenv("LIGHTER_BASE_URL") or "https://mainnet.zklighter.elliot.ai").rstrip("/")
        self.wallet_address = (wallet_address or os.getenv("WALLET_ADDRESS") or "0x5cE95F8F7594c082549B34A32c26f4bf2F1bcFe9").strip()

        # Load subaccount profiles or construct institutional defaults
        self.profiles: Dict[SubaccountRole, SubaccountProfile] = {}
        self.states: Dict[int, SubaccountState] = {}
        self._role_to_index: Dict[SubaccountRole, int] = {}
        self._index_to_role: Dict[int, SubaccountRole] = {}

        if profiles:
            for role, prof in profiles.items():
                self.register_subaccount(prof)
        else:
            self._init_default_profiles()

    def _init_default_profiles(self) -> None:
        """Initializes standard strategy shard profiles from environment or defaults."""
        sniper_idx = int(os.getenv("LIGHTER_SNIPER_ACCOUNT_INDEX") or os.getenv("LIGHTER_ACCOUNT_INDEX") or self.DEFAULT_SNIPER_INDEX)
        mm_idx = int(os.getenv("LIGHTER_MM_ACCOUNT_INDEX") or self.DEFAULT_MM_INDEX)
        arb_idx = int(os.getenv("LIGHTER_ARB_ACCOUNT_INDEX") or self.DEFAULT_ARB_INDEX)
        api_key_idx = int(os.getenv("LIGHTER_API_KEY_INDEX") or "5")

        default_list = [
            SubaccountProfile(
                role=SubaccountRole.SNIPER,
                account_index=sniper_idx,
                name="Sniper Shard",
                description="Sub-5ms Catalyst Sniping & One-Tap Manual Quick Trades",
                api_key_index=api_key_idx,
                target_allocation_pct=40.0,
                min_collateral_usd=1.0,
                max_leverage=10.0,
            ),
            SubaccountProfile(
                role=SubaccountRole.MARKET_MAKER,
                account_index=mm_idx,
                name="Market Maker Shard",
                description="Avellaneda-Stoikov 2-Sided Quoting & Robinhood Points Farming",
                api_key_index=api_key_idx,
                target_allocation_pct=40.0,
                min_collateral_usd=1.0,
                max_leverage=5.0,
            ),
            SubaccountProfile(
                role=SubaccountRole.ARBITRAGE,
                account_index=arb_idx,
                name="Arbitrage Shard",
                description="Cross-DEX Hyperliquid Latency Arbitrage & Funding Harvester",
                api_key_index=api_key_idx,
                target_allocation_pct=20.0,
                min_collateral_usd=0.5,
                max_leverage=8.0,
            ),
        ]

        for prof in default_list:
            self.register_subaccount(prof)

    def register_subaccount(self, profile: SubaccountProfile) -> None:
        """Registers or updates a subaccount profile."""
        self.profiles[profile.role] = profile
        self._role_to_index[profile.role] = profile.account_index
        self._index_to_role[profile.account_index] = profile.role

        if profile.account_index not in self.states:
            self.states[profile.account_index] = SubaccountState(
                account_index=profile.account_index,
                role=profile.role,
                name=profile.name,
                collateral_usd=5.5208 if profile.role == SubaccountRole.SNIPER else 0.0,
                available_margin_usd=5.5208 if profile.role == SubaccountRole.SNIPER else 0.0,
                status="Active",
                last_updated=time.time(),
            )

    def get_subaccount(self, identifier: Union[SubaccountRole, str, int]) -> Optional[SubaccountProfile]:
        """Resolves subaccount profile by role, name, or account index."""
        if isinstance(identifier, SubaccountRole):
            return self.profiles.get(identifier)

        if isinstance(identifier, int):
            role = self._index_to_role.get(identifier)
            return self.profiles.get(role) if role else None

        if isinstance(identifier, str):
            clean = identifier.strip().upper()
            for role, prof in self.profiles.items():
                if (
                    clean == role.value
                    or clean == prof.name.upper()
                    or clean in prof.name.upper()
                    or clean == str(prof.account_index)
                ):
                    return prof

        return None

    def get_state(self, identifier: Union[SubaccountRole, str, int]) -> Optional[SubaccountState]:
        """Gets current state for a given subaccount."""
        prof = self.get_subaccount(identifier)
        if prof and prof.account_index in self.states:
            return self.states[prof.account_index]
        if isinstance(identifier, int) and identifier in self.states:
            return self.states[identifier]
        return None

    def route_strategy(self, strategy: Union[SubaccountRole, str]) -> SubaccountProfile:
        """
        Routes an incoming trading order or strategy action to its dedicated subaccount shard.
        - 'sniper', 'news', 'catalyst', 'quick', 'manual', 'long', 'short' -> Subaccount #737649 (Sniper)
        - 'mm', 'market_maker', 'quoter', 'points', 'farming' -> Subaccount MM
        - 'arb', 'arbitrage', 'cross_dex', 'funding', 'harvester' -> Subaccount Arb
        """
        if isinstance(strategy, SubaccountRole):
            prof = self.profiles.get(strategy)
            if prof:
                return prof

        clean = str(strategy).strip().lower()

        if any(k in clean for k in ["sniper", "news", "catalyst", "quick", "manual", "trigger", "long", "short", "737649"]):
            return self.profiles.get(SubaccountRole.SNIPER, list(self.profiles.values())[0])

        if any(k in clean for k in ["mm", "market_maker", "marketmaker", "quoter", "points", "farming", "spread"]):
            return self.profiles.get(SubaccountRole.MARKET_MAKER, list(self.profiles.values())[0])

        if any(k in clean for k in ["arb", "arbitrage", "cross_dex", "hyperliquid", "funding", "harvest", "basis"]):
            return self.profiles.get(SubaccountRole.ARBITRAGE, list(self.profiles.values())[0])

        # Fallback to sniper default
        return self.profiles.get(SubaccountRole.SNIPER, list(self.profiles.values())[0])

    def update_state(
        self,
        account_index: int,
        collateral_usd: Optional[float] = None,
        available_margin_usd: Optional[float] = None,
        allocated_margin_usd: Optional[float] = None,
        active_positions_count: Optional[int] = None,
        pending_orders_count: Optional[int] = None,
        total_volume_usd: Optional[float] = None,
        unrealized_pnl_usd: Optional[float] = None,
        realized_pnl_usd: Optional[float] = None,
        status: Optional[str] = None,
    ) -> SubaccountState:
        """Updates internal subaccount telemetry."""
        role = self._index_to_role.get(account_index, SubaccountRole.SNIPER)
        prof = self.profiles.get(role)
        name = prof.name if prof else f"Subaccount #{account_index}"

        if account_index not in self.states:
            self.states[account_index] = SubaccountState(
                account_index=account_index,
                role=role,
                name=name,
            )

        st = self.states[account_index]
        if collateral_usd is not None:
            st.collateral_usd = max(0.0, float(collateral_usd))
        if available_margin_usd is not None:
            st.available_margin_usd = max(0.0, float(available_margin_usd))
        if allocated_margin_usd is not None:
            st.allocated_margin_usd = max(0.0, float(allocated_margin_usd))
        else:
            st.allocated_margin_usd = max(0.0, st.collateral_usd - st.available_margin_usd)

        if st.collateral_usd > 0:
            st.margin_utilization_pct = min(100.0, (st.allocated_margin_usd / st.collateral_usd) * 100.0)
        else:
            st.margin_utilization_pct = 0.0

        if active_positions_count is not None:
            st.active_positions_count = int(active_positions_count)
        if pending_orders_count is not None:
            st.pending_orders_count = int(pending_orders_count)
        if total_volume_usd is not None:
            st.total_volume_usd = float(total_volume_usd)
        if unrealized_pnl_usd is not None:
            st.unrealized_pnl_usd = float(unrealized_pnl_usd)
        if realized_pnl_usd is not None:
            st.realized_pnl_usd = float(realized_pnl_usd)
        if status is not None:
            st.status = status

        st.last_updated = time.time()
        return st

    async def fetch_subaccount_balances(
        self, session: Optional[aiohttp.ClientSession] = None
    ) -> Dict[int, SubaccountState]:
        """
        Fetches live account balances from zkLighter API for all managed subaccounts.
        Gracefully handles offline or mock scenarios.
        """
        url = f"{self.base_url}/api/v1/accountsByL1Address?l1_address={self.wallet_address}"
        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=4.0)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    sub_accs = data.get("sub_accounts", [])
                    for acc in sub_accs:
                        idx = int(acc.get("index", 0))
                        collateral = float(acc.get("collateral", "0.0"))
                        pending = int(acc.get("pending_order_count", 0))
                        status_code = acc.get("status", 1)
                        status_str = "Active" if status_code == 1 else str(status_code)

                        # Update matching or create state
                        self.update_state(
                            account_index=idx,
                            collateral_usd=collateral,
                            available_margin_usd=collateral,
                            pending_orders_count=pending,
                            status=status_str,
                        )
        except Exception as e:
            logger.debug(f"[SubaccountManager] Fetch balance error: {e}")
        finally:
            if close_session:
                await session.close()

        return self.states

    def get_total_portfolio_value(self) -> float:
        """Calculates total collateral across all managed subaccounts."""
        return sum(st.collateral_usd for st in self.states.values())

    def calculate_rebalancing(
        self,
        drift_threshold_pct: float = 0.15,
        min_transfer_usd: float = 0.50,
    ) -> List[RebalanceRecommendation]:
        """
        Analyzes collateral distribution across all subaccounts vs target allocation percentages.
        Generates actionable rebalance recommendations if drift exceeds threshold or minimum floor is violated.
        """
        total_collateral = self.get_total_portfolio_value()
        if total_collateral <= 0.0:
            return []

        recommendations: List[RebalanceRecommendation] = []
        surplus_accounts: List[Tuple[SubaccountProfile, SubaccountState, float]] = []
        deficit_accounts: List[Tuple[SubaccountProfile, SubaccountState, float]] = []

        # 1. Compute target USD and drift for each profile
        for role, prof in self.profiles.items():
            st = self.states.get(prof.account_index)
            if not st:
                continue

            current_val = st.collateral_usd
            current_ratio = current_val / total_collateral if total_collateral > 0 else 0.0
            target_ratio = prof.target_allocation_pct / 100.0
            target_val = total_collateral * target_ratio

            diff_usd = current_val - target_val
            drift_pct = abs(current_ratio - target_ratio) / (target_ratio if target_ratio > 0 else 1.0)

            # Check critical floor violation
            if current_val < prof.min_collateral_usd and target_val >= prof.min_collateral_usd:
                deficit_accounts.append((prof, st, abs(diff_usd)))
            elif diff_usd < 0 and drift_pct >= drift_threshold_pct and abs(diff_usd) >= min_transfer_usd:
                deficit_accounts.append((prof, st, abs(diff_usd)))
            elif diff_usd > 0 and drift_pct >= drift_threshold_pct and diff_usd >= min_transfer_usd:
                surplus_accounts.append((prof, st, diff_usd))

        # 2. Pair surplus accounts with deficit accounts
        for d_prof, d_st, d_amount in deficit_accounts:
            needed = d_amount
            for s_prof, s_st, s_amount in surplus_accounts:
                if needed <= 0:
                    break
                if s_amount <= 0:
                    continue

                transfer_amount = min(needed, s_amount)
                if transfer_amount < min_transfer_usd:
                    continue

                is_critical = d_st.collateral_usd < d_prof.min_collateral_usd
                urgency = "CRITICAL" if is_critical else ("HIGH" if transfer_amount > total_collateral * 0.25 else "MEDIUM")
                reason = (
                    f"Subaccount #{d_st.account_index} ({d_prof.name}) below minimum threshold (${d_st.collateral_usd:.2f} < ${d_prof.min_collateral_usd:.2f})"
                    if is_critical
                    else f"Collateral drift rebalance: align to target {d_prof.target_allocation_pct:.0f}% allocation"
                )

                rec = RebalanceRecommendation(
                    from_account_index=s_st.account_index,
                    from_role=s_prof.role,
                    from_name=s_prof.name,
                    to_account_index=d_st.account_index,
                    to_role=d_prof.role,
                    to_name=d_prof.name,
                    amount_usd=round(transfer_amount, 4),
                    current_ratio_from=s_st.collateral_usd / total_collateral,
                    target_ratio_from=s_prof.target_allocation_pct / 100.0,
                    current_ratio_to=d_st.collateral_usd / total_collateral,
                    target_ratio_to=d_prof.target_allocation_pct / 100.0,
                    reason=reason,
                    urgency=urgency,
                )
                recommendations.append(rec)
                needed -= transfer_amount

        return recommendations

    async def transfer_collateral(
        self,
        from_account_index: int,
        to_account_index: int,
        amount_usd: float,
        is_paper: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes internal collateral transfer between subaccounts on zkLighter.
        """
        if amount_usd <= 0:
            return {"success": False, "error": "Invalid amount"}

        from_st = self.states.get(from_account_index)
        to_st = self.states.get(to_account_index)

        if not from_st:
            return {"success": False, "error": f"From subaccount #{from_account_index} not found"}
        if not to_st:
            return {"success": False, "error": f"To subaccount #{to_account_index} not found"}

        if from_st.available_margin_usd < amount_usd:
            return {
                "success": False,
                "error": f"Insufficient available collateral in #{from_account_index}: ${from_st.available_margin_usd:.2f} < ${amount_usd:.2f}",
            }

        # In-memory balance shift
        from_st.collateral_usd = max(0.0, from_st.collateral_usd - amount_usd)
        from_st.available_margin_usd = max(0.0, from_st.available_margin_usd - amount_usd)
        to_st.collateral_usd += amount_usd
        to_st.available_margin_usd += amount_usd
        from_st.last_updated = time.time()
        to_st.last_updated = time.time()

        tx_hash = f"0xsim_transfer_{int(time.time()*1000)}_{from_account_index}_{to_account_index}"
        
        # If live and signer is provided, execute on-chain transfer
        if not is_paper and hasattr(self, "signer_client") and self.signer_client:
            try:
                prof = self.profiles.get(from_st.role)
                api_idx = prof.api_key_index if prof else self.default_api_key_index
                # Asset ID 3 = USDC, Route 1 = ROUTE_PERP
                if hasattr(self.signer_client, "transfer_same_master_account"):
                    res = await self.signer_client.transfer_same_master_account(
                        to_account_index=to_account_index,
                        asset_id=3,
                        route_from=1,
                        route_to=1,
                        amount=amount_usd,
                        fee=0,
                        memo=f"rebalance_to_{to_account_index}",
                        api_key_index=api_idx,
                    )
                    tx_hash = str(getattr(res, "tx_hash", None) or res or tx_hash)
            except Exception as e:
                logger.warning(f"[SubaccountManager] Live on-chain transfer warning: {e}")

        logger.info(
            f"💰 [SubaccountManager] Transferred ${amount_usd:.2f} USD from #{from_account_index} ({from_st.name}) "
            f"to #{to_account_index} ({to_st.name}). Tx: {tx_hash}"
        )

        return {
            "success": True,
            "tx_hash": str(tx_hash),
            "from_account_index": from_account_index,
            "to_account_index": to_account_index,
            "amount_usd": amount_usd,
            "is_paper": is_paper,
        }

    def get_portfolio_summary(self) -> Dict[str, Any]:
        """Returns consolidated multi-subaccount portfolio telemetry."""
        total_collateral = self.get_total_portfolio_value()
        total_available = sum(st.available_margin_usd for st in self.states.values())
        total_allocated = sum(st.allocated_margin_usd for st in self.states.values())
        total_volume = sum(st.total_volume_usd for st in self.states.values())
        total_unrealized_pnl = sum(st.unrealized_pnl_usd for st in self.states.values())
        total_realized_pnl = sum(st.realized_pnl_usd for st in self.states.values())
        total_positions = sum(st.active_positions_count for st in self.states.values())

        shards = []
        for prof in self.profiles.values():
            st = self.states.get(prof.account_index)
            if st:
                data = st.to_dict()
                data["target_allocation_pct"] = prof.target_allocation_pct
                data["actual_allocation_pct"] = round((st.collateral_usd / total_collateral * 100.0) if total_collateral > 0 else 0.0, 2)
                shards.append(data)

        return {
            "total_collateral_usd": round(total_collateral, 4),
            "total_available_margin_usd": round(total_available, 4),
            "total_allocated_margin_usd": round(total_allocated, 4),
            "total_volume_usd": round(total_volume, 2),
            "total_unrealized_pnl_usd": round(total_unrealized_pnl, 4),
            "total_realized_pnl_usd": round(total_realized_pnl, 4),
            "total_positions_count": total_positions,
            "subaccounts_count": len(self.profiles),
            "shards": shards,
            "wallet_address": self.wallet_address,
        }

    def format_subaccounts_report_html(self) -> str:
        """Formats institutional multi-subaccount status report for Telegram."""
        summary = self.get_portfolio_summary()
        total_collat = summary["total_collateral_usd"]

        lines = [
            "🏦 <b>MULTI-SUBACCOUNT STRATEGY SHARDING</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"💼 <b>Total Portfolio Collateral:</b> <code>${total_collat:,.4f} USDC</code>",
            f"👛 <b>Master Wallet:</b> <code>{self.wallet_address[:6]}...{self.wallet_address[-4:]}</code>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]

        icons = {
            SubaccountRole.SNIPER: "🎯",
            SubaccountRole.MARKET_MAKER: "🌾",
            SubaccountRole.ARBITRAGE: "⚡",
            SubaccountRole.TREASURY: "🏦",
        }

        for prof in self.profiles.values():
            st = self.states.get(prof.account_index)
            if not st:
                continue

            icon = icons.get(prof.role, "🔹")
            alloc_pct = (st.collateral_usd / total_collat * 100.0) if total_collat > 0 else 0.0
            pnl_emoji = "🟢" if st.unrealized_pnl_usd >= 0 else "🔴"

            lines.append(
                f"{icon} <b>{prof.name} (Subaccount #{prof.account_index})</b>\n"
                f"• <b>Role:</b> {prof.role.value}\n"
                f"• <b>Collateral:</b> <code>${st.collateral_usd:,.2f} USDC</code> ({alloc_pct:.1f}% / Target {prof.target_allocation_pct:.0f}%)\n"
                f"• <b>Available Margin:</b> <code>${st.available_margin_usd:,.2f}</code> | Util: <code>{st.margin_utilization_pct:.1f}%</code>\n"
                f"• <b>Positions:</b> {st.active_positions_count} open | {pnl_emoji} uPnL: <code>${st.unrealized_pnl_usd:+,.2f}</code>\n"
                f"• <b>Pending Orders:</b> {st.pending_orders_count} | Status: <i>{st.status}</i>\n"
            )

        # Check rebalance recommendations
        recs = self.calculate_rebalancing()
        if recs:
            lines.append("⚖️ <b>REBALANCE RECOMMENDATIONS:</b>")
            for r in recs:
                urgency_icon = "🚨" if r.urgency == "CRITICAL" else ("⚠️" if r.urgency == "HIGH" else "ℹ️")
                lines.append(
                    f"{urgency_icon} <b>Shift ${r.amount_usd:,.2f}</b>: #{r.from_account_index} ➡️ #{r.to_account_index}\n"
                    f"  <i>{r.reason}</i>"
                )
        else:
            lines.append("✅ <i>All strategy shards are perfectly balanced and funded!</i>")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🕒 <i>Updated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</i>")
        return "\n".join(lines)

    def format_rebalance_recommendations_html(self, recs: List[RebalanceRecommendation]) -> str:
        """Formats detailed rebalance recommendations."""
        if not recs:
            return (
                "⚖️ <b>SUBACCOUNT REBALANCE STATUS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ <b>All subaccounts within optimal tolerance!</b>\n"
                "No collateral transfers required at this time."
            )

        lines = [
            f"⚖️ <b>COLLATERAL REBALANCE RECOMMENDATIONS ({len(recs)})</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
        ]

        for i, r in enumerate(recs, 1):
            urgency_icon = "🚨" if r.urgency == "CRITICAL" else ("⚠️" if r.urgency == "HIGH" else "ℹ️")
            lines.append(
                f"{urgency_icon} <b>Action #{i}: Transfer ${r.amount_usd:,.2f} USDC</b>\n"
                f"• <b>From:</b> Subaccount #{r.from_account_index} ({r.from_name})\n"
                f"• <b>To:</b> Subaccount #{r.to_account_index} ({r.to_name})\n"
                f"• <b>Current Allocations:</b> {r.from_name} ({r.current_ratio_from*100:.1f}%) ➡️ {r.to_name} ({r.current_ratio_to*100:.1f}%)\n"
                f"• <b>Target Allocations:</b> {r.from_name} ({r.target_ratio_from*100:.1f}%) ➡️ {r.to_name} ({r.target_ratio_to*100:.1f}%)\n"
                f"• <b>Reason:</b> {r.reason}\n"
                f"• <b>Urgency:</b> <code>{r.urgency}</code>\n"
            )

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 <i>Tap 'Execute Rebalance' to automatically re-distribute funds.</i>")
        return "\n".join(lines)
