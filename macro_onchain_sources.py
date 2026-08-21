#!/usr/bin/env python3
"""
Macro & On-Chain Alpha Ingestion Sources (macro_onchain_sources.py)
==================================================================
1. FOMC / Macro Statement Word-by-Word Diff Parser.
2. Whale & Stablecoin Mint/Burn Ingestor (>$10M Tether/Circle).
3. DAO Governance Forum & Snapshot Proposal Stream.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("MacroOnChainSources")


@dataclass
class MacroSignal:
    """Actionable macro or on-chain signal emitted to the news pipeline."""
    source_type: str        # "FOMC_DIFF", "WHALE_STABLECOIN_MINT", "DAO_GOVERNANCE"
    asset: str
    direction: str          # "BULLISH", "BEARISH", "NEUTRAL"
    headline: str
    impact_score: float     # [0.0 ... 1.0]
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class MacroOnChainSourcesEngine:
    """
    Parses macro statements, stablecoin mints, and governance votes for instant alpha.
    """

    HAWKISH_TERMS = {"inflation", "tightening", "rate hike", "higher for longer", "restrictive", "elevated", "upside risks"}
    DOVISH_TERMS = {"easing", "rate cut", "disinflation", "cooling", "accommodative", "downturn", "softening", "labor market weakness"}

    def __init__(self, min_stablecoin_mint_usd: float = 10000000.0):  # $10M min
        self.min_stablecoin_mint_usd = min_stablecoin_mint_usd
        self._last_fomc_statement: str = ""

    def parse_fomc_statement_diff(
        self,
        new_statement_text: str,
        previous_statement_text: Optional[str] = None,
    ) -> Optional[MacroSignal]:
        """
        Calculates word-by-word diff of central bank statement and classifies policy shift.
        """
        prev = previous_statement_text or self._last_fomc_statement
        self._last_fomc_statement = new_statement_text

        if not prev:
            return None

        # Clean words
        words_prev = re.findall(r"\b\w+\b", prev.lower())
        words_new = re.findall(r"\b\w+\b", new_statement_text.lower())

        added_words = set(words_new) - set(words_prev)
        removed_words = set(words_prev) - set(words_new)

        dovish_hits = (added_words & self.DOVISH_TERMS) | (removed_words & self.HAWKISH_TERMS)
        hawkish_hits = (added_words & self.HAWKISH_TERMS) | (removed_words & self.DOVISH_TERMS)

        score_diff = len(dovish_hits) - len(hawkish_hits)

        if score_diff > 0:
            return MacroSignal(
                source_type="FOMC_DIFF",
                asset="BTC",
                direction="BULLISH",
                headline=f"FOMC Statement Shifts DOVISH ({len(dovish_hits)} dovish terms added/removed)",
                impact_score=min(0.99, 0.85 + (score_diff * 0.05)),
                metadata={"dovish_hits": list(dovish_hits), "hawkish_hits": list(hawkish_hits)},
            )
        elif score_diff < 0:
            return MacroSignal(
                source_type="FOMC_DIFF",
                asset="BTC",
                direction="BEARISH",
                headline=f"FOMC Statement Shifts HAWKISH ({len(hawkish_hits)} hawkish terms added/removed)",
                impact_score=min(0.99, 0.85 + (abs(score_diff) * 0.05)),
                metadata={"dovish_hits": list(dovish_hits), "hawkish_hits": list(hawkish_hits)},
            )

        return None

    def parse_stablecoin_mint_burn(
        self,
        token: str,
        amount_usd: float,
        action: str,  # "MINT" or "BURN"
        chain: str = "Ethereum",
    ) -> Optional[MacroSignal]:
        """
        Emits signal on massive institutional liquidity injection ($10M+ USDT/USDC mints).
        """
        if amount_usd < self.min_stablecoin_mint_usd:
            return None

        is_mint = action.upper() == "MINT"
        direction = "BULLISH" if is_mint else "BEARISH"
        action_verb = "Minted" if is_mint else "Burned"

        return MacroSignal(
            source_type="WHALE_STABLECOIN_MINT",
            asset="CRYPTO_GENERAL",
            direction=direction,
            headline=f"Tether/Circle Treasury: ${amount_usd:,.0f} {token.upper()} {action_verb} on {chain}",
            impact_score=min(0.95, 0.75 + (amount_usd / 100000000.0) * 0.20),
            metadata={"token": token, "amount_usd": amount_usd, "action": action, "chain": chain},
        )

    def parse_governance_proposal(
        self,
        protocol: str,
        title: str,
        status: str = "PASSED",
    ) -> Optional[MacroSignal]:
        """
        Evaluates DeFi governance proposals (fee switches, burns, buybacks).
        """
        title_lower = title.lower()
        catalyst_terms = ["fee switch", "revenue share", "buyback", "burn", "staking rewards", "airdrop"]

        matched = [t for t in catalyst_terms if t in title_lower]
        if not matched:
            return None

        asset = protocol.upper()
        return MacroSignal(
            source_type="DAO_GOVERNANCE",
            asset=asset,
            direction="BULLISH",
            headline=f"🏛️ {protocol} Governance {status.upper()}: {title}",
            impact_score=0.92,
            metadata={"protocol": protocol, "catalyst_type": matched[0], "title": title},
        )
