#!/usr/bin/env python3
"""
Quantitative Market Making Strategy Engine for Lighter DEX
==========================================================
Implements:
- Continuous Avellaneda-Stoikov (2008) optimal reservation pricing
- Guéant-Tapia-Manziadi (GLFT) multi-tier asymmetric quoting
- Micro-price and Volume-Weighted Top-of-Book calculation
- Order Flow Imbalance (OFI) alpha tracking
- Asymmetric inventory skewing and post-only safety guards
"""

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class L2OrderBook:
    market_index: int
    bids: List[OrderBookLevel] = field(default_factory=list)  # Sorted descending by price
    asks: List[OrderBookLevel] = field(default_factory=list)  # Sorted ascending by price
    timestamp: float = field(default_factory=time.time)
    nonce: int = 0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_bid_size(self) -> float:
        return self.bids[0].size if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else float("inf")

    @property
    def best_ask_size(self) -> float:
        return self.asks[0].size if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        if self.best_bid > 0 and self.best_ask < float("inf"):
            return (self.best_bid + self.best_ask) / 2.0
        elif self.best_bid > 0:
            return self.best_bid
        elif self.best_ask < float("inf"):
            return self.best_ask
        return 0.0

    @property
    def spread(self) -> float:
        if self.best_bid > 0 and self.best_ask < float("inf"):
            return max(0.0, self.best_ask - self.best_bid)
        return 0.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        if mid > 0:
            return (self.spread / mid) * 10_000.0
        return 0.0


@dataclass
class TargetQuote:
    side: OrderSide
    price: float
    size: float
    layer: int
    is_post_only: bool = True


class AvellanedaStoikovQuoter:
    """
    High-frequency market making quoting engine using the Avellaneda-Stoikov
    and Guéant et al. (GLFT) frameworks tailored for high-volume maker churning.
    """

    def __init__(
        self,
        gamma: float = 0.05,        # Risk aversion parameter (lower = tighter quotes / more volume)
        kappa: float = 1.8,         # Order book liquidity density / elasticity
        volatility: float = 0.015,  # Asset price volatility (sigma)
        phi: float = 0.01,          # Inventory penalty coefficient
        num_layers: int = 3,        # Number of layered grid orders per side
        base_size: float = 0.05,    # Base order size in base asset units
        min_size: float = 0.001,    # Minimum allowable order size
        max_size: float = 2.0,      # Maximum allowable order size
        tick_size: float = 0.01,    # Minimum price increment
        size_decimals: int = 4,     # Decimal places for order size
        price_decimals: int = 2,    # Decimal places for order price
        target_spread_bps: float = 2.0,  # Base target spread in basis points
    ):
        self.gamma = max(0.0001, gamma)
        self.kappa = max(0.01, kappa)
        self.sigma = max(0.0001, volatility)
        self.phi = phi
        self.num_layers = max(1, num_layers)
        self.base_size = base_size
        self.min_size = min_size
        self.max_size = max_size
        self.tick_size = tick_size
        self.size_decimals = size_decimals
        self.price_decimals = price_decimals
        self.target_spread_bps = target_spread_bps

        # Rolling volatility tracking
        self.price_history: List[float] = []
        self.max_history_len = 120

    def update_volatility(self, mid_price: float) -> float:
        """Dynamically updates rolling realized volatility from mid-price stream."""
        if mid_price <= 0:
            return self.sigma

        self.price_history.append(mid_price)
        if len(self.price_history) > self.max_history_len:
            self.price_history.pop(0)

        if len(self.price_history) >= 10:
            returns = [
                (self.price_history[i] - self.price_history[i - 1]) / self.price_history[i - 1]
                for i in range(1, len(self.price_history))
            ]
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
            realized_vol = math.sqrt(variance)
            # Smooth with exponential moving average
            self.sigma = 0.9 * self.sigma + 0.1 * max(0.0001, realized_vol)

        return self.sigma

    def calculate_micro_price(self, book: L2OrderBook) -> float:
        """
        Calculates volume-weighted micro-price:
        S_micro = (V_a * P_b + V_b * P_a) / (V_a + V_b)
        """
        if not book.bids or not book.asks:
            return book.mid_price

        bid_p, bid_v = book.bids[0].price, book.bids[0].size
        ask_p, ask_v = book.asks[0].price, book.asks[0].size
        total_vol = bid_v + ask_v

        if total_vol <= 0:
            return (bid_p + ask_p) / 2.0

        return (bid_v * ask_p + ask_v * bid_p) / total_vol

    def calculate_ofi(self, prev_book: Optional[L2OrderBook], curr_book: L2OrderBook) -> float:
        """
        Calculates Order Flow Imbalance (OFI) metric between consecutive orderbook snapshots.
        Positive OFI indicates net buying pressure (skew quotes up).
        Negative OFI indicates net selling pressure (skew quotes down).
        """
        if prev_book is None or not prev_book.bids or not prev_book.asks or not curr_book.bids or not curr_book.asks:
            return 0.0

        # Bid queue change
        if curr_book.best_bid > prev_book.best_bid:
            delta_bid = curr_book.best_bid_size
        elif curr_book.best_bid == prev_book.best_bid:
            delta_bid = curr_book.best_bid_size - prev_book.best_bid_size
        else:
            delta_bid = -prev_book.best_bid_size

        # Ask queue change
        if curr_book.best_ask < prev_book.best_ask:
            delta_ask = curr_book.best_ask_size
        elif curr_book.best_ask == prev_book.best_ask:
            delta_ask = curr_book.best_ask_size - prev_book.best_ask_size
        else:
            delta_ask = -prev_book.best_ask_size

        return delta_bid - delta_ask

    def calculate_reservation_price(
        self,
        fair_price: float,
        inventory_q: float,
    ) -> float:
        """
        Calculates Avellaneda-Stoikov reservation (indifference) price:
        r(s, q) = s - q * phi * sigma^2
        """
        inventory_penalty = inventory_q * self.phi * (self.sigma ** 2) * fair_price
        return fair_price - inventory_penalty

    def round_price(self, price: float) -> float:
        """Rounds price to tick size precision."""
        if self.tick_size <= 0:
            return round(price, self.price_decimals)
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, self.price_decimals)

    def round_size(self, size: float) -> float:
        """Clamps and rounds order size to specified decimals."""
        clamped = max(self.min_size, min(self.max_size, size))
        return round(clamped, self.size_decimals)

    def generate_quotes(
        self,
        book: L2OrderBook,
        inventory_q: float,
        prev_book: Optional[L2OrderBook] = None,
        cex_lead_price: Optional[float] = None,
    ) -> Dict[OrderSide, List[TargetQuote]]:
        """
        Generates multi-layer target quotes for buy and sell sides.
        Applies:
        - Micro-price & OFI alpha
        - Avellaneda-Stoikov reservation price
        - Inventory-skewed asymmetric quote sizing
        - Post-only safe non-crossing guard
        """
        if book.mid_price <= 0:
            return {OrderSide.BUY: [], OrderSide.SELL: []}

        # 1. Update Realized Volatility
        self.update_volatility(book.mid_price)

        # 2. Compute Fair Price with Alpha
        micro_price = self.calculate_micro_price(book)
        ofi = self.calculate_ofi(prev_book, book)
        # Small OFI alpha offset
        ofi_alpha = math.tanh(ofi) * (self.tick_size * 0.5)

        if cex_lead_price and cex_lead_price > 0:
            # 60% lead CEX price + 40% DEX micro-price + OFI alpha
            fair_price = 0.6 * cex_lead_price + 0.4 * micro_price + ofi_alpha
        else:
            fair_price = micro_price + ofi_alpha

        # 3. Reservation Price
        reservation_price = self.calculate_reservation_price(fair_price, inventory_q)

        # 4. Optimal Base Half-Spread: (1 / gamma) * ln(1 + gamma / kappa)
        # Scaled by target_spread_bps
        model_half_spread = (1.0 / self.gamma) * math.log(1.0 + (self.gamma / self.kappa)) * (fair_price * 0.0001)
        min_half_spread = fair_price * (self.target_spread_bps / 20_000.0)
        base_half_spread = max(model_half_spread, min_half_spread, self.tick_size)

        # Multipliers for grid layers
        layer_spread_multipliers = [1.0, 2.0, 3.5, 5.5, 8.0]
        layer_size_multipliers = [1.0, 1.4, 2.0, 2.8, 4.0]

        buy_quotes: List[TargetQuote] = []
        sell_quotes: List[TargetQuote] = []

        # Inventory skewing factors:
        # If long (inventory_q > 0): quote smaller on bid, larger on ask to unload passively
        # If short (inventory_q < 0): quote larger on bid, smaller on ask to cover passively
        bid_inventory_factor = max(0.2, 1.0 - (0.3 * inventory_q))
        ask_inventory_factor = max(0.2, 1.0 + (0.3 * inventory_q))

        for layer in range(self.num_layers):
            idx = min(layer, len(layer_spread_multipliers) - 1)
            spread_mult = layer_spread_multipliers[idx]
            size_mult = layer_size_multipliers[idx]

            current_half_spread = base_half_spread * spread_mult

            # Asymmetric quote prices
            raw_bid = reservation_price - current_half_spread
            raw_ask = reservation_price + current_half_spread

            bid_price = self.round_price(raw_bid)
            ask_price = self.round_price(raw_ask)

            # Post-only guarantee: Do NOT cross the best opposite book side
            if book.best_ask < float("inf") and bid_price >= book.best_ask:
                bid_price = self.round_price(book.best_ask - self.tick_size)

            if book.best_bid > 0 and ask_price <= book.best_bid:
                ask_price = self.round_price(book.best_bid + self.tick_size)

            # Ensure bid is strictly lower than ask
            if bid_price >= ask_price:
                bid_price = self.round_price(ask_price - self.tick_size)

            # Calculate sizes with layer multiplier and inventory factor
            bid_size = self.round_size(self.base_size * size_mult * bid_inventory_factor)
            ask_size = self.round_size(self.base_size * size_mult * ask_inventory_factor)

            buy_quotes.append(
                TargetQuote(
                    side=OrderSide.BUY,
                    price=bid_price,
                    size=bid_size,
                    layer=layer,
                    is_post_only=True,
                )
            )

            sell_quotes.append(
                TargetQuote(
                    side=OrderSide.SELL,
                    price=ask_price,
                    size=ask_size,
                    layer=layer,
                    is_post_only=True,
                )
            )

        return {
            OrderSide.BUY: buy_quotes,
            OrderSide.SELL: sell_quotes,
        }
