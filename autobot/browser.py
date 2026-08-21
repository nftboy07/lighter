"""
browser.py — Playwright Chromium wrapper for autobot.

Features:
- Stealth mode (randomized user-agent, disables navigator.webdriver)
- Auto-dismiss cookie consent banners
- Popup / overlay dismissal
- Retry-with-backoff on navigation errors
- Screenshot on failure
- Proxy support via AUTOBOT_PROXY env var
- Headless/headed mode via AUTOBOT_HEADLESS env var
"""

import asyncio
import logging
import os
import random
from pathlib import Path
from typing import Optional

log = logging.getLogger("autobot.browser")

# ─── User-agent pool ──────────────────────────────────────────────────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# ─── Cookie consent selectors (ordered by specificity) ────────────────────────

_CONSENT_SELECTORS = [
    # Generic accept/agree buttons
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept cookies')",
    "button:has-text('Accept Cookies')",
    "button:has-text('I Accept')",
    "button:has-text('I agree')",
    "button:has-text('Agree')",
    "button:has-text('Got it')",
    "button:has-text('OK')",
    "button:has-text('Allow all')",
    "button:has-text('Allow All')",
    "button:has-text('Consent')",
    # Common IDs / classes
    "#onetrust-accept-btn-handler",
    "#accept-all-cookies",
    ".cookie-accept",
    ".cookie-consent__accept",
    "[data-testid='cookie-accept']",
    "[aria-label='Accept cookies']",
]

# ─── CAPTCHA detection patterns ───────────────────────────────────────────────

_CAPTCHA_INDICATORS = [
    "g-recaptcha",
    "h-captcha",
    "cf-turnstile",           # Cloudflare Turnstile
    "data-sitekey",
    "recaptcha/api.js",
    "hcaptcha.com/1/api.js",
    "challenges.cloudflare.com",
    "arkoselabs.com",
    "funcaptcha",
]

# ─── Success page patterns ────────────────────────────────────────────────────

_SUCCESS_PATTERNS = [
    "successfully", "welcome", "confirmed", "verified", "activated",
    "account created", "registration complete", "you're in", "thank you",
    "check your email", "email sent",
]


class Browser:
    """Async context-manager wrapping a Playwright Chromium browser instance."""

    def __init__(self):
        self._playwright = None
        self._browser    = None
        self._context    = None
        self.page        = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        from playwright.async_api import async_playwright   # lazy import

        headless = os.getenv("AUTOBOT_HEADLESS", "true").lower() not in ("false", "0", "no")
        proxy_url = os.getenv("AUTOBOT_PROXY", "").strip() or None
        ua = random.choice(_USER_AGENTS)

        self._playwright = await async_playwright().start()

        launch_opts: dict = {"headless": headless}
        if proxy_url:
            launch_opts["proxy"] = {"server": proxy_url}

        self._browser = await self._playwright.chromium.launch(**launch_opts)

        ctx_opts: dict = {
            "user_agent": ua,
            "viewport":   {"width": 1366 + random.randint(-20, 20),
                           "height": 768 + random.randint(-10, 10)},
            "locale":     "en-US",
            "timezone_id": "America/New_York",
        }

        self._context = await self._browser.new_context(**ctx_opts)

        # Stealth: remove webdriver fingerprint
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        self.page = await self._context.new_page()

        # Dismiss dialogs automatically
        self.page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

        log.info("Browser started (headless=%s, ua=%.60s…)", headless, ua)

    async def stop(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        log.info("Browser stopped")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.stop()

    # ── Navigation ────────────────────────────────────────────────────────────

    async def goto(self, url: str, retries: int = 3, timeout: int = 30_000):
        """Navigate to *url* with retry-backoff on network errors."""
        for attempt in range(1, retries + 1):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                await self._dismiss_consent()
                return
            except Exception as exc:
                log.warning("goto %s attempt %d/%d failed: %s", url, attempt, retries, exc)
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"Navigation to {url} failed after {retries} attempts")

    # ── Consent banner dismissal ───────────────────────────────────────────────

    async def _dismiss_consent(self):
        for sel in _CONSENT_SELECTORS:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=800):
                    await btn.click(timeout=800)
                    log.debug("Dismissed consent banner: %s", sel)
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                continue

    # ── CAPTCHA detection ─────────────────────────────────────────────────────

    async def detect_captcha(self) -> bool:
        """Return True if a CAPTCHA widget is detected on the current page."""
        try:
            content = await self.page.content()
            for indicator in _CAPTCHA_INDICATORS:
                if indicator in content:
                    log.warning("CAPTCHA detected: %s", indicator)
                    return True
        except Exception:
            pass
        return False

    # ── Success detection ─────────────────────────────────────────────────────

    async def detect_success(self) -> bool:
        """Return True if the page shows registration/verification success signals."""
        try:
            content = (await self.page.content()).lower()
            url     = self.page.url.lower()
            for pat in _SUCCESS_PATTERNS:
                if pat in content or pat.replace(" ", "") in url:
                    return True
        except Exception:
            pass
        return False

    # ── Cookies ───────────────────────────────────────────────────────────────

    async def get_cookies(self) -> list[dict]:
        return await self._context.cookies()

    async def set_cookies(self, cookies: list[dict]):
        await self._context.add_cookies(cookies)

    # ── Screenshot ────────────────────────────────────────────────────────────

    async def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot saved: %s", path)
        return path

    # ── Utilities ─────────────────────────────────────────────────────────────

    async def current_url(self) -> str:
        return self.page.url

    async def page_text(self) -> str:
        try:
            return await self.page.inner_text("body")
        except Exception:
            return ""

    async def find_link(self, url_fragment: str) -> Optional[str]:
        """Find the href of the first <a> whose href contains *url_fragment*."""
        try:
            el = self.page.locator(f"a[href*='{url_fragment}']").first
            if await el.is_visible(timeout=2000):
                return await el.get_attribute("href")
        except Exception:
            pass
        return None

    async def wait_for_url_change(self, original_url: str, timeout: int = 15_000):
        """Wait until the page URL differs from *original_url*."""
        try:
            await self.page.wait_for_function(
                f"() => window.location.href !== {repr(original_url)}",
                timeout=timeout,
            )
        except Exception:
            pass   # Not fatal — caller will check
