@echo off
title Lighter Trading Bot - 24/7 Auto-Restart Daemon
color 0A

echo =======================================================
echo   LIGHTER 24/7 AUTOMATED TRADING DAEMON
echo =======================================================
echo.

:loop
echo [%date% %time%] Starting Lighter Bot...
"C:\Program Files\Python312\python.exe" C:\LighterBot\lighter_news_sniper.py --live --margin-pct 85
:: Or for Market Maker volume farming:
:: "C:\Program Files\Python312\python.exe" C:\LighterBot\lighter_mm_bot.py --live --market 0 --size 0.001 --spread 2.0

echo.
echo [%date% %time%] Bot stopped or disconnected. Auto-restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto loop
