#!/usr/bin/env python3
"""
VPS Remote Execution Utility (run_vps_cmd.py)
==============================================
Allows executing any command, status check, log tailing, or restart
directly on the AWS Windows VPS (18.153.70.154) in 1 line.

Usage:
    python scripts/run_vps_cmd.py "status"
    python scripts/run_vps_cmd.py "logs"
    python scripts/run_vps_cmd.py "restart"
    python scripts/run_vps_cmd.py "tasklist /FI \"IMAGENAME eq python.exe\""
"""

import sys
import paramiko

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VPS_HOST = "18.153.70.154"
VPS_USER = "administrator"
VPS_PASS = "HjPGzX?4@%k8W&tRT!aZ9dDeq$?C(MpO"


def execute_vps(cmd: str):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(VPS_HOST, username=VPS_USER, password=VPS_PASS, timeout=10)
    except Exception as e:
        print(f"❌ Failed to connect to VPS {VPS_HOST}: {e}")
        return

    # Handle built-in alias shortcuts
    if cmd.lower() in ("status", "health"):
        real_cmd = (
            'powershell -Command "'
            'Write-Host \'=== 🟢 LIVE PROCESSES & MEMORY USAGE ===\' -ForegroundColor Green;'
            'Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, @{Name=\'Memory(MB)\';Expression={[math]::Round($_.WS/1MB,2)}}, StartTime | Format-Table -AutoSize;'
            'Write-Host \'=== 🛡️ 24/7 SCHEDULED TASKS ===\' -ForegroundColor Cyan;'
            'schtasks /query /tn \'LighterLiveBot\' /fo LIST | Select-String -Pattern \'TaskName|Status|Next Run Time\';'
            'schtasks /query /tn \'LighterRespawnGuard\' /fo LIST | Select-String -Pattern \'TaskName|Status|Next Run Time\';'
            'Write-Host \'=== 📋 RECENT SNIPER LOGS (LAST 15 LINES) ===\' -ForegroundColor Yellow;'
            'Get-Content -Path \'C:\\LighterBot\\sniper_app.log\' -Tail 15 -ErrorAction SilentlyContinue;'
            '"'
        )
    elif cmd.lower() in ("logs", "log", "tail"):
        real_cmd = "powershell -Command \"Get-Content -Path 'C:\\LighterBot\\sniper_app.log' -Tail 35 -ErrorAction SilentlyContinue\""
    elif cmd.lower() in ("restart", "reboot_bot"):
        real_cmd = (
            'powershell -Command "'
            'Stop-ScheduledTask -TaskName LighterLiveBot -ErrorAction SilentlyContinue;'
            'taskkill /F /IM python.exe -ErrorAction SilentlyContinue;'
            'Start-Sleep -Seconds 1;'
            'Start-ScheduledTask -TaskName LighterLiveBot;'
            'Start-Sleep -Seconds 3;'
            'Get-Process python | Select-Object Id, ProcessName, @{Name=\'Memory(MB)\';Expression={[math]::Round($_.WS/1MB,2)}}, StartTime | Format-Table -AutoSize;'
            '"'
        )
    else:
        real_cmd = cmd

    try:
        stdin, stdout, stderr = ssh.exec_command(real_cmd)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if out:
            print(out)
        if err:
            print("STDERR:", err)
    except Exception as e:
        print(f"❌ Execution error: {e}")
    finally:
        ssh.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_vps_cmd.py \"status\" | \"logs\" | \"restart\" | \"<custom_cmd>\"")
        sys.exit(1)
    command = " ".join(sys.argv[1:])
    execute_vps(command)
