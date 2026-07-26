#!/usr/bin/env python3
"""
Unit tests for Phase 10 Ultimate Institutional & Security Suite Upgrades:
- Bytecode opcode assembly scanning
- Dev wallet funding cluster analysis
- Database analytics dashboard computation
"""

import unittest
from unittest.mock import patch, MagicMock

from safety_analyzer import SafetyAnalyzer


class TestPhase10Institutional(unittest.TestCase):

    def test_bytecode_opcode_scanner_safe(self):
        mock_w3 = MagicMock()
        dummy_addr = "0x" + "1" * 40
        # Normal bytecode (no selfdestruct or delegatecall)
        mock_w3.eth.get_code.return_value = bytes.fromhex("608060405234801561001057600080fd5b50")

        analyzer = SafetyAnalyzer(w3=mock_w3, quoter_v2=dummy_addr, router=dummy_addr, weth=dummy_addr)
        res = analyzer.scan_bytecode_opcodes(dummy_addr)
        self.assertTrue(res["is_safe"])
        self.assertEqual(res["score"], 100)
        self.assertEqual(len(res["dangerous_opcodes"]), 0)

    def test_bytecode_opcode_scanner_dangerous(self):
        mock_w3 = MagicMock()
        dummy_addr = "0x" + "1" * 40
        # Bytecode containing 0xff (SELFDESTRUCT) and 0xf4 (DELEGATECALL)
        mock_w3.eth.get_code.return_value = bytes.fromhex("6080604052348015ff57600080f45b50")

        analyzer = SafetyAnalyzer(w3=mock_w3, quoter_v2=dummy_addr, router=dummy_addr, weth=dummy_addr)
        res = analyzer.scan_bytecode_opcodes(dummy_addr)
        self.assertFalse(res["is_safe"])
        self.assertIn("SELFDESTRUCT (0xff)", res["dangerous_opcodes"])

    def test_dev_funding_cluster(self):
        mock_w3 = MagicMock()
        dummy_addr = "0x" + "2" * 40
        mock_w3.eth.get_transaction_count.return_value = 1
        mock_w3.eth.get_balance.return_value = 1000000000000000000
        mock_w3.from_wei.return_value = 1.0

        analyzer = SafetyAnalyzer(w3=mock_w3, quoter_v2=dummy_addr, router=dummy_addr, weth=dummy_addr)
        res = analyzer.analyze_dev_funding_cluster(dummy_addr)
        self.assertTrue(res["is_fresh_wallet"])
        self.assertEqual(res["risk_level"], "HIGH")


if __name__ == '__main__':
    unittest.main()
