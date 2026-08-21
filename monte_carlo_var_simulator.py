#!/usr/bin/env python3
"""
Monte Carlo 10,000-Path VaR & Tail-Risk Simulator (monte_carlo_var_simulator.py)
==============================================================================
Runs 10,000 synthetic Geometric Brownian Motion (GBM) simulation paths on active
portfolio positions to compute 99% Value at Risk (VaR) and Expected Shortfall (CVaR).
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("MonteCarloVaR")


@dataclass
class PortfolioVaRReport:
    """Consolidated Value at Risk and tail-risk report."""
    total_portfolio_usd: float
    simulated_paths_count: int
    var_95_pct_usd: float              # Max expected dollar loss at 95% confidence (1-day)
    var_99_pct_usd: float              # Max expected dollar loss at 99% confidence (1-day)
    cvar_99_pct_usd: float             # Expected Shortfall (average loss in worst 1% tail)
    tail_risk_probability_pct: float   # Probability of losing >= 10% of portfolio
    recommended_margin_scaling_mult: float # Multiplier to scale down margin if tail risk is high (e.g. 0.7x - 1.0x)
    is_safe: bool
    timestamp: float = field(default_factory=time.time)


class MonteCarloRiskSimulator:
    """
    10,000-Path Monte Carlo Portfolio Risk Simulator.
    """

    def __init__(
        self,
        num_paths: int = 10000,
        time_horizon_days: float = 1.0,
        max_acceptable_tail_prob_pct: float = 2.5,
    ):
        self.num_paths = num_paths
        self.time_horizon_days = time_horizon_days
        self.max_acceptable_tail_prob_pct = max_acceptable_tail_prob_pct

    def simulate_portfolio_var(
        self,
        total_portfolio_usd: float,
        active_positions: List[Dict[str, Any]],  # List of {"symbol": str, "notional_usd": float, "is_long": bool, "volatility_annual_pct": float}
    ) -> PortfolioVaRReport:
        """
        Simulates 10,000 portfolio outcome paths using GBM.
        """
        if not active_positions or total_portfolio_usd <= 0:
            return PortfolioVaRReport(
                total_portfolio_usd=total_portfolio_usd,
                simulated_paths_count=self.num_paths,
                var_95_pct_usd=0.0,
                var_99_pct_usd=0.0,
                cvar_99_pct_usd=0.0,
                tail_risk_probability_pct=0.0,
                recommended_margin_scaling_mult=1.0,
                is_safe=True,
            )

        dt = self.time_horizon_days / 365.0
        final_pnls: List[float] = []

        for _ in range(self.num_paths):
            path_pnl = 0.0
            for pos in active_positions:
                notional = pos.get("notional_usd", 0.0)
                is_long = pos.get("is_long", True)
                sigma = (pos.get("volatility_annual_pct", 55.0) / 100.0)

                # Box-Muller standard normal
                z = random.gauss(0.0, 1.0)
                # S_T = S_0 * exp( -0.5 * sigma^2 * dt + sigma * sqrt(dt) * z )
                drift = -0.5 * (sigma ** 2) * dt
                diffusion = sigma * math.sqrt(dt) * z
                ret = math.exp(drift + diffusion) - 1.0

                pos_pnl = notional * ret if is_long else notional * (-ret)
                path_pnl += pos_pnl

            final_pnls.append(path_pnl)

        # Sort paths from worst loss to highest profit
        final_pnls.sort()

        idx_95 = int(self.num_paths * 0.05)
        idx_99 = int(self.num_paths * 0.01)

        var_95 = abs(min(0.0, final_pnls[idx_95]))
        var_99 = abs(min(0.0, final_pnls[idx_99]))

        worst_1_pct = [p for p in final_pnls[:idx_99]]
        cvar_99 = abs(sum(worst_1_pct) / len(worst_1_pct)) if worst_1_pct else var_99

        # Tail risk prob (loss >= 10% of portfolio)
        tail_threshold = total_portfolio_usd * 0.10
        tail_breaches = sum(1 for p in final_pnls if p <= -tail_threshold)
        tail_prob = (tail_breaches / self.num_paths) * 100.0

        is_safe = tail_prob <= self.max_acceptable_tail_prob_pct
        scaling_mult = 1.0 if is_safe else round(max(0.4, 1.0 - (tail_prob / 100.0)), 2)

        report = PortfolioVaRReport(
            total_portfolio_usd=round(total_portfolio_usd, 2),
            simulated_paths_count=self.num_paths,
            var_95_pct_usd=round(var_95, 2),
            var_99_pct_usd=round(var_99, 2),
            cvar_99_pct_usd=round(cvar_99, 2),
            tail_risk_probability_pct=round(tail_prob, 2),
            recommended_margin_scaling_mult=scaling_mult,
            is_safe=is_safe,
        )

        logger.info("🎲 [Monte Carlo 10k] 99%% VaR: $%.2f | CVaR: $%.2f | Tail Risk: %.2f%% (Safe: %s)", var_99, cvar_99, tail_prob, is_safe)
        return report
