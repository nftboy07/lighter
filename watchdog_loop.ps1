# Survives SSH teardown when launched via WMI. Restarts the live bot if python dies.
$ErrorActionPreference = "SilentlyContinue"
$log = "C:\LighterBot\watchdog.log"
while ($true) {
    $running = Get-Process python -ErrorAction SilentlyContinue
    if (-not $running) {
        Add-Content $log ("{0} python down - restarting" -f (Get-Date -Format o))
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c C:\LighterBot\run_live.bat" -WindowStyle Hidden
    }
    $sniper = Get-Item "C:\LighterBot\sniper.log" -ErrorAction SilentlyContinue
    if ($sniper -and $sniper.Length -gt 50MB) {
        Move-Item "C:\LighterBot\sniper.log" "C:\LighterBot\sniper.log.bak" -Force
        Add-Content $log ("{0} rotated sniper.log" -f (Get-Date -Format o))
    }
    Start-Sleep -Seconds 30
}
