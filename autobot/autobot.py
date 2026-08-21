"""
autobot.py — Autonomous Web Account Registration Agent (CLI entry point)
========================================================================

Usage:
    python autobot/autobot.py <url> [options]

Examples:
    python autobot/autobot.py https://reddit.com
    python autobot/autobot.py https://discord.com --timeout 180
    python autobot/autobot.py https://example.com --no-login --cleanup

The agent will:
  1. Generate a disposable email address
  2. Detect and fill the site's registration form
  3. Wait for and click the verification email link
  4. Attempt to log in to confirm activation
  5. Save credentials to workspace/credentials.json
  6. Print a structured report to stdout

Stops and reports when blocked by: CAPTCHA, MFA, payment, mandatory phone.
Retries up to 3 times with fresh credentials on recoverable failures.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env or .env.autobot if present
load_dotenv(dotenv_path=Path(".env"))
load_dotenv(dotenv_path=Path(".env.autobot"), override=False)

from .reporter     import AutobotResult, print_report
from .tempmail     import TempMailProvider
from .browser      import Browser
from .form_detector import FormDetector, FormFiller
from .credential_gen import generate_credentials
from .verification  import EmailVerifier, LoginVerifier
from .session_store import save_result, save_cookies, screenshot_path

log = logging.getLogger("autobot")

# ─── Logging setup ────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)
    # Quiet noisy libraries
    for lib in ("httpx", "httpcore", "playwright", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)


# ─── Core orchestrator ────────────────────────────────────────────────────────

class Autobot:
    """
    Orchestrates the full registration flow with up to *max_retries* attempts.
    Each attempt uses fresh credentials and a fresh disposable email.
    """

    MAX_RETRIES = 3

    def __init__(
        self,
        url:         str,
        timeout:     int  = 300,
        skip_login:  bool = False,
        cleanup:     bool = False,
        verbose:     bool = False,
    ):
        self.url        = url.rstrip("/")
        self.timeout    = timeout
        self.skip_login = skip_login
        self.cleanup    = cleanup
        self.verbose    = verbose

    async def run(self) -> AutobotResult:
        result = AutobotResult(website=self.url)

        for attempt in range(1, self.MAX_RETRIES + 1):
            result.attempts = attempt
            log.info("═" * 60)
            log.info("Attempt %d/%d — %s", attempt, self.MAX_RETRIES, self.url)
            log.info("═" * 60)

            mail_provider = TempMailProvider(timeout=self.timeout)
            try:
                email = await mail_provider.create()
            except Exception as exc:
                result.status = "FAILED"
                result.notes  = f"Temp-email creation failed: {exc}"
                log.error("Temp-email failed: %s", exc)
                continue

            creds = generate_credentials(email)
            log.info("Generated credentials for: %s", email)

            async with Browser() as browser:
                try:
                    outcome = await self._attempt(
                        browser, mail_provider, creds, result
                    )
                    if outcome:
                        # Save cookies
                        try:
                            cookies = await browser.get_cookies()
                            save_cookies(self.url, cookies)
                        except Exception:
                            pass
                        # Persist to credentials.json
                        save_result(result)
                        await mail_provider.close()
                        return result

                except _BlockerError as exc:
                    # Non-retryable: CAPTCHA, payment, etc.
                    result.status = "BLOCKED"
                    result.notes  = str(exc)
                    log.warning("BLOCKED: %s", exc)
                    # Save screenshot if available
                    shot_path = screenshot_path(self.url, f"_blocked_attempt{attempt}")
                    try:
                        await browser.screenshot(shot_path)
                        result.screenshot = str(shot_path)
                    except Exception:
                        pass
                    await mail_provider.close()
                    save_result(result)
                    return result   # Do NOT retry blockers

                except Exception as exc:
                    result.status = "FAILED"
                    result.notes  = f"Attempt {attempt} error: {exc}"
                    log.warning("Attempt %d failed: %s", attempt, exc)
                    shot_path = screenshot_path(self.url, f"_fail_attempt{attempt}")
                    try:
                        await browser.screenshot(shot_path)
                        result.screenshot = str(shot_path)
                    except Exception:
                        pass

                finally:
                    await mail_provider.close()

        # All retries exhausted
        log.error("All %d attempts failed for %s", self.MAX_RETRIES, self.url)
        save_result(result)
        return result

    async def _attempt(
        self,
        browser:       Browser,
        mail_provider: TempMailProvider,
        creds:         dict,
        result:        AutobotResult,
    ) -> bool:
        """
        Single registration attempt.
        Returns True on success, raises _BlockerError on non-retryable blocks,
        raises generic Exception on retryable failures.
        """

        # ── Step 1: Open site ────────────────────────────────────────────────
        log.info("Opening %s…", self.url)
        await browser.goto(self.url)

        # ── Step 2: Check for CAPTCHA on landing page ────────────────────────
        if await browser.detect_captcha():
            raise _BlockerError("CAPTCHA on landing page — human interaction required")

        # ── Step 3: Find registration form ───────────────────────────────────
        log.info("Searching for registration form…")
        detector = FormDetector(browser)
        fm = await detector.detect()

        # If no form on root, try common registration paths
        if fm is None or fm.confidence < 0.3:
            for path in ["/register", "/signup", "/sign-up", "/join",
                         "/create-account", "/account/register", "/users/sign_up"]:
                reg_url = self.url + path
                try:
                    await browser.goto(reg_url)
                    fm = await detector.detect()
                    if fm and fm.confidence >= 0.3:
                        log.info("Found registration form at: %s", reg_url)
                        break
                except Exception:
                    continue

        if fm is None or fm.confidence < 0.3:
            raise RuntimeError("No registration form found (confidence too low)")

        log.info(
            "Form found (confidence=%.2f) — email=%s, user=%s, pass=%s, confirm=%s",
            fm.confidence, bool(fm.email), bool(fm.username),
            bool(fm.password), bool(fm.confirm_password),
        )

        # ── Step 4: Raise blockers ───────────────────────────────────────────
        if fm.captcha_detected:
            raise _BlockerError("CAPTCHA widget detected in registration form")
        if fm.payment_required:
            raise _BlockerError("Payment required — cannot automate")
        if fm.phone_required:
            raise _BlockerError("Mandatory phone field — cannot automate without real number")

        # ── Step 5: Fill & submit ────────────────────────────────────────────
        log.info("Filling registration form…")
        filler  = FormFiller(browser)
        try:
            await filler.fill(fm, creds)
        except RuntimeError as exc:
            # FormFiller raises RuntimeError on blocker fields
            raise _BlockerError(str(exc)) from exc

        # ── Step 6: Post-submit CAPTCHA check ────────────────────────────────
        if await browser.detect_captcha():
            raise _BlockerError("CAPTCHA appeared after form submission")

        # Update result with generated credentials
        result.email    = creds["email"]
        result.username = creds["username"]
        result.password = creds["password"]
        result.notes    = "Form submitted; awaiting email verification…"

        # ── Step 7: Email verification ───────────────────────────────────────
        log.info("Waiting for verification email…")
        verifier = EmailVerifier(browser, mail_provider)
        try:
            verified = await verifier.verify(timeout=self.timeout)
        except RuntimeError as exc:
            result.notes = f"Email verification failed: {exc}"
            log.warning("Email verification failed: %s", exc)
            verified = False

        result.notes = "Email verified." if verified else "Email verification unclear — proceeding."

        # ── Step 8: Login confirmation ───────────────────────────────────────
        if not self.skip_login:
            log.info("Attempting login to confirm activation…")
            login_verifier = LoginVerifier(browser)
            logged_in = await login_verifier.attempt_login(self.url, creds)
            if logged_in:
                result.notes += " Login confirmed."
            else:
                result.notes += " Login confirmation unclear."

        result.status = "SUCCESS"
        log.info("Registration complete for %s", creds["email"])
        return True


class _BlockerError(Exception):
    """Non-retryable blocker: CAPTCHA, payment, phone requirement, etc."""
    pass


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="autobot",
        description="Autonomous web account registration agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python autobot/autobot.py https://reddit.com
  python autobot/autobot.py https://example.com --timeout 180
  python autobot/autobot.py https://example.com --no-login --headless false
  python autobot/autobot.py https://example.com --cleanup
""",
    )
    parser.add_argument("url",             help="Target website URL")
    parser.add_argument("--timeout",  "-t", type=int, default=int(os.getenv("AUTOBOT_TIMEOUT", 300)),
                        help="Seconds to wait for verification email (default: 300)")
    parser.add_argument("--no-login",       action="store_true",
                        help="Skip login confirmation step")
    parser.add_argument("--cleanup",        action="store_true",
                        help="Delete workspace directory after run")
    parser.add_argument("--headless",       default=None,
                        help="Override AUTOBOT_HEADLESS (true/false)")
    parser.add_argument("--proxy",          default=None,
                        help="Proxy URL override (e.g. socks5://host:port)")
    parser.add_argument("--verbose",  "-v", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--json",           action="store_true",
                        help="Output result as JSON instead of formatted text")
    return parser.parse_args()


async def _main():
    args = _parse_args()
    _setup_logging(args.verbose)

    # Apply CLI overrides to env
    if args.headless is not None:
        os.environ["AUTOBOT_HEADLESS"] = args.headless
    if args.proxy:
        os.environ["AUTOBOT_PROXY"] = args.proxy

    # Validate URL
    url = args.url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    bot = Autobot(
        url        = url,
        timeout    = args.timeout,
        skip_login = args.no_login,
        cleanup    = args.cleanup,
        verbose    = args.verbose,
    )

    result = await bot.run()

    if args.json:
        print(result.to_json())
    else:
        print_report(result)

    if args.cleanup:
        from .session_store import delete_workspace
        delete_workspace()
        log.info("Workspace deleted (--cleanup)")

    # Exit code: 0 = success, 1 = failed/blocked
    sys.exit(0 if result.succeeded() else 1)


def run():
    """Public entry point for programmatic use."""
    asyncio.run(_main())


if __name__ == "__main__":
    run()
