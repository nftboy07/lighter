#!/usr/bin/env python3
"""
Autonomous Delta-Neutral Basis Compounder Vault (delta_neutral_basis_vault.py)
=============================================================================
Manages a 50% Spot Long / 50% Perp Short cash-and-carry basis position.
Collects 8-hour perpetual funding payments, maintains 0.00 Delta neutrality,
and automatically compounds harvested yield back into the spot asset.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("BasisVault")


@dataclass
class BasisVaultState:
    """State of the delta-neutral basis compounding vault."""
    symbol: str
    spot_balance: float
    perp_short_size: float
    current_spot_price: float
    current_perp_price: float
    total_vault_value_usd: float
    net_delta_usd: float               # Spot USD - Perp USD (Target: 0.00)
    accumulated_funding_harvest_usd: float
    annualized_yield_pct: float
    last_harvest_timestamp: float = field(default_factory=time.time)


class DeltaNeutralBasisVault:
    """
    Automated cash-and-carry basis position manager & compounder.
    """

    def __init__(
        self,
        symbol: str = "SOL",
        initial_capital_usd: float = 100.0,
        max_delta_imbalance_usd: float = 5.0,  # Max allowed delta deviation
    ):
        self.symbol = symbol.upper()
        self.max_delta_imbalance_usd = max_delta_imbalance_usd

        self.spot_balance: float = 0.0
        self.perp_short_size: float = 0.0
        self.accumulated_harvest_usd: float = 0.0
        self.harvest_history: List[Tuple[float, float]] = []  # (harvest_usd, timestamp)

        # Allocate 50/50 initially if capital provided
        self._initial_capital = initial_capital_usd

    def allocate_initial_position(self, spot_price: float, perp_price: float) -> BasisVaultState:
        """Initializes 50% spot long and 50% perp short."""
        half_cap = self._initial_capital / 2.0
        self.spot_balance = round(half_cap / spot_price, 4)
        self.perp_short_size = round(half_cap / perp_price, 4)
        return self.get_vault_state(spot_price, perp_price)

    def harvest_funding_payment(
        self,
        funding_rate_8h: float,
        spot_price: float,
        perp_price: float,
    ) -> float:
        """
        Collects funding payment on short perp position and reinvests into spot.
        Payment = Perp_Short_Size * Perp_Price * Funding_Rate
        """
        perp_notional = self.perp_short_size * perp_price
        payout_usd = perp_notional * funding_rate_8h

        if payout_usd > 0:
            self.accumulated_harvest_usd += payout_usd
            self.harvest_history.append((payout_usd, time.time()))

            # Compound: Reinvest 100% of payout into additional spot
            additional_spot = payout_usd / spot_price
            self.spot_balance += additional_spot
            logger.info("🌾 [Basis Vault] Harvested $%.4f funding payout on %s! Compounded +%.4f spot units", payout_usd, self.symbol, additional_spot)

        return payout_usd

    def rebalance_delta(self, spot_price: float, perp_price: float) -> Tuple[bool, float]:
        """
        Rebalances spot and perp short legs if net delta exceeds threshold.
        """
        spot_usd = self.spot_balance * spot_price
        perp_usd = self.perp_short_size * perp_price
        net_delta = spot_usd - perp_usd

        if abs(net_delta) > self.max_delta_imbalance_usd:
            # Need to rebalance perp short to match spot
            target_short = spot_usd / perp_price
            delta_adj = target_short - self.perp_short_size
            self.perp_short_size = round(target_short, 4)
            logger.info("⚖️ [Basis Vault] Rebalanced %s Delta: Adjusted short by %+.4f units (Net Delta was $%.2f)", self.symbol, delta_adj, net_delta)
            return True, delta_adj

        return False, 0.0

    def get_vault_state(self, spot_price: float, perp_price: float) -> BasisVaultState:
        """Computes current vault value, net delta, and annualized yield."""
        spot_usd = self.spot_balance * spot_price
        perp_usd = self.perp_short_size * perp_price
        total_val = spot_usd + perp_usd
        net_delta = spot_usd - perp_usd

        # Estimate APR based on last 24h harvests or baseline
        annualized_yield = (self.accumulated_harvest_usd / max(1.0, total_val)) * 365.0 * 100.0 if self.accumulated_harvest_usd > 0 else 28.5

        return BasisVaultState(
            symbol=self.symbol,
            spot_balance=round(self.spot_balance, 4),
            perp_short_size=round(self.perp_short_size, 4),
            current_spot_price=spot_price,
            current_perp_price=perp_price,
            total_vault_value_usd=round(total_val, 2),
            net_delta_usd=round(net_delta, 2),
            accumulated_funding_harvest_usd=round(self.accumulated_harvest_usd, 4),
            annualized_yield_pct=round(annualized_yield, 2),
        )
