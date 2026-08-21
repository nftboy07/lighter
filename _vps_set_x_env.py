"""Append the operator's own X bearer to VPS .env. Does not print secrets. Does not use GitHub PAT."""

from pathlib import Path
from urllib.parse import unquote

import paramiko

import _vps_deploy as d

ENV_PATH = r"C:\LighterBot\.env"
PASS_FILE = Path(r"C:\Users\91907\Desktop\pass.txt")


def read_x_bearer() -> str:
    for line in PASS_FILE.read_text(encoding="utf-8").splitlines():
        low = line.lower()
        if low.startswith("x bearer") or low.startswith("twitter bearer") or low.startswith("x_bearer"):
            token = line.split(":", 1)[1].strip()
            return unquote(token)
    raise SystemExit("X bearer not found in pass.txt")


def main() -> None:
    token = read_x_bearer()
    if not token:
        raise SystemExit("empty X bearer")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(d.HOST, username=d.USER, password=d.password(), timeout=20, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    with sftp.open(ENV_PATH, "r") as fh:
        current = fh.read().decode("utf-8", "replace")
    lines = [ln for ln in current.splitlines() if not ln.startswith("X_BEARER_TOKEN=") and not ln.startswith("TWITTER_BEARER")]
    lines.append(f"X_BEARER_TOKEN={token}")
    payload = "\n".join(lines) + "\n"
    with sftp.open(ENV_PATH, "w") as fh:
        fh.write(payload)
    sftp.close()
    print("X_BEARER_TOKEN written to VPS .env (value not printed)", flush=True)
    c.close()


if __name__ == "__main__":
    main()
