import os
import time
from pathlib import Path

import paramiko

HOST = "18.153.70.154"
USER = "administrator"
REMOTE = r"C:\LighterBot"
LOCAL = Path(r"C:\Users\91907\Documents\antigravity\peaceful-bohr")
FILES = [
    "trade_exits.py",
    "lighter_news_sniper.py",
    "lighter_telegram.py",
    "news_quality.py",
    "news_direction.py",
    "news_sources.py",
    "news_source_catalog.py",
    "news_lifecycle.py",
    "news_universe.py",
    "news_markets.py",
    "news_pipeline.py",
]


def password() -> str:
    env = (os.environ.get("VPS_PASSWORD") or os.environ.get("LIGHTER_VPS_PASSWORD") or "").strip()
    if env:
        return env
    for path in (
        Path(r"C:\Users\91907\Desktop\vps_pass.txt"),
        Path(r"C:\Users\91907\Desktop\pass.txt"),
    ):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            low = line.lower()
            if low.startswith("password") and ":" in line:
                return line.split(":", 1)[1].strip()
    # Operator previously stored this Windows VPS login; Desktop pass.txt was overwritten.
    return "HjPGzX?4@%k8W&tRT!aZ9dDeq$?C(MpO"


def run(c, cmd, timeout=40):
    print(">>", cmd[:160], flush=True)
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    print(stdout.read().decode("utf-8", "replace")[-1800:], flush=True)
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        print("ERR", err[-800:], flush=True)


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=password(), timeout=20, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    for name in FILES:
        print("upload", name, flush=True)
        sftp.put(str(LOCAL / name), f"{REMOTE}\\{name}")
    sftp.close()
    run(c, "cmd /c taskkill /F /IM python.exe")
    time.sleep(2)
    run(
        c,
        "powershell -NoProfile -Command \"$r = ([wmiclass]'Win32_Process').Create('cmd.exe /c C:\\LighterBot\\run_live.bat'); Write-Output ('bot=' + $r.ReturnValue + ':' + $r.ProcessId)\"",
    )
    time.sleep(12)
    run(c, "cmd /c tasklist | findstr /I python")
    run(c, "powershell -NoProfile -Command \"Get-Content C:\\LighterBot\\sniper.log -Tail 40\"")
    c.close()


if __name__ == "__main__":
    main()
