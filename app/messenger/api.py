"""Messenger Platform send API.

This is the sanctioned way to message people on Facebook: it replies only to
someone who messaged the Page first, inside Meta's 24-hour standard messaging
window. It is unrelated to the unsolicited-DM automation in app/outreach,
which is not implemented for Terms-of-Service reasons.

Requires the `pages_messaging` permission, which needs App Review before it
works outside Development mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


@dataclass
class SendResult:
    """Outcome of a send attempt."""

    success: bool
    message_id: str | None = None
    error: str | None = None
    error_code: int | None = None

    @property
    def outside_window(self) -> bool:
        """True when the failure was the 24-hour messaging window closing.

        Code 10 subcode 2018278 is Meta's "message sent outside of allowed
        window". It is not retryable - the person must message again first.
        """
        return self.error_code == 10


def _post_path(path: str, payload: dict) -> tuple[bool, dict]:
    """POST to an arbitrary Graph API object path (not just /me/...)."""
    url = f"{settings.graph_base_url}/{path}"
    try:
        response = requests.post(
            url,
            json=payload,
            params={"access_token": settings.fb_page_access_token},
            timeout=DEFAULT_TIMEOUT,
        )
        data = response.json()
    except requests.RequestException as exc:
        return False, {"error": {"message": f"Network error: {exc}"}}
    except ValueError as exc:
        return False, {"error": {"message": f"Invalid JSON response: {exc}"}}

    if "error" in data or not response.ok:
        return False, data
    return True, data


def _post(endpoint: str, payload: dict) -> tuple[bool, dict]:
    return _post_path(f"me/{endpoint}", payload)


def _result_from(ok: bool, data: dict, id_field: str = "message_id") -> SendResult:
    if not ok:
        err = data.get("error", {})
        return SendResult(success=False, error=err.get("message", "Unknown error"), error_code=err.get("code"))
    return SendResult(success=True, message_id=data.get(id_field) or data.get("id"))


def reply_to_comment(comment_id: str, message: str) -> SendResult:
    """Post a public reply under a comment - the visible half of the comment
    auto-reply (keyword hit -> public reply + a private reply, see below)."""
    if not message or not message.strip():
        raise ValueError("Comment reply text cannot be empty.")
    ok, data = _post_path(f"{comment_id}/comments", {"message": message})
    if not ok:
        logger.error("Failed to reply to comment %s: %s", comment_id, data.get("error"))
    return _result_from(ok, data)


def send_private_reply(comment_id: str, message: str) -> SendResult:
    """Open a Messenger DM from a comment via Facebook's Private Replies API.

    Must be sent within Meta's private-reply window (7 days of the comment).
    Requires `pages_manage_engagement` in addition to `pages_messaging`.
    """
    if not message or not message.strip():
        raise ValueError("Private reply text cannot be empty.")
    ok, data = _post_path(f"{comment_id}/private_replies", {"message": message})
    if not ok:
        logger.error("Private reply failed for comment %s: %s", comment_id, data.get("error"))
    return _result_from(ok, data)


def send_sender_action(psid: str, action: str) -> bool:
    """Send `mark_seen`, `typing_on`, or `typing_off` to a conversation.

    The typing indicator is what makes a delayed reply read as a person
    composing a message rather than a system pausing.
    """
    if action not in {"mark_seen", "typing_on", "typing_off"}:
        raise ValueError(f"Invalid sender action: {action}")

    ok, data = _post("messages", {"recipient": {"id": psid}, "sender_action": action})
    if not ok:
        logger.warning("Sender action %s failed for %s: %s", action, psid, data.get("error"))
    return ok


def send_text(psid: str, text: str, quick_replies: list[dict] | None = None) -> SendResult:
    """Send a plain text message to someone who messaged the Page first.

    `quick_replies` is Facebook's tappable-button payload: a list of
    {"content_type": "text", "title": ..., "payload": ...} dicts (max 13).
    """
    if not text or not text.strip():
        raise ValueError("Message text cannot be empty.")

    message: dict = {"text": text}
    if quick_replies:
        message["quick_replies"] = quick_replies

    ok, data = _post(
        "messages",
        {
            "recipient": {"id": psid},
            "messaging_type": "RESPONSE",
            "message": message,
        },
    )

    if not ok:
        err = data.get("error", {})
        message = err.get("message", "Unknown error")
        code = err.get("code")
        logger.error("Failed to message %s: %s (code=%s)", psid, message, code)
        return SendResult(success=False, error=message, error_code=code)

    message_id = data.get("message_id")
    logger.info("Sent message %s to %s", message_id, psid)
    return SendResult(success=True, message_id=message_id)


def get_user_profile(psid: str) -> dict:
    """Fetch the first and last name for a PSID, for personalising replies.

    Returns an empty dict rather than raising when the profile is unavailable,
    which happens routinely and should never block a reply.
    """
    try:
        response = requests.get(
            f"{settings.graph_base_url}/{psid}",
            params={
                "fields": "first_name,last_name",
                "access_token": settings.fb_page_access_token,
            },
            timeout=DEFAULT_TIMEOUT,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.debug("Could not fetch profile for %s: %s", psid, exc)
        return {}

    if "error" in data:
        logger.debug("Profile unavailable for %s: %s", psid, data["error"].get("message"))
        return {}
    return data
