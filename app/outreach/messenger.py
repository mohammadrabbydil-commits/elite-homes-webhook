"""Personalised outreach message composition and delivery.

  COMPLIANCE NOTICE
  -----------------
  Automated sending of unsolicited direct messages, friend requests, or Page
  invites through a logged-in personal profile violates Meta's Terms of Service
  and, for unsolicited commercial messages, may also engage US telemarketing and
  anti-spam rules. `send_message()` is therefore left unimplemented.

  Message *composition* is fully implemented and is provider-agnostic: the same
  render_message() output can be pasted by a human, sent through the sanctioned
  Messenger Platform API to people who messaged the Page first, or sent by any
  other compliant channel.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass

from app.config import TEMPLATES_FILE
from app.database.models import OutreachProspect, Segment
from app.outreach.session import NotImplementedByDesign

logger = logging.getLogger(__name__)


class MessageTemplateError(RuntimeError):
    """Raised when a template cannot be found or rendered."""


@dataclass(frozen=True)
class RenderedMessage:
    """A message ready to be delivered by some channel."""

    prospect_name: str
    segment: Segment
    body: str


def _load_templates() -> dict:
    if not TEMPLATES_FILE.exists():
        raise MessageTemplateError(f"Templates file not found: {TEMPLATES_FILE}")
    return json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))


def first_name(full_name: str) -> str:
    """Best-effort first name, falling back to the whole string."""
    cleaned = (full_name or "").strip()
    return cleaned.split()[0] if cleaned else "there"


def render_message(
    prospect: OutreachProspect,
    variant: int | None = None,
    template_override: str | None = None,
) -> RenderedMessage:
    """Fill a segment template with this prospect's details.

    Args:
        prospect: The prospect to address.
        variant: Index into the segment's template list. Random when omitted,
            which keeps outreach from looking like one copy-pasted blast.
        template_override: Use this template string instead of the file.
    """
    payload = _load_templates()
    defaults = payload.get("defaults", {})

    if template_override is not None:
        template = template_override
    else:
        block = payload.get("segments", {}).get(prospect.segment.value)
        if not block:
            raise MessageTemplateError(f"No templates for segment {prospect.segment.value!r}")

        templates = block.get("templates", [])
        if not templates:
            raise MessageTemplateError(f"Segment {prospect.segment.value!r} has no templates")

        index = random.randrange(len(templates)) if variant is None else variant % len(templates)
        template = templates[index]

    context = {
        "name": prospect.name,
        "first_name": first_name(prospect.name),
        "source_group": prospect.source_group or "a local group",
        "city": defaults.get("city", "Jacksonville"),
        "business_name": defaults.get("business_name", "Elite Homes USA Group"),
    }

    try:
        body = template.format(**context)
    except KeyError as exc:
        raise MessageTemplateError(f"Template references unknown placeholder {exc}") from exc

    return RenderedMessage(prospect_name=prospect.name, segment=prospect.segment, body=body)


def render_follow_up(prospect: OutreachProspect) -> RenderedMessage:
    """Render the single follow-up message for a non-responder."""
    payload = _load_templates()
    template = payload.get("follow_ups", {}).get("template")
    if not template:
        raise MessageTemplateError("No follow_ups.template configured")
    return render_message(prospect, template_override=template)


def preview_all(prospect: OutreachProspect) -> list[str]:
    """Render every template variant for a prospect. Useful for review."""
    payload = _load_templates()
    block = payload.get("segments", {}).get(prospect.segment.value, {})
    count = len(block.get("templates", []))
    return [render_message(prospect, variant=i).body for i in range(count)]


# --- Unimplemented by design ----------------------------------------------


def send_message(prospect: OutreachProspect, body: str, session=None) -> None:
    """NOT IMPLEMENTED - automated unsolicited DMs violate Meta's Terms."""
    raise NotImplementedByDesign(
        "Automated message sending is not implemented: unsolicited bulk DMs from "
        "an automated personal profile violate Meta's Terms of Service and risk "
        "a permanent ban. render_message() gives you the text; deliver it through "
        "a compliant channel. See the README section 'Outreach: compliant "
        "alternatives'."
    )


def send_friend_request(prospect: OutreachProspect, session=None) -> None:
    """NOT IMPLEMENTED - automated friend requests violate Meta's Terms."""
    raise NotImplementedByDesign(
        "Automated friend requests are not implemented. See the README."
    )


def send_page_invite(prospect: OutreachProspect, session=None) -> None:
    """NOT IMPLEMENTED - automated Page invites violate Meta's Terms."""
    raise NotImplementedByDesign(
        "Automated Page invites are not implemented. See the README."
    )
