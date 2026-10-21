"""Follow-ups for conversations that stopped midway.

The client wants a message like "Hi Dejon, we wanted to follow up. Do you have
any questions?" sent once when someone goes quiet. Meta's rules decide how:

- Within 24 hours of the person's last message a Page may message
  automatically. `run_nudges` does that, once, after NUDGE_AFTER_HOURS of
  silence (20 by default), in business hours.
- After 24 hours a bot may not message at all (Facebook rejects it with "sent
  outside of allowed window", and the 7-day HUMAN_AGENT tag is refused without
  approval and meant for a human replying). `due_followups` lists who has gone
  quiet for FOLLOW_UP_AFTER_DAYS (3 by default) so a person on the team can send
  the same message from the Page inbox, for up to 7 days.

Both read Facebook's own conversation history rather than our database,
because Render's free plan wipes the database on every restart, and both send
a follow-up only once per person.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from app.config import settings
from app.messenger.ai_agent import CLOSING_LINE
from app.messenger.api import DEFAULT_TIMEOUT, _parse_graph_time, send_text
from app.messenger.responder import OPT_OUT_REPLY, is_opt_out_request, person_first_name

logger = logging.getLogger(__name__)

FOLLOW_UP_AFTER_DAYS = settings.follow_up_after_days
# Meta lets a human reply for 7 days after the person's last message.
HUMAN_WINDOW_DAYS = 7
# Meta lets a Page message automatically for 24 hours; stop a little short.
NUDGE_MAX_HOURS = 23.5
NUDGE_MAX_PER_RUN = 20
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


@dataclass(frozen=True)
class Nudge:
    psid: str
    first_name: str | None
    hours_silent: float
    hours_since_customer: float
    text: str


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


@dataclass(frozen=True)
class _Stalled:
    psid: str
    name: str
    first_name: str | None
    last_page_at: datetime
    last_customer_at: datetime
    last_page_message: str


def _stalled(conversation: dict) -> _Stalled | None:
    """The conversation if it stopped midway and a follow-up hasn't been sent:
    our message was the last, they had written, it isn't closed or opted out,
    and nobody has followed up yet."""
    page_id = str(settings.fb_page_id)
    messages = (conversation.get("messages") or {}).get("data") or []
    if not messages:
        return None

    newest = messages[0]  # Facebook returns newest first
    if str((newest.get("from") or {}).get("id")) != page_id:
        return None  # their message is the latest: it's our turn, not a follow-up

    last_text = (newest.get("message") or "").strip()
    if CLOSING_LINE in last_text or last_text == OPT_OUT_REPLY:
        return None  # closed, or stopped on request

    # Everything since the customer last wrote. Our automatic reply is one
    # message; a second one means somebody already followed up (worded any
    # way they liked), and a follow-up happens only once.
    page_messages_since = 0
    customer_last = None
    for m in messages:
        if str((m.get("from") or {}).get("id")) != page_id:
            customer_last = m
            break
        page_messages_since += 1
        if FOLLOW_UP_MARKER in (m.get("message") or "").lower():
            return None
    if page_messages_since > 1 or customer_last is None:
        return None  # already followed up, or they never wrote

    if any(
        is_opt_out_request(m.get("message") or "")
        for m in messages
        if str((m.get("from") or {}).get("id")) != page_id
    ):
        return None  # asked us to stop: never follow up

    person = next(
        (p for p in (conversation.get("participants") or {}).get("data", []) if str(p.get("id")) != page_id),
        None,
    )
    if not person:
        return None
    name = person.get("name") or "(unknown)"
    return _Stalled(
        psid=str(person["id"]),
        name=name,
        first_name=person_first_name(name),
        last_page_at=_parse_graph_time(newest.get("created_time")),
        last_customer_at=_parse_graph_time(customer_last.get("created_time")),
        last_page_message=last_text[:200],
    )


# --- The team list: quiet for days, a person sends it -------------------------


def _evaluate(conversation: dict, now: datetime) -> FollowUp | None:
    """A FollowUp if this conversation has been quiet long enough for the team."""
    s = _stalled(conversation)
    if s is None:
        return None
    days = (now - s.last_page_at).total_seconds() / 86400
    if days < FOLLOW_UP_AFTER_DAYS:
        return None
    return FollowUp(
        psid=s.psid,
        name=s.name,
        first_name=s.first_name,
        last_message_at=s.last_page_at,
        days_silent=round(days, 1),
        last_page_message=s.last_page_message,
        suggested_message=suggested_message(s.first_name),
    )


def due_followups(now: datetime | None = None) -> list[FollowUp]:
    """Conversations that went quiet for FOLLOW_UP_AFTER_DAYS+ days with our
    message unanswered, the ones about to leave Meta's 7-day window first."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    due = [f for c in _conversations() if (f := _evaluate(c, now))]
    return sorted(due, key=lambda f: (not f.can_still_message, f.days_left))


# --- The automatic nudge: inside Meta's 24 hour window ------------------------


def _in_business_hours(now: datetime) -> bool:
    local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone))
    return settings.business_hours_start <= local.hour < settings.business_hours_end


def _evaluate_nudge(conversation: dict, now: datetime) -> Nudge | None:
    """A Nudge if this person should get the automatic follow-up right now:
    quiet for NUDGE_AFTER_HOURS, still inside Meta's 24 hour window."""
    s = _stalled(conversation)
    if s is None:
        return None
    hours_silent = (now - s.last_page_at).total_seconds() / 3600
    hours_since_customer = (now - s.last_customer_at).total_seconds() / 3600
    if hours_silent < settings.nudge_after_hours or hours_since_customer >= NUDGE_MAX_HOURS:
        return None
    return Nudge(
        psid=s.psid,
        first_name=s.first_name,
        hours_silent=round(hours_silent, 1),
        hours_since_customer=round(hours_since_customer, 1),
        text=suggested_message(s.first_name),
    )


_send_lock = threading.Lock()


def run_nudges(now: datetime | None = None, dry_run: bool = False) -> dict:
    """Send the follow-up to everyone due, once. Safe to call as often as you
    like: a person who has been nudged shows a second Page message in their
    history and is skipped, and only one run executes at a time."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    summary: dict = {"enabled": settings.nudge_enabled, "sent": 0, "failed": 0, "due": 0, "dry_run": dry_run}
    if not (settings.nudge_enabled and settings.autoreply_enabled):
        summary["skipped"] = "nudges are switched off"
        return summary
    if not _in_business_hours(now):
        summary["skipped"] = "outside business hours"
        return summary

    with _send_lock:
        due = [n for c in _conversations() if (n := _evaluate_nudge(c, now))][:NUDGE_MAX_PER_RUN]
        summary["due"] = len(due)
        if dry_run:
            summary["would_send"] = [n.text for n in due]
            return summary
        for nudge in due:
            result = send_text(nudge.psid, nudge.text, messaging_type="UPDATE")
            if result.success:
                summary["sent"] += 1
                logger.info("Sent the follow-up nudge to %s", nudge.psid)
            else:
                summary["failed"] += 1
                logger.error("Follow-up nudge to %s failed: %s", nudge.psid, result.error)
    return summary
