"""
form_detector.py — Heuristic registration form detector and auto-filler.

Scores all <form> elements on the page to find the registration form,
identifies field types, fills them in, and submits.

Flags raised:
  captcha_detected   — a CAPTCHA widget is present (stop, ask human)
  phone_required     — a mandatory phone field found (may need user help)
  payment_required   — payment field detected (stop)
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .browser import Browser

log = logging.getLogger("autobot.form_detector")

# ─── Field classification keywords ───────────────────────────────────────────

_EMAIL_KW    = ["email", "e-mail", "mail"]
_PASS_KW     = ["password", "passwd", "pass", "pwd"]
_CONF_KW     = ["confirm", "repeat", "retype", "verify"]
_USER_KW     = ["username", "user_name", "user-name", "login", "handle", "nickname"]
_NAME_KW     = ["fullname", "full_name", "full-name", "displayname", "display_name"]
_FIRST_KW    = ["firstname", "first_name", "first-name", "fname", "given"]
_LAST_KW     = ["lastname", "last_name", "last-name", "lname", "surname", "family"]
_PHONE_KW    = ["phone", "mobile", "tel", "cell"]
_PAYMENT_KW  = ["card", "credit", "cvv", "expiry", "billing", "payment"]
_TOS_KW      = ["agree", "terms", "tos", "privacy", "accept"]

# Form scoring — fields that suggest registration (not login)
_REG_SIGNALS = ["register", "signup", "sign-up", "create", "join", "new account",
                 "get started", "create account"]
_LOGIN_SIGNALS = ["login", "log-in", "log in", "sign in", "signin"]


@dataclass
class FieldMap:
    """Classified fields found in a registration form."""
    email:            Optional[str] = None   # CSS selector
    password:         Optional[str] = None
    confirm_password: Optional[str] = None
    username:         Optional[str] = None
    display_name:     Optional[str] = None
    first_name:       Optional[str] = None
    last_name:        Optional[str] = None
    tos_checkbox:     Optional[str] = None
    submit_btn:       Optional[str] = None
    captcha_detected: bool = False
    phone_required:   bool = False
    payment_required: bool = False
    form_selector:    Optional[str] = None
    confidence:       float = 0.0


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _attr_text(el: dict) -> str:
    """Return a lowercase string of name+id+placeholder+aria-label for scoring."""
    parts = [
        el.get("name", ""),
        el.get("id", ""),
        el.get("placeholder", ""),
        el.get("aria-label", ""),
        el.get("autocomplete", ""),
        el.get("label_text", ""),
    ]
    return " ".join(p for p in parts if p).lower()


def _matches(text: str, keywords: list[str]) -> bool:
    return any(kw in text for kw in keywords)


# ─── Main detector ────────────────────────────────────────────────────────────

class FormDetector:
    """
    Uses Playwright's page evaluation to introspect all forms,
    classify their fields, score them, and return the best FieldMap.
    """

    def __init__(self, browser: "Browser"):
        self._browser = browser

    async def detect(self) -> Optional[FieldMap]:
        """
        Scan the current page for a registration form.
        Returns FieldMap if found, else None.
        """
        page = self._browser.page

        # --- Extract form data from DOM via page.evaluate ---
        raw_forms = await page.evaluate("""
        () => {
            const forms = Array.from(document.querySelectorAll('form'));
            return forms.map((form, fi) => {
                const inputs = Array.from(form.querySelectorAll('input, select, textarea, button'));
                const formText = (form.textContent || '').toLowerCase();
                return {
                    index: fi,
                    formText: formText.substring(0, 400),
                    action: form.action || '',
                    method: form.method || '',
                    inputs: inputs.map((el, ii) => {
                        // Try to find associated <label>
                        let labelText = '';
                        if (el.id) {
                            const lbl = document.querySelector(`label[for="${el.id}"]`);
                            if (lbl) labelText = lbl.textContent.trim();
                        }
                        if (!labelText) {
                            const parent = el.closest('label');
                            if (parent) labelText = parent.textContent.trim();
                        }
                        return {
                            tag:         el.tagName.toLowerCase(),
                            type:        el.type || '',
                            name:        el.name || '',
                            id:          el.id || '',
                            placeholder: el.placeholder || '',
                            'aria-label': el.getAttribute('aria-label') || '',
                            autocomplete: el.autocomplete || '',
                            required:    el.required,
                            label_text:  labelText,
                            fi: fi,
                            ii: ii,
                            selector: el.id
                                ? `#${CSS.escape(el.id)}`
                                : el.name
                                    ? `form:nth-of-type(${fi+1}) [name="${el.name}"]`
                                    : `form:nth-of-type(${fi+1}) input:nth-of-type(${ii+1})`
                        };
                    })
                };
            });
        }
        """)

        if not raw_forms:
            log.warning("No forms found on page")
            return None

        best_map:   Optional[FieldMap] = None
        best_score: float = -1.0

        for form_data in raw_forms:
            fm = self._classify_form(form_data)
            if fm.confidence > best_score:
                best_score = fm.confidence
                best_map   = fm

        if best_map and best_map.confidence >= 0.3:
            log.info("Best form found (confidence=%.2f)", best_map.confidence)
            return best_map

        log.warning("No registration form found with sufficient confidence")
        return None

    def _classify_form(self, form_data: dict) -> FieldMap:
        fi         = form_data["index"]
        form_text  = form_data.get("formText", "")
        inputs     = form_data.get("inputs", [])
        fm         = FieldMap(form_selector=f"form:nth-of-type({fi+1})")
        score      = 0.0

        # Score form-level text signals
        for sig in _REG_SIGNALS:
            if sig in form_text:
                score += 0.15
                break
        for sig in _LOGIN_SIGNALS:
            if sig in form_text:
                score -= 0.1
                break

        has_password_confirm = False

        for el in inputs:
            t    = el.get("type", "").lower()
            text = _attr_text(el)
            sel  = el["selector"]

            # Skip hidden / submit inputs from field classification
            if t in ("hidden", "image"):
                continue

            # Submit button
            if t in ("submit", "button") or el.get("tag") == "button":
                if _matches(text + el.get("tag", ""), _REG_SIGNALS + ["submit", "continue", "next", "register", "create"]):
                    fm.submit_btn = sel
                continue

            # CAPTCHA
            if t == "checkbox" and _matches(text, ["captcha", "robot", "human"]):
                fm.captcha_detected = True
                continue

            # Check class / data attributes for captcha
            if any(kw in str(el).lower() for kw in ["recaptcha", "hcaptcha", "turnstile"]):
                fm.captcha_detected = True
                continue

            # ToS checkbox
            if t == "checkbox" and _matches(text, _TOS_KW):
                fm.tos_checkbox = sel
                continue

            # Payment
            if _matches(text, _PAYMENT_KW):
                fm.payment_required = True
                continue

            # Phone
            if t in ("tel",) or _matches(text, _PHONE_KW):
                if el.get("required"):
                    fm.phone_required = True
                continue

            # Email
            if t == "email" or _matches(text, _EMAIL_KW):
                if not fm.email:
                    fm.email = sel
                    score += 0.25
                continue

            # Password (confirm first, then main)
            if t == "password" or _matches(text, _PASS_KW):
                if _matches(text, _CONF_KW) or has_password_confirm:
                    fm.confirm_password = sel
                    has_password_confirm = True
                    score += 0.10
                else:
                    fm.password = sel
                    has_password_confirm = True
                    score += 0.20
                continue

            # Username
            if _matches(text, _USER_KW):
                if not fm.username:
                    fm.username = sel
                    score += 0.15
                continue

            # Display name
            if _matches(text, _NAME_KW):
                if not fm.display_name:
                    fm.display_name = sel
                    score += 0.10
                continue

            # First name
            if _matches(text, _FIRST_KW):
                fm.first_name = sel
                score += 0.05
                continue

            # Last name
            if _matches(text, _LAST_KW):
                fm.last_name = sel
                score += 0.05
                continue

        # A form with only email+password is likely a login, not register
        if fm.email and fm.password and not fm.confirm_password and not fm.username:
            score -= 0.15

        fm.confidence = max(0.0, min(1.0, score))
        return fm


# ─── Form filler ──────────────────────────────────────────────────────────────

class FormFiller:
    """Fills a detected FieldMap with provided credentials and submits."""

    def __init__(self, browser: "Browser"):
        self._browser = browser

    async def fill(self, fm: FieldMap, creds: dict) -> bool:
        """
        Fill the form fields with *creds* and submit.

        Returns True if submission appears to have succeeded (URL changed
        or success text detected).
        Raises RuntimeError on CAPTCHA / payment blockers.
        """
        if fm.captcha_detected:
            raise RuntimeError("CAPTCHA detected — human interaction required")
        if fm.payment_required:
            raise RuntimeError("Payment required — stopping")
        if fm.phone_required:
            raise RuntimeError("Mandatory phone field detected — cannot automate")

        page = self._browser.page
        original_url = page.url

        async def _type(selector: Optional[str], value: str):
            if not selector or not value:
                return
            try:
                el = page.locator(selector).first
                await el.scroll_into_view_if_needed(timeout=3000)
                await el.click(timeout=3000)
                await el.fill(value)
                await asyncio.sleep(0.1)
            except Exception as exc:
                log.debug("Could not fill %s: %s", selector, exc)

        # Fill all identified fields
        await _type(fm.email,            creds.get("email", ""))
        await _type(fm.username,         creds.get("username", ""))
        await _type(fm.display_name,     creds.get("display_name", ""))
        await _type(fm.first_name,       creds.get("first_name", ""))
        await _type(fm.last_name,        creds.get("last_name", ""))
        await _type(fm.password,         creds.get("password", ""))
        await _type(fm.confirm_password, creds.get("password", ""))  # same value

        # Check ToS checkbox
        if fm.tos_checkbox:
            try:
                chk = page.locator(fm.tos_checkbox).first
                if not await chk.is_checked():
                    await chk.check()
            except Exception as exc:
                log.debug("ToS checkbox error: %s", exc)

        await asyncio.sleep(0.5)

        # Submit
        submitted = False
        if fm.submit_btn:
            try:
                btn = page.locator(fm.submit_btn).first
                await btn.click(timeout=5000)
                submitted = True
            except Exception as exc:
                log.warning("Submit button click failed: %s", exc)

        if not submitted:
            # Fallback: press Enter on password field
            try:
                if fm.password:
                    await page.locator(fm.password).first.press("Enter")
                    submitted = True
            except Exception:
                pass

        if not submitted:
            log.error("Could not submit form")
            return False

        # Wait for navigation or success signal
        await asyncio.sleep(2)
        await self._browser.wait_for_url_change(original_url, timeout=10_000)

        success = await self._browser.detect_success()
        captcha = await self._browser.detect_captcha()

        if captcha:
            raise RuntimeError("CAPTCHA appeared after form submit")

        log.info("Form submitted (success_detected=%s)", success)
        return success
