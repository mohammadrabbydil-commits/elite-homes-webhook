"""Who is due a follow-up: conversations that stopped midway.

The client wants a message like "Hi Dejon, we wanted to follow up. Do you have
any questions?" when someone goes quiet for 5 days. Meta doesn't allow a bot
to send that: a Page may only message automatically within 24 hours of the
person's last message (Facebook rejects it with "sent outside of allowed
window"), and the 7-day HUMAN_AGENT tag is restricted to a human replying. A
person on the team can send it from the Page inbox for up to 7 days.

So this finds the people who are due and prepares the message; a human sends it.
It reads Facebook's own conversation history rather than our database, because
Render's free plan wipes the database on every restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from app.config import settings
from app.messenger.ai_agent import CLOSING_LINE
from app.messenger.api import DEFAULT_TIMEOUT, _parse_graph_time
from app.messenger.responder import OPT_OUT_REPLY, is_opt_out_request, person_first_name

logger = logging.getLogger(__name__)

FOLLOW_UP_AFTER_DAYS = 5
# Meta lets a human reply for 7 days after the person's last message.
HUMAN_WINDOW_DAYS = 7
FOLLOW_UP_MARKER = "wanted to follow up"
_PAGES = 4
_PAGE_SIZE = 25


@dataclass(frozen=True)
class FollowUp:
    psid: str
    name: str
    first_name: str | None
    last_message_at: datetime
    days_silent: float
    last_page_message: str
    suggested_message: str

    @property
    def days_left(self) -> float:
        return HUMAN_WINDOW_DAYS - self.days_silent

    @property
    def can_still_message(self) -> bool:
        return self.days_left > 0


def suggested_message(first_name: str | None) -> str:
    greeting = f"Hi {first_name}" if first_name else "Hi"
    return f"{greeting}, we {FOLLOW_UP_MARKER}. Do you have any questions?"


def _conversations() -> list[dict]:
    """The Page's most recent Messenger conversations, newest first, each with
    its latest 20 messages. Returns [] if Facebook can't be reached."""
    fields = "updated_time,participants,messages.limit(20){message,from,created_time}"
    url = f"{settings.graph_base_url}/{settings.fb_page_id}/conversations"
    params: dict | None = {
        "platform": "messenger",
        "fields": fields,
        "limit": _PAGE_SIZE,
        "access_token": settings.fb_page_access_token,
    }
    out: list[dict] = []
    for _ in range(_PAGES):
        try:
            data = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT).json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Could not read conversations for follow-ups: %s", exc)
            break
        if "error" in data:
            logger.warning("Conversations unavailable for follow-ups: %s", data["error"].get("message"))
            break
        out.extend(data.get("data") or [])
        url = (data.get("paging") or {}).get("next")
        params = None
        if not url:
            break
    return out


def _evaluate(conversation: dict, now: datetime) -> FollowUp | None:
    """A FollowUp if this conversation stopped midway and is due, else None."""
    page_id = str(settings.fb_page_id)
    messages = (conversation.get("messages") or {}).get("data") or []
    if not messages:
        return None

    newest = messages[0]  # Facebook returns newest first
    if str((newest.get("from") or {}).get("id")) != page_id:
        return None  # their message is the latest: it's our turn, not a follow-up

    last_text = (newest.get("message") or "").strip()
    if CLOSING_LINE in last_text or last_text == OPT_OUT_REPLY or FOLLOW_UP_MARKER in last_text.lower():
        return None  # closed, stopped on request, or already followed up

    theirs = [
        (m.get("message") or "") for m in messages if str((m.get("from") or {}).get("id")) != page_id
    ]
    if any(is_opt_out_request(t) for t in theirs):
        return None  # asked us to stop: never follow up
    if not theirs:
        return None  # they never wrote; not a conversation to chase

    last_at = _parse_graph_time(newest.get("created_time"))
    days = (now - last_at).total_seconds() / 86400
    if days < FOLLOW_UP_AFTER_DAYS:
        return None

    person = next(
        (p for p in (conversation.get("participants") or {}).get("data", []) if str(p.get("id")) != page_id),
        None,
    )
    if not person:
        return None
    name = person.get("name") or "(unknown)"
    first = person_first_name(name)
    return FollowUp(
        psid=str(person["id"]),
        name=name,
        first_name=first,
        last_message_at=last_at,
        days_silent=round(days, 1),
        last_page_message=last_text[:200],
        suggested_message=suggested_message(first),
    )


def due_followups(now: datetime | None = None) -> list[FollowUp]:
    """Conversations that went quiet for 5+ days with our message unanswered,
    oldest silence last (the ones about to leave Meta's 7-day window first)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    due = [f for c in _conversations() if (f := _evaluate(c, now))]
    return sorted(due, key=lambda f: (not f.can_still_message, f.days_left))
