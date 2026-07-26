#!/usr/bin/env python3
"""
Unit tests for Phase 9 Multi-Source Data & Security Aggregation:
- GoPlus Security API parsing
- DexScreener data extraction
- GeckoTerminal data extraction
- Expanded Base RPC provider pool
"""

import unittest
from unittest.mock import patch, MagicMock

from safety_analyzer import SafetyAnalyzer
from telegram_bot import fetch_dexscreener_data, fetch_geckoterminal_data
from b20_mainnet_sniper import DEFAULT_BASE_RPCS


class TestPhase9Sources(unittest.TestCase):

    @patch('requests.get')
    def test_goplus_security_parsing(self, mock_get):
        dummy_addr = "0x948d4991b25be9cf2632fad6ecf6ff9528298538"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "result": {
                dummy_addr.lower(): {
                    "is_honeypot": "0",
                    "cannot_sell_all": "0",
                    "buy_tax": "0.01",
                    "sell_tax": "0.02",
                    "is_blacklisted": "0"
                }
            }
        }
        mock_get.return_value = mock_resp

        analyzer = SafetyAnalyzer(w3=None, quoter_v2=dummy_addr, router=dummy_addr, weth=dummy_addr)
        res = analyzer.check_goplus_security(dummy_addr)
        self.assertTrue(res["success"])
        self.assertFalse(res["is_honeypot"])
        self.assertAlmostEqual(res["buy_tax_pct"], 1.0)
        self.assertAlmostEqual(res["sell_tax_pct"], 2.0)

    @patch('requests.get')
    def test_dexscreener_parsing(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "pairs": [{
                "priceUsd": "0.0025",
                "volume": {"m5": 1500.0},
                "liquidity": {"usd": 50000.0},
                "dexId": "uniswap"
            }]
        }
        mock_get.return_value = mock_resp

        data = fetch_dexscreener_data("0x1234")
        self.assertIsNotNone(data)
        self.assertEqual(data["price_usd"], 0.0025)
        self.assertEqual(data["liquidity_usd"], 50000.0)

    @patch('requests.get')
    def test_geckoterminal_parsing(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "attributes": {
                    "price_usd": "0.0025",
                    "fdv_usd": "250000.0"
                }
            }
        }
        mock_get.return_value = mock_resp

        data = fetch_geckoterminal_data("0x1234")
        self.assertIsNotNone(data)
        self.assertEqual(data["price_usd"], 0.0025)
        self.assertEqual(data["fdv_usd"], 250000.0)

    def test_expanded_rpc_providers(self):
        self.assertGreaterEqual(len(DEFAULT_BASE_RPCS), 10)
        self.assertIn("https://base.llamarpc.com", DEFAULT_BASE_RPCS)
        self.assertIn("https://base.drpc.org", DEFAULT_BASE_RPCS)


if __name__ == '__main__':
    unittest.main()
