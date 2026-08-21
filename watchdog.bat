@echo off
cd /d C:\LighterBot
tasklist | findstr /I python.exe >nul
if errorlevel 1 (
  echo %DATE% %TIME% python down - restarting >> C:\LighterBot\watchdog.log
  start /B C:\LighterBot\run_live.bat
)
for %%F in (C:\LighterBot\sniper.log) do (
  if %%~zF GTR 52428800 (
    move /Y C:\LighterBot\sniper.log C:\LighterBot\sniper.log.bak >nul
    echo %DATE% %TIME% rotated sniper.log >> C:\LighterBot\watchdog.log
  )
)
