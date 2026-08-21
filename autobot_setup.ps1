# autobot_setup.ps1 — One-time setup for autobot on Windows
# Run this once before first use:
#   powershell -ExecutionPolicy Bypass -File autobot_setup.ps1

Write-Host "Installing autobot Python dependencies..." -ForegroundColor Cyan
pip install -r requirements_autobot.txt

Write-Host "Downloading Playwright Chromium browser (~130 MB)..." -ForegroundColor Cyan
playwright install chromium

Write-Host ""
Write-Host "Setup complete! Run the bot with:" -ForegroundColor Green
Write-Host "  python autobot/autobot.py https://example.com" -ForegroundColor Yellow
