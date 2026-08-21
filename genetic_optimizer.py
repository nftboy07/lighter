#!/usr/bin/env python3
"""
AI Genetic Strategy & Parameter Auto-Tuner (genetic_optimizer.py)
================================================================
Autonomously evolves and optimizes trading parameters (TP %, SL %, ATR multiplier,
and minimum conviction threshold) using genetic algorithms to maximize Sharpe ratio.
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("GeneticOptimizer")


@dataclass
class StrategyGenome:
    """Individual parameter chromosome for the trading strategy."""
    genome_id: str
    tp_pct: float
    sl_pct: float
    atr_multiplier: float
    min_conviction: float
    time_decay_seconds: float
    fitness_score: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0


class GeneticStrategyOptimizer:
    """
    Genetic Algorithm optimizing hyperparameters across completed trade outcomes.
    """

    def __init__(
        self,
        population_size: int = 15,
        mutation_rate: float = 0.20,
        generations: int = 10,
    ):
        self.population_size = population_size
        self.mutation_rate = mutation_rate
        self.generations = generations
        self.population: List[StrategyGenome] = []
        self._init_population()

    def _init_population(self) -> None:
        """Initializes diverse parameter chromosomes."""
        for i in range(self.population_size):
            self.population.append(
                StrategyGenome(
                    genome_id=f"genome_{i}",
                    tp_pct=round(random.uniform(1.8, 5.0), 2),
                    sl_pct=round(random.uniform(1.0, 2.5), 2),
                    atr_multiplier=round(random.uniform(1.2, 2.5), 2),
                    min_conviction=round(random.uniform(0.75, 0.95), 2),
                    time_decay_seconds=round(random.uniform(60.0, 180.0), 0),
                )
            )

    def evaluate_fitness(
        self,
        genome: StrategyGenome,
        trade_pnls: List[float],
    ) -> float:
        """
        Evaluates genome fitness based on Sharpe ratio, win rate, and profit factor.
        """
        if not trade_pnls:
            genome.fitness_score = 1.0
            return 1.0

        wins = [p for p in trade_pnls if p > 0]
        losses = [abs(p) for p in trade_pnls if p < 0]

        win_rate = (len(wins) / len(trade_pnls)) * 100.0
        gross_profit = sum(wins)
        gross_loss = sum(losses)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 1.0)

        # Sharpe ratio proxy
        mean_pnl = sum(trade_pnls) / len(trade_pnls)
        variance = sum((p - mean_pnl) ** 2 for p in trade_pnls) / max(1, len(trade_pnls) - 1)
        std_dev = math.sqrt(variance) if variance > 0 else 1.0
        sharpe = (mean_pnl / std_dev) * math.sqrt(252.0)

        fitness = (profit_factor * 0.4) + (win_rate * 0.03) + (max(0.0, sharpe) * 0.3)
        genome.fitness_score = round(fitness, 3)
        genome.win_rate_pct = round(win_rate, 2)
        genome.profit_factor = round(profit_factor, 2)
        return genome.fitness_score

    def evolve_generation(self, trade_pnls: List[float]) -> StrategyGenome:
        """
        Runs one cycle of evaluation, selection, crossover, and mutation.
        """
        for g in self.population:
            self.evaluate_fitness(g, trade_pnls)

        # Sort by highest fitness
        self.population.sort(key=lambda g: g.fitness_score, reverse=True)
        best_genome = self.population[0]

        # Top 30% elites survive
        elite_count = max(2, int(self.population_size * 0.3))
        survivors = self.population[:elite_count]

        new_pop: List[StrategyGenome] = list(survivors)
        while len(new_pop) < self.population_size:
            # Tournament selection
            p1 = random.choice(survivors)
            p2 = random.choice(survivors)

            # Crossover
            child = StrategyGenome(
                genome_id=f"genome_gen_{int(time.time()*1000)%10000}",
                tp_pct=p1.tp_pct if random.random() > 0.5 else p2.tp_pct,
                sl_pct=p1.sl_pct if random.random() > 0.5 else p2.sl_pct,
                atr_multiplier=p1.atr_multiplier if random.random() > 0.5 else p2.atr_multiplier,
                min_conviction=p1.min_conviction if random.random() > 0.5 else p2.min_conviction,
                time_decay_seconds=p1.time_decay_seconds if random.random() > 0.5 else p2.time_decay_seconds,
            )

            # Mutation
            if random.random() < self.mutation_rate:
                child.tp_pct = max(1.5, round(child.tp_pct + random.uniform(-0.3, 0.3), 2))
                child.sl_pct = max(0.8, round(child.sl_pct + random.uniform(-0.2, 0.2), 2))

            new_pop.append(child)

        self.population = new_pop
        logger.info("🧬 [GeneticOptimizer] Evolved best genome %s: TP +%.1f%% | SL -%.1f%% | Fitness: %.2f", best_genome.genome_id, best_genome.tp_pct, best_genome.sl_pct, best_genome.fitness_score)
        return best_genome
