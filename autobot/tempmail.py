"""
tempmail.py — Disposable email address provider with multi-provider fallback.

Providers (tried in order):
  1. mail.tm  — REST API, no browser needed, reliable
  2. guerrillamail.com — REST API fallback

Public API:
    provider = TempMailProvider()
    email, password = await provider.create()
    body = await provider.wait_for_email(timeout=300)
    links = provider.extract_links(body)
"""

import asyncio
import logging
import re
import secrets
import string
from typing import Optional

import httpx

log = logging.getLogger("autobot.tempmail")

_LINK_RE = re.compile(r'https?://[^\s\'"<>]+', re.IGNORECASE)

# Keywords that suggest a verification link (ranked high → low)
_VERIFY_KEYWORDS = ["verif", "confirm", "activat", "token", "validate", "auth"]


# ─── mail.tm provider ─────────────────────────────────────────────────────────

class MailTmProvider:
    """https://api.mail.tm — free, no registration, REST API."""

    BASE = "https://api.mail.tm"

    def __init__(self):
        self._email:    Optional[str] = None
        self._password: Optional[str] = None
        self._token:    Optional[str] = None
        self._client = httpx.AsyncClient(timeout=30, follow_redirects=True)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = kwargs.pop("headers", {})
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        resp = await self._client.request(method, self.BASE + path, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    async def create(self) -> tuple[str, str]:
        """Create a fresh disposable address. Returns (email, password)."""
        # Get available domains
        data    = await self._request("GET", "/domains?page=1")
        domains = data.get("hydra:member", [])
        if not domains:
            raise RuntimeError("mail.tm: no domains available")
        domain = domains[0]["domain"]

        # Generate random local part
        local    = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12))
        email    = f"{local}@{domain}"
        password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))

        # Register account
        await self._request("POST", "/accounts", json={"address": email, "password": password})

        # Obtain JWT
        token_data   = await self._request("POST", "/token", json={"address": email, "password": password})
        self._token  = token_data["token"]
        self._email  = email
        self._password = password

        log.info("mail.tm: created %s", email)
        return email, password

    async def wait_for_email(self, timeout: int = 300, poll_interval: int = 5) -> Optional[str]:
        """
        Poll the inbox until a message arrives.
        Returns the full text+html body of the first message, or None on timeout.
        """
        log.info("mail.tm: waiting for email (timeout=%ds)…", timeout)
        elapsed = 0
        while elapsed < timeout:
            try:
                data     = await self._request("GET", "/messages?page=1")
                messages = data.get("hydra:member", [])
                if messages:
                    msg_id = messages[0]["id"]
                    msg    = await self._request("GET", f"/messages/{msg_id}")
                    body   = msg.get("html", "") or msg.get("text", "") or ""
                    # html is a list in mail.tm
                    if isinstance(body, list):
                        body = "\n".join(body)
                    log.info("mail.tm: received email (subject=%s)", messages[0].get("subject", ""))
                    return body
            except Exception as exc:
                log.debug("mail.tm poll error: %s", exc)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        log.warning("mail.tm: timed out waiting for email")
        return None

    async def close(self):
        await self._client.aclose()

    @property
    def email(self) -> Optional[str]:
        return self._email


# ─── Guerrillamail fallback ───────────────────────────────────────────────────

class GuerrillaMailProvider:
    """https://www.guerrillamail.com/GuerrillaMailAPI.html"""

    BASE = "https://api.guerrillamail.com/ajax.php"

    def __init__(self):
        self._email:    Optional[str] = None
        self._sid_token: Optional[str] = None
        self._client = httpx.AsyncClient(timeout=30, follow_redirects=True)

    async def create(self) -> tuple[str, str]:
        resp = await self._client.get(self.BASE, params={"f": "get_email_address"})
        resp.raise_for_status()
        data         = resp.json()
        self._email  = data["email_addr"]
        self._sid_token = data["sid_token"]
        log.info("guerrillamail: created %s", self._email)
        return self._email, ""   # guerrillamail has no password

    async def wait_for_email(self, timeout: int = 300, poll_interval: int = 6) -> Optional[str]:
        log.info("guerrillamail: waiting for email (timeout=%ds)…", timeout)
        seq   = 0
        elapsed = 0
        while elapsed < timeout:
            try:
                resp = await self._client.get(self.BASE, params={
                    "f":         "get_email_list",
                    "offset":    0,
                    "sid_token": self._sid_token,
                    "seq":       seq,
                })
                data  = resp.json()
                mails = data.get("list", [])
                if mails:
                    mail_id = mails[0]["mail_id"]
                    fetch   = await self._client.get(self.BASE, params={
                        "f":         "fetch_email",
                        "email_id":  mail_id,
                        "sid_token": self._sid_token,
                    })
                    body = fetch.json().get("mail_body", "")
                    log.info("guerrillamail: received email")
                    return body
            except Exception as exc:
                log.debug("guerrillamail poll error: %s", exc)

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        log.warning("guerrillamail: timed out waiting for email")
        return None

    async def close(self):
        await self._client.aclose()

    @property
    def email(self) -> Optional[str]:
        return self._email


# ─── Unified provider with fallback ──────────────────────────────────────────

class TempMailProvider:
    """
    High-level disposable email provider.
    Tries mail.tm first; falls back to guerrillamail on failure.
    """

    def __init__(self, timeout: int = 300):
        self.timeout   = timeout
        self._provider = None

    async def create(self) -> str:
        """Create a new address. Returns the email string."""
        for ProviderClass in (MailTmProvider, GuerrillaMailProvider):
            try:
                p = ProviderClass()
                email, _ = await p.create()
                self._provider = p
                return email
            except Exception as exc:
                log.warning("%s failed: %s — trying next provider", ProviderClass.__name__, exc)
        raise RuntimeError("All temp-email providers failed")

    async def wait_for_email(self) -> Optional[str]:
        if not self._provider:
            raise RuntimeError("Call create() before wait_for_email()")
        return await self._provider.wait_for_email(timeout=self.timeout)

    async def close(self):
        if self._provider:
            await self._provider.close()

    @property
    def email(self) -> Optional[str]:
        return self._provider.email if self._provider else None

    # ── Link extraction ──────────────────────────────────────────────────────

    @staticmethod
    def extract_links(body: str) -> list[str]:
        """
        Extract all http(s) links from an email body (HTML or plain text).
        Verification-related links are ranked first.
        """
        if not body:
            return []
        raw   = _LINK_RE.findall(body)
        # Clean trailing punctuation artefacts from HTML
        links = [l.rstrip(".,;:)>\"'\\") for l in raw]
        # Remove duplicates while preserving order
        seen  = set()
        unique = []
        for l in links:
            if l not in seen:
                seen.add(l)
                unique.append(l)

        def _score(link: str) -> int:
            ll = link.lower()
            for i, kw in enumerate(_VERIFY_KEYWORDS):
                if kw in ll:
                    return len(_VERIFY_KEYWORDS) - i   # higher = better
            return 0

        unique.sort(key=_score, reverse=True)
        return unique
