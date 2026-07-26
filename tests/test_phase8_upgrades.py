#!/usr/bin/env python3
"""
Unit tests for Phase 8 Upgrades:
- Private RPC submission & gas escalation
- Pre-trade EVM sell simulation
- Kelly Criterion position sizing
- Social signal address parsing
"""

import unittest
from unittest.mock import MagicMock, patch
import os
import tempfile
import sqlite3

from execution_engine import ExecutionEngine
from safety_analyzer import SafetyAnalyzer
from risk_manager import RiskManager
from early_detection import SocialSignalParser


class TestPhase8Upgrades(unittest.TestCase):

    def test_gas_escalation(self):
        engine = ExecutionEngine(w3=None, quoter_v2="0x" + "1" * 40, router="0x" + "2" * 40, weth="0x" + "3" * 40)
        base_gas = {"maxPriorityFeePerGas": 1000, "maxFeePerGas": 5000, "gasLimit": 300000}
        escalated = engine.escalate_transaction_priority(base_gas, multiplier=2.0)
        self.assertEqual(escalated["maxPriorityFeePerGas"], 2000)
        self.assertEqual(escalated["maxFeePerGas"], 10000)

    @patch('safety_analyzer.Web3')
    def test_pretrade_sell_simulation(self, mock_web3):
        mock_w3 = MagicMock()
        dummy_addr = "0x" + "1" * 40
        analyzer = SafetyAnalyzer(w3=mock_w3, quoter_v2=dummy_addr, router=dummy_addr, weth=dummy_addr)
        mock_w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value = 1000
        mock_w3.eth.contract.return_value.functions.approve.return_value.build_transaction.return_value = {"data": "0x123"}
        mock_w3.eth.call.return_value = b''

        is_sellable, reason = analyzer.simulate_pretrade_sell(dummy_addr, dummy_addr)
        self.assertTrue(is_sellable)
        self.assertIn("passed", reason)

    def test_kelly_criterion_from_db(self):
        risk_mgr = RiskManager(max_position_eth=0.1)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_db = tmp.name

        try:
            conn = sqlite3.connect(tmp_db)
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, action TEXT, eth_amount REAL, status TEXT)")
            # Insert 5 wins and 2 losses
            for _ in range(5):
                cursor.execute("INSERT INTO trades (action, eth_amount, status) VALUES ('sell', 0.005, 'success')")
            for _ in range(2):
                cursor.execute("INSERT INTO trades (action, eth_amount, status) VALUES ('STOP_LOSS', 0.001, 'success')")
            conn.commit()
            conn.close()

            size = risk_mgr.calculate_kelly_from_db(tmp_db, wallet_balance_eth=1.0)
            self.assertGreater(size, 0.0)
            self.assertLessEqual(size, 0.1)
        finally:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)

    def test_social_signal_parser(self):
        sample_text = "New token launch on o1.exchange! Check CA 0x948D4991b25BE9cf2632Fad6ECf6FF9528298538 live now!"
        addresses = SocialSignalParser.extract_contract_addresses(sample_text)
        self.assertEqual(len(addresses), 1)
        self.assertEqual(addresses[0], "0x948D4991b25BE9cf2632Fad6ECf6FF9528298538")
        self.assertTrue(SocialSignalParser.contains_alpha_keywords(sample_text))


if __name__ == '__main__':
    unittest.main()
