"""
verification.py — Email verification link handler.

After registration, the site typically sends a verification email.
This module:
  1. Waits for an email via the TempMailProvider
  2. Extracts and ranks verification links
  3. Navigates to the best candidate link in the browser
  4. Waits for a success signal
"""

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .browser import Browser
    from .tempmail import TempMailProvider

log = logging.getLogger("autobot.verification")

# ─── Success signals after clicking verify link ───────────────────────────────

_POST_VERIFY_SUCCESS = [
    "verified", "confirmed", "activated", "success", "welcome",
    "you're all set", "you are now", "account is active", "email confirmed",
    "thank you", "congratulations",
]


class EmailVerifier:
    """
    Orchestrates waiting for the verification email and clicking through it.
    """

    def __init__(self, browser: "Browser", mail_provider: "TempMailProvider"):
        self._browser  = browser
        self._mail     = mail_provider

    async def verify(self, timeout: int = 300) -> bool:
        """
        Wait for the verification email and click through the link.

        Returns True if verification appears successful.
        Raises RuntimeError on timeout or no usable link.
        """
        log.info("Waiting for verification email (timeout=%ds)…", timeout)

        body = await self._mail.wait_for_email()
        if not body:
            raise RuntimeError(f"No verification email received within {timeout}s")

        links = self._mail.extract_links(body)
        if not links:
            raise RuntimeError("Verification email received but no links found")

        log.info("Found %d link(s) in email; best candidate: %s", len(links), links[0])

        # Try links in ranked order (top 3 max)
        for link in links[:3]:
            try:
                success = await self._click_and_verify(link)
                if success:
                    log.info("Email verified successfully via: %s", link)
                    return True
            except Exception as exc:
                log.warning("Verification link failed (%s): %s", link, exc)

        # Soft fail — link was clicked but success text not detected
        # (some sites redirect to a login page after verify, which is OK)
        log.warning("Could not positively confirm verification — proceeding anyway")
        return False

    async def _click_and_verify(self, link: str) -> bool:
        """Navigate to *link* and check for a success signal."""
        original_url = await self._browser.current_url()
        await self._browser.goto(link)
        await asyncio.sleep(2)

        # Check if page shows success
        try:
            content = (await self._browser.page_text()).lower()
            page_url = (await self._browser.current_url()).lower()
            for pat in _POST_VERIFY_SUCCESS:
                if pat in content or pat.replace(" ", "") in page_url:
                    return True
        except Exception:
            pass

        # Generic success: URL changed from what it was before
        current = await self._browser.current_url()
        if current != original_url and current != link:
            # Navigated somewhere — might be the dashboard/home page
            return True

        return False


class LoginVerifier:
    """
    Attempts to log in with the registered credentials to confirm
    account activation.
    """

    def __init__(self, browser: "Browser"):
        self._browser = browser

    async def attempt_login(
        self,
        url: str,
        creds: dict,
        login_path_hints: Optional[list[str]] = None,
    ) -> bool:
        """
        Try to find a login form and submit credentials.
        Returns True if login succeeds (URL changes or success text detected).
        """
        from .form_detector import FormDetector, FormFiller, _LOGIN_SIGNALS

        login_path_hints = login_path_hints or ["/login", "/signin", "/sign-in",
                                                  "/account/login", "/user/login"]

        # Try the base URL first, then common login paths
        candidates = [url] + [
            url.rstrip("/") + path for path in login_path_hints
        ]

        for candidate in candidates:
            try:
                await self._browser.goto(candidate)
                content = (await self._browser.page.content()).lower()

                # Quick check: does this page have a login form?
                if not any(sig in content for sig in _LOGIN_SIGNALS + ["password"]):
                    continue

                detector = FormDetector(self._browser)
                fm = await detector.detect()
                if not fm or not fm.password:
                    continue

                # For login, use email OR username depending on what's available
                login_creds = {
                    "email":    creds.get("email", ""),
                    "username": creds.get("username", ""),
                    "password": creds["password"],
                }

                original_url = self._browser.page.url
                filler = FormFiller(self._browser)
                try:
                    await filler.fill(fm, login_creds)
                except RuntimeError as e:
                    log.warning("Login fill error: %s", e)
                    continue

                await asyncio.sleep(2)
                current = await self._browser.current_url()
                success = await self._browser.detect_success()

                if current != original_url or success:
                    log.info("Login confirmed on %s", candidate)
                    return True

            except Exception as exc:
                log.debug("Login attempt on %s failed: %s", candidate, exc)

        log.warning("Could not confirm login — account may still be valid")
        return False
