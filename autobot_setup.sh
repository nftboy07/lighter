#!/usr/bin/env bash
# autobot_setup.sh — One-time setup for autobot on Linux / VPS
# Run: bash autobot_setup.sh

set -e

echo "Installing autobot Python dependencies..."
pip install -r requirements_autobot.txt

echo "Downloading Playwright Chromium browser (~130 MB)..."
playwright install chromium
playwright install-deps chromium

echo ""
echo "Setup complete! Run the bot with:"
echo "  python autobot/autobot.py https://example.com"
