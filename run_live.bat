@echo off
cd /d C:\LighterBot
set PYTHONPATH=C:\LighterBot
set PYTHONUNBUFFERED=1
"C:\Program Files\Python312\python.exe" -u C:\LighterBot\lighter_news_sniper.py --live --margin-pct 85 >> C:\LighterBot\sniper.log 2>&1
