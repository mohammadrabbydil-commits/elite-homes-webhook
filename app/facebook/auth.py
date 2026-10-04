"""Facebook Graph API token management.

Short-lived user tokens (the kind the Graph API Explorer hands out) expire in
about an hour. This module exchanges them for long-lived tokens (~60 days) and
derives the never-expiring Page token used for posting.

Token lifecycle for this POC:
    1. Grab a short-lived *user* token from the Graph API Explorer.
    2. exchange_for_long_lived_token()  -> ~60-day user token
    3. get_page_access_token()          -> Page token that does not expire
    4. Paste the result into config/.env as FB_PAGE_ACCESS_TOKEN
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from app.config import ENV_FILE, settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class FacebookAuthError(RuntimeError):
    """Raised when a token exchange or validation call fails."""


# Access tokens are long, unbroken, and URL-safe. Anything else is a paste
# mistake - catching it here gives a clear message instead of the Graph API's
# opaque "Cannot parse access token" (code 190).
_PLACEHOLDER_MARKERS = ("<", ">", "paste", "your-", "same-token", "new-token", "token-here")


def validate_token_format(token: str, label: str = "token") -> str:
    """Reject obviously malformed tokens before spending an API call.

    Returns the stripped token, or raises FacebookAuthError explaining what
    looks wrong.
    """
    if token is None:
        raise FacebookAuthError(f"No {label} supplied.")

    cleaned = token.strip().strip('"').strip("'")

    if not cleaned:
        raise FacebookAuthError(f"The {label} is empty.")

    lowered = cleaned.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in lowered:
            raise FacebookAuthError(
                f"That looks like a placeholder, not a real {label}: {cleaned[:40]!r}\n"
                "Replace it with the actual token string copied from Facebook."
            )

    if any(char.isspace() for char in cleaned):
        raise FacebookAuthError(
            f"The {label} contains whitespace, so it was probably copied in "
            "parts or wrapped across lines. Copy it again in one piece."
        )

    if len(cleaned) < 50:
        raise FacebookAuthError(
            f"The {label} is only {len(cleaned)} characters. Real tokens are "
            "150+ characters, so this copy is truncated."
        )

    return cleaned


@dataclass(frozen=True)
class TokenInfo:
    """Result of debugging a token against /debug_token."""

    is_valid: bool
    app_id: str | None
    token_type: str | None
    scopes: list[str]
    expires_at: datetime | None
    user_id: str | None
    error: str | None = None

    @property
    def never_expires(self) -> bool:
        return self.is_valid and self.expires_at is None

    @property
    def days_remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        delta = self.expires_at - datetime.now(timezone.utc)
        return round(delta.total_seconds() / 86400, 1)


def _graph_get(path: str, params: dict) -> dict:
    """GET a Graph API endpoint, raising FacebookAuthError on any API error."""
    url = f"{settings.graph_base_url}/{path.lstrip('/')}"
    try:
        response = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise FacebookAuthError(f"Network error calling {path}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise FacebookAuthError(
            f"Non-JSON response from {path} (HTTP {response.status_code})"
        ) from exc

    if "error" in payload:
        err = payload["error"]
        raise FacebookAuthError(
            f"Graph API error on {path}: {err.get('message')} "
            f"(type={err.get('type')}, code={err.get('code')})"
        )
    if not response.ok:
        raise FacebookAuthError(f"HTTP {response.status_code} from {path}: {response.text[:300]}")

    return payload


def exchange_for_long_lived_token(short_lived_token: str) -> tuple[str, int | None]:
    """Exchange a short-lived user token for a long-lived one (~60 days).

    Returns:
        (long_lived_token, expires_in_seconds). `expires_in_seconds` is None
        when Facebook reports the token as non-expiring.
    """
    short_lived_token = validate_token_format(short_lived_token, "short-lived token")
    if not settings.fb_app_id or not settings.fb_app_secret:
        raise FacebookAuthError("FB_APP_ID and FB_APP_SECRET must be set in config/.env")

    logger.info("Exchanging short-lived token for a long-lived token...")
    payload = _graph_get(
        "oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": settings.fb_app_id,
            "client_secret": settings.fb_app_secret,
            "fb_exchange_token": short_lived_token,
        },
    )

    token = payload.get("access_token")
    if not token:
        raise FacebookAuthError(f"No access_token in exchange response: {payload}")

    expires_in = payload.get("expires_in")
    if expires_in:
        logger.info("Long-lived token acquired, expires in ~%.0f days", expires_in / 86400)
    else:
        logger.info("Long-lived token acquired (no expiry reported)")
    return token, expires_in


def get_page_access_token(user_access_token: str, page_id: str | None = None) -> str:
    """Derive the Page access token from a long-lived *user* token.

    A Page token derived from a long-lived user token does not expire, which is
    what we want for unattended scheduled posting.
    """
    page_id = page_id or settings.fb_page_id
    if not page_id:
        raise FacebookAuthError("FB_PAGE_ID must be set in config/.env")

    payload = _graph_get(
        page_id, {"fields": "access_token,name", "access_token": user_access_token}
    )
    token = payload.get("access_token")
    if not token:
        raise FacebookAuthError(
            f"No Page access_token returned for page {page_id}. "
            "Confirm the user is an admin of the Page and the token carries "
            "pages_manage_posts + pages_read_engagement."
        )
    logger.info("Page access token acquired for %r", payload.get("name", page_id))
    return token


def list_pages(user_access_token: str) -> list[dict]:
    """List the Pages this user administers, with IDs, names, and permissions.

    Use this to confirm which numeric ID is the "Elite Homes USA" Page. A
    facebook.com/profile.php?id=<n> URL is ambiguous: new-style Pages and
    personal profiles both use it, and only a Page can be posted to via the
    Graph API.
    """
    payload = _graph_get(
        "me/accounts",
        {"fields": "id,name,tasks", "access_token": user_access_token},
    )
    pages = payload.get("data", [])
    logger.info("User administers %d Page(s)", len(pages))
    return pages


def get_me(access_token: str) -> dict:
    """Identify the account a token belongs to."""
    return _graph_get("me", {"fields": "id,name", "access_token": access_token})


def describe_object(object_id: str, access_token: str) -> dict:
    """Fetch an object to determine whether it is a Page or a personal profile.

    Pages expose `category` and `fan_count`; personal profiles do not. Returns
    a dict with an `error` key rather than raising when the object is not
    visible to this token.
    """
    try:
        return _graph_get(
            object_id,
            {"fields": "id,name,category,fan_count", "access_token": access_token},
        )
    except FacebookAuthError as exc:
        return {"error": str(exc)}


def verify_token(access_token: str | None = None) -> TokenInfo:
    """Check whether a token is still valid via /debug_token.

    Never raises on an *invalid* token - it returns TokenInfo(is_valid=False)
    so callers can branch. Still raises FacebookAuthError on network/config
    problems, which are a different kind of failure.
    """
    token = access_token or settings.fb_page_access_token
    if not token:
        return TokenInfo(False, None, None, [], None, None, error="No token configured.")
    if not settings.fb_app_id or not settings.fb_app_secret:
        raise FacebookAuthError("FB_APP_ID and FB_APP_SECRET must be set to verify a token.")

    app_token = f"{settings.fb_app_id}|{settings.fb_app_secret}"
    try:
        payload = _graph_get("debug_token", {"input_token": token, "access_token": app_token})
    except FacebookAuthError as exc:
        logger.warning("Token verification failed: %s", exc)
        return TokenInfo(False, None, None, [], None, None, error=str(exc))

    data = payload.get("data", {})
    expires_raw = data.get("expires_at") or 0
    expires_at = datetime.fromtimestamp(expires_raw, tz=timezone.utc) if expires_raw else None

    info = TokenInfo(
        is_valid=bool(data.get("is_valid")),
        app_id=data.get("app_id"),
        token_type=data.get("type"),
        scopes=list(data.get("scopes", [])),
        expires_at=expires_at,
        user_id=data.get("user_id"),
        error=(data.get("error") or {}).get("message"),
    )

    if info.is_valid:
        window = "never expires" if info.never_expires else f"{info.days_remaining} days left"
        logger.info("Token is valid (%s, %s)", info.token_type, window)
    else:
        logger.warning("Token is INVALID: %s", info.error)
    return info


def token_expiring_soon(threshold_days: int = 7, access_token: str | None = None) -> bool:
    """True when the token is invalid or expires within `threshold_days`."""
    info = verify_token(access_token)
    if not info.is_valid:
        return True
    if info.never_expires:
        return False
    return (info.expires_at - datetime.now(timezone.utc)) < timedelta(days=threshold_days)


def persist_token_to_env(token: str, env_path: Path | None = None) -> None:
    """Write FB_PAGE_ACCESS_TOKEN into config/.env, preserving the other lines.

    Creates the file if it does not exist. Only the token line is touched.
    """
    env_path = env_path or ENV_FILE
    env_path.parent.mkdir(parents=True, exist_ok=True)

    key = "FB_PAGE_ACCESS_TOKEN"
    new_line = f"{key}={token}"

    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = new_line
            break
    else:
        lines.append(new_line)

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote %s to %s", key, env_path)


def bootstrap_page_token(short_lived_token: str, persist: bool = True) -> str:
    """Full one-shot flow: short-lived user token -> persisted Page token."""
    long_lived, _ = exchange_for_long_lived_token(short_lived_token)
    page_token = get_page_access_token(long_lived)
    if persist:
        persist_token_to_env(page_token)
    return page_token
