"""
session_store.py — Persist credentials, cookies, and screenshots locally.

All data is stored under AUTOBOT_WORKSPACE (default: ./workspace/).
The workspace directory is gitignored.
"""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .reporter import AutobotResult

# ─── Workspace paths ──────────────────────────────────────────────────────────

def _workspace() -> Path:
    root = os.getenv("AUTOBOT_WORKSPACE", "./workspace")
    return Path(root).expanduser().resolve()

def credentials_path() -> Path:
    return _workspace() / "credentials.json"

def sessions_dir() -> Path:
    d = _workspace() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d

def screenshots_dir() -> Path:
    d = _workspace() / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d

def session_path(url: str) -> Path:
    domain = urlparse(url).netloc.replace(":", "_")
    return sessions_dir() / f"{domain}.json"

def screenshot_path(url: str, suffix: str = "") -> Path:
    domain = urlparse(url).netloc.replace(":", "_").replace(".", "_")
    ts     = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name   = f"{domain}_{ts}{suffix}.png"
    return screenshots_dir() / name


# ─── Credentials store ────────────────────────────────────────────────────────

def save_result(result: AutobotResult) -> Path:
    """Append the result to credentials.json. Creates the file if missing."""
    _workspace().mkdir(parents=True, exist_ok=True)
    path = credentials_path()

    existing: list = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []

    existing.append(result.to_dict())
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return path


def load_results() -> list[dict]:
    """Return all previously saved results."""
    path = credentials_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


# ─── Cookie / session store ───────────────────────────────────────────────────

def save_cookies(url: str, cookies: list[dict]) -> Path:
    """Persist Playwright cookie list for a domain."""
    path = session_path(url)
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    return path


def load_cookies(url: str) -> list[dict]:
    """Load persisted cookies for a domain, or empty list if none."""
    path = session_path(url)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def delete_workspace() -> None:
    """Remove the entire workspace directory (use with --cleanup flag)."""
    ws = _workspace()
    if ws.exists():
        shutil.rmtree(ws)
