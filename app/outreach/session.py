"""Playwright browser session and cookie management for the outreach module.

  COMPLIANCE NOTICE
  -----------------
  Logging into facebook.com with stored credentials and driving the UI with an
  automation framework violates Meta's Terms of Service (Section 3.2.3,
  "Automated Data Collection") and the Platform Terms. Accounts detected doing
  this are typically disabled without appeal, and a disabled personal profile
  takes the Pages it administers down with it - including the "Elite Homes USA"
  Page this POC posts to through the sanctioned Graph API.

  The browser lifecycle below (launch, context, cookie persistence) is real and
  harmless. `login()` is deliberately left unimplemented. See the README section
  "Outreach: compliant alternatives" for supported ways to do this work.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import TracebackType

from app.config import BASE_DIR, settings

logger = logging.getLogger(__name__)

COOKIE_FILE = BASE_DIR / "data" / "fb_session_cookies.json"
FACEBOOK_URL = "https://www.facebook.com"


class OutreachSessionError(RuntimeError):
    """Raised when a browser session cannot be established."""


class NotImplementedByDesign(NotImplementedError):
    """Raised by stubs left unimplemented for Terms-of-Service reasons."""


class FacebookSession:
    """Manages a persistent Playwright browser context.

    Usage:
        with FacebookSession(headless=False) as fb:
            fb.load_cookies()
            page = fb.new_page()
    """

    def __init__(self, headless: bool = True, cookie_file: Path | None = None) -> None:
        self.headless = headless
        self.cookie_file = cookie_file or COOKIE_FILE
        self._playwright = None
        self._browser = None
        self._context = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> "FacebookSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise OutreachSessionError(
                "Playwright is not installed. Run: pip install playwright "
                "&& python -m playwright install chromium"
            ) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        logger.info("Browser session started (headless=%s)", self.headless)
        return self

    def close(self) -> None:
        for resource, name in (
            (self._context, "context"),
            (self._browser, "browser"),
            (self._playwright, "playwright"),
        ):
            if resource is None:
                continue
            try:
                resource.stop() if name == "playwright" else resource.close()
            except Exception as exc:  # pragma: no cover - teardown is best-effort
                logger.debug("Error closing %s: %s", name, exc)
        self._context = self._browser = self._playwright = None
        logger.info("Browser session closed")

    def __enter__(self) -> "FacebookSession":
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- pages and cookies -------------------------------------------------

    def new_page(self):
        if self._context is None:
            raise OutreachSessionError("Session not started. Call start() first.")
        return self._context.new_page()

    def save_cookies(self) -> None:
        """Persist the current context cookies so a later run can reuse them."""
        if self._context is None:
            raise OutreachSessionError("Session not started.")
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        cookies = self._context.cookies()
        self.cookie_file.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        logger.info("Saved %d cookie(s) to %s", len(cookies), self.cookie_file.name)

    def load_cookies(self) -> bool:
        """Restore cookies from disk. Returns False when no cookie file exists."""
        if self._context is None:
            raise OutreachSessionError("Session not started.")
        if not self.cookie_file.exists():
            logger.info("No saved cookies at %s", self.cookie_file)
            return False
        cookies = json.loads(self.cookie_file.read_text(encoding="utf-8"))
        self._context.add_cookies(cookies)
        logger.info("Loaded %d cookie(s) from %s", len(cookies), self.cookie_file.name)
        return True

    def clear_cookies(self) -> None:
        if self.cookie_file.exists():
            self.cookie_file.unlink()
            logger.info("Cleared saved cookies")

    # --- login (intentionally not implemented) -----------------------------

    def login(self, email: str | None = None, password: str | None = None) -> None:
        """Automated credential login - NOT IMPLEMENTED.

        Driving Facebook's login form with stored credentials violates Meta's
        Terms of Service and risks permanent loss of the profile and the Pages
        it administers.

        If you have made an informed decision to accept that risk, the supported
        manual path is:

            1. Run with headless=False.
            2. Call `interactive_login()` and sign in by hand in the window,
               completing any 2FA or checkpoint challenge yourself.
            3. Call `save_cookies()` so subsequent runs reuse the session.

        That path keeps a human in the loop for authentication, which is the
        part automation is least able to do safely.
        """
        raise NotImplementedByDesign(
            "Automated credential login is not implemented: it violates Meta's "
            "Terms of Service and risks a permanent ban on the profile and the "
            "Elite Homes USA Page. Use interactive_login() instead, or see the "
            "README section 'Outreach: compliant alternatives'."
        )

    def interactive_login(self, timeout_seconds: int = 300):
        """Open a visible browser at facebook.com for a human to sign in.

        Blocks until the login form is gone or `timeout_seconds` elapses, then
        saves cookies. Requires headless=False to be useful.
        """
        if self.headless:
            raise OutreachSessionError(
                "interactive_login() needs a visible browser. "
                "Construct FacebookSession(headless=False)."
            )

        page = self.new_page()
        page.goto(FACEBOOK_URL, wait_until="domcontentloaded")
        logger.info("Sign in manually in the browser window (waiting up to %ds)...", timeout_seconds)

        try:
            page.wait_for_selector("input[name='pass']", state="detached", timeout=timeout_seconds * 1000)
        except Exception as exc:
            raise OutreachSessionError(
                f"Login was not completed within {timeout_seconds}s: {exc}"
            ) from exc

        self.save_cookies()
        logger.info("Interactive login complete, cookies saved")
        return page


def credentials_configured() -> bool:
    """Whether FB_USER_EMAIL / FB_USER_PASSWORD are both set."""
    return bool(settings.fb_user_email and settings.fb_user_password)
