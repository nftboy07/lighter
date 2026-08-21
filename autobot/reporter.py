"""
reporter.py — Structured output formatter for autobot results.

Formats the final registration report for CLI display and JSON output.
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, timezone


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class AutobotResult:
    website:    str
    email:      str       = ""
    username:   str       = ""
    password:   str       = ""
    status:     str       = "PENDING"   # PENDING | SUCCESS | FAILED | BLOCKED
    notes:      str       = ""
    attempts:   int       = 0
    screenshot: Optional[str] = None
    timestamp:  str       = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def succeeded(self) -> bool:
        return self.status == "SUCCESS"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ─── Formatters ───────────────────────────────────────────────────────────────

_STATUS_ICONS = {
    "SUCCESS": "✅",
    "FAILED":  "❌",
    "BLOCKED": "🚫",
    "PENDING": "⏳",
}

def format_cli(result: AutobotResult) -> str:
    """Return a human-readable CLI report string."""
    icon   = _STATUS_ICONS.get(result.status, "❓")
    border = "─" * 52

    lines = [
        "",
        border,
        f"  🤖  AUTOBOT RESULT  {icon}",
        border,
        f"  Website   : {result.website}",
        f"  Email     : {result.email   or '—'}",
        f"  Username  : {result.username or '—'}",
        f"  Password  : {result.password or '—'}",
        f"  Status    : {result.status}",
        f"  Notes     : {result.notes   or '—'}",
        f"  Attempts  : {result.attempts}",
    ]

    if result.screenshot:
        lines.append(f"  Screenshot: {result.screenshot}")

    lines += [
        f"  Timestamp : {result.timestamp}",
        border,
        "",
    ]
    return "\n".join(lines)


def format_blocked(result: AutobotResult) -> str:
    """Return a clear blocked/failure explanation."""
    return format_cli(result)


def print_report(result: AutobotResult) -> None:
    """Print the report to stdout."""
    print(format_cli(result))
