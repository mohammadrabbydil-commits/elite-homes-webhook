"""Humanised auto-reply logic for inbound Messenger conversations.

Design goals, in priority order:

1. **Respect the person.** Anyone who asks us to stop is suppressed for good -
   no more automated replies, and they never show up as a lead to follow up.
   Once a person owns a conversation, the bot stays out of it.
2. **Never sound like a bot.** The first reply lands quickly enough that the
   lead doesn't feel ignored (~30-40s), but every reply after that is
   deliberately slower (90-120s, client preference) - a real person doesn't
   answer every message within seconds. The delay is measured from when the
   message arrived, so the AI's own thinking time is part of it, not added
   on top. `mark_seen` is timed to land just before the reply, not the
   instant the message arrives, so there's no long "Seen" silence either.
3. **Get interested leads to the team.** The AI agent (primary) follows the
   client's service policy. When someone asks for a person, an offer, a
   walkthrough, or sends a deal, the lead is recorded for the team right
   away - before the reply goes out, so the bot never says it passed details
   along that weren't actually saved. The scripted question flow is the
   fallback whenever the AI agent is unavailable.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.config import TEMPLATES_FILE, settings
from app.database.db import session_scope
from app.database.models import (
    Conversation,
    ConversationStatus,
    FlowStage,
    Intent,
    Message,
    MessageDirection,
    utcnow,
)
from app.messenger import ai_agent
from app.messenger.ai_agent import EXTRACTABLE_FIELDS
from app.messenger.api import (
    get_conversation_history,
    get_user_profile,
    send_sender_action,
    send_text,
)
from app.messenger.florida_zips import describe_mention, find_zip_mentions

logger = logging.getLogger(__name__)

# Keywords the posts ask people to send, plus natural phrasings.
INTENT_KEYWORDS: dict[Intent, tuple[str, ...]] = {
    Intent.SELL: (
        "sell", "selling", "sold", "my house", "my property", "inherited",
        "vacant", "landlord", "tenant", "as-is", "as is", "offer on my",
        "do you buy", "buy my", "buy houses", "buying houses", "still buying",
    ),
    Intent.BUY: ("buy", "buying", "buyer", "investor", "rental", "deal flow"),
    Intent.PARTNER: ("partner", "wholesale", "wholesaler", "under contract", "jv", "agent", "deal"),
}

# Unmistakably someone sending us a deal - outranks seller words like
# "are you buying", which a wholesaler also says.
_STRONG_PARTNER_KEYWORDS = (
    "wholesale", "wholesaler", "under contract", "assignment", "assigned contract",
    "arv", "deal sheet", "deal sheets", "buy box", "proof of funds",
    "off-market", "off market", "dispo",
)

# A message that is only one of these is a stop request.
_OPT_OUT_EXACT = {
    "stop", "stop all", "unsubscribe", "opt out", "optout", "opt-out",
    "remove me", "stop messaging", "stop texting", "stop contacting me",
}
# Stop requests inside a longer message. "Don't text me, call me instead" is
# a contact preference, not a stop request, so the don't/do-not form only
# counts when nothing follows it.
_OPT_OUT_PATTERN = re.compile(
    r"\b(?:stop|quit)\s+(?:texting|messaging|contacting|sending|emailing|calling)\b"
    r"|\b(?:don'?t|do not|never)\s+(?:text|message|contact|email)\s+me"
    r"(?:\s+(?:again|anymore|any more|ever))?\s*(?:[.!]|$)"
    r"|\bremove me from\b|\btake me off\b|\bunsubscribe\b|\bopt[\s-]?out\b"
    r"|\bleave me alone\b|\bno more messages\b",
    re.IGNORECASE,
)

OPT_OUT_REPLY = "Understood. We won't reach out again. Take care!"

# Sent instead of the planned reply if the lead couldn't be saved - the
# planned reply may say the details were passed along, which would be false.
LEAD_NOT_SAVED_REPLY = (
    "I haven't been able to pass your details through yet, but our team can "
    "still see this conversation."
)

# The seller question-intake flow, in order. Each stage names the question
# still outstanding; the next inbound message answers it and saves into the
# matching Conversation column.
_FLOW_ORDER: tuple[FlowStage, ...] = (
    FlowStage.AWAITING_ADDRESS,
    FlowStage.AWAITING_CONDITION,
    FlowStage.AWAITING_TIMELINE,
    FlowStage.AWAITING_REASON,
    FlowStage.AWAITING_PHONE,
    FlowStage.AWAITING_BEST_TIME,
)
_STAGE_FIELD: dict[FlowStage, str] = {
    FlowStage.AWAITING_ADDRESS: "property_address",
    FlowStage.AWAITING_CONDITION: "condition",
    FlowStage.AWAITING_TIMELINE: "timeline",
    FlowStage.AWAITING_REASON: "reason_for_selling",
    FlowStage.AWAITING_PHONE: "phone_number",
    FlowStage.AWAITING_BEST_TIME: "best_time_to_call",
}

_KNOWN_FIELD_NAMES = tuple(f for f in EXTRACTABLE_FIELDS if f != "lead_summary") + ("county",)

# Words that mark a Facebook profile name as a business rather than a person.
_BUSINESS_WORDS = frozenset(
    "llc inc corp corporation co company group homes home realty realtor realtors properties "
    "property investments investing investors invest capital holdings enterprises solutions "
    "services real estate team ventures partners construction builders management agency "
    "global usa studio shop store official media marketing consulting rentals buyers cash "
    "house houses land development associates trust fund bank llp ltd pllc acquisitions "
    "remodeling roofing renovations brokerage mortgage title insurance".split()
)
_NAME_WORD = re.compile(r"^[A-Za-z][A-Za-z'\-]*$")


def person_first_name(full_name: str | None) -> str | None:
    """The first name to greet someone by, or None when the name is missing
    or looks like a business ("MJ Capital LLC", "Sunshine Homes"). The client
    only wants a person's own name used, never a company's."""
    words = (full_name or "").replace(",", " ").split()
    if not words:
        return None
    if any(w.lower().strip(".&") in _BUSINESS_WORDS or any(ch.isdigit() for ch in w) or w == "&" for w in words):
        return None
    first = words[0]
    if not _NAME_WORD.match(first) or len(first) < 2:
        return None
    if len(words) == 1 and first.isupper() and len(first) > 3:  # an acronym like "EHUSA"
        return None
    return first[0].upper() + first[1:]


def _greeting_name(conversation: Conversation) -> str | None:
    """A name they told us beats their profile name."""
    return person_first_name(conversation.contact_name) or person_first_name(conversation.name)


def _known_fields(conversation: Conversation) -> dict[str, str]:
    """Already-captured columns on this conversation, so the AI agent is told
    these as facts instead of having to re-infer them from raw history."""
    return {
        field: value
        for field in _KNOWN_FIELD_NAMES
        if (value := getattr(conversation, field, None))
    }


def _next_stage(stage: FlowStage) -> FlowStage:
    index = _FLOW_ORDER.index(stage)
    if index + 1 < len(_FLOW_ORDER):
        return _FLOW_ORDER[index + 1]
    return FlowStage.COMPLETE


@dataclass
class ReplyPlan:
    """What the responder decided to do about one inbound message."""

    should_reply: bool
    text: str | None = None
    delay_seconds: float = 0.0
    intent: Intent = Intent.UNKNOWN
    handoff: bool = True
    reason: str | None = None
    quick_replies: list[dict] | None = None
    new_stage: FlowStage | None = None
    # {column_name: value} to save onto the Conversation row, if any.
    captured: dict[str, str] | None = None
    # Record this conversation as a lead for the team (the bot may keep replying).
    submit_lead: bool = False
    # They asked us to stop - suppress the contact for good.
    opt_out: bool = False


def _load_auto_reply_config() -> dict:
    if not TEMPLATES_FILE.exists():
        raise FileNotFoundError(f"Templates file not found: {TEMPLATES_FILE}")
    payload = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
    return payload.get("auto_reply", {})


def _load_seller_flow_config() -> dict:
    if not TEMPLATES_FILE.exists():
        raise FileNotFoundError(f"Templates file not found: {TEMPLATES_FILE}")
    payload = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
    return payload.get("seller_flow", {})


def _has_keyword(lowered: str, keyword: str) -> bool:
    return bool(re.search(rf"\b{re.escape(keyword)}\b", lowered))


def detect_intent(text: str) -> Intent:
    """Classify an inbound message by keyword.

    Only a starting guess: when the AI agent replies it classifies the
    message itself, and that takes precedence. Clear wholesaler language
    wins first; after that the Page is acquisition-focused, so any seller
    signal wins over buyer or partner words.
    """
    if not text:
        return Intent.UNKNOWN

    lowered = text.lower()
    if any(_has_keyword(lowered, kw) for kw in _STRONG_PARTNER_KEYWORDS):
        return Intent.PARTNER

    scores: dict[Intent, int] = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if _has_keyword(lowered, kw))
        if hits:
            scores[intent] = hits

    if not scores:
        return Intent.UNKNOWN
    # A message like "selling my rental, I'm an investor" scores higher on
    # buyer keywords but is plainly a seller lead, and those are the ones
    # that matter.
    if Intent.SELL in scores:
        return Intent.SELL
    return max(scores, key=lambda k: scores[k])


def is_opt_out_request(text: str) -> bool:
    """Whether a message asks us to stop contacting them.

    Checked in code before the AI agent ever sees the message, so honouring
    a stop request never depends on the model getting it right.
    """
    if not text:
        return False
    normalized = re.sub(r"[^\w\s'-]", "", text).strip().lower()
    return normalized in _OPT_OUT_EXACT or bool(_OPT_OUT_PATTERN.search(text))


def is_business_hours(now: datetime | None = None) -> bool:
    """Whether local time in the Page's timezone is inside business hours."""
    zone = ZoneInfo(settings.timezone)
    local = (now or datetime.now(zone)).astimezone(zone)
    return settings.business_hours_start <= local.hour < settings.business_hours_end


def compute_delay(is_first_reply: bool = False) -> float:
    """A randomised pause before replying.

    Randomised so consecutive leads never see the same interval - a fixed delay
    is as obvious a tell as an instant reply. The very first reply in a
    conversation uses a shorter band (client preference: ~35s, so the lead
    doesn't feel ignored) - every reply after that uses the slower, more
    deliberate band so the pace feels human rather than instant.
    """
    if is_first_reply:
        low = settings.autoreply_first_min_delay_seconds
        high = max(low, settings.autoreply_first_max_delay_seconds)
    else:
        low = settings.autoreply_min_delay_seconds
        high = max(low, settings.autoreply_max_delay_seconds)
    return random.uniform(low, high)


def choose_reply(intent: Intent, first_name: str | None = None) -> str:
    """Pick and fill a reply template for this intent and time of day.

    Used for buy/partner/unknown intents, which still get exactly one
    acknowledgement. SELL intent uses the question flow instead (see
    `_start_flow`), not this function.
    """
    config = _load_auto_reply_config()
    in_hours = is_business_hours()

    block = config.get("after_hours" if not in_hours else "business_hours", {})
    templates = block.get(intent.value) or block.get("default") or []
    if not templates:
        templates = ["Thanks for reaching out. Someone from our team will get back to you shortly."]

    template = random.choice(templates)
    return template.format(
        first_name=first_name or "there",
        business_name=config.get("business_name", "Elite Homes USA Group"),
        city=config.get("city", "Jacksonville"),
    )


def _quick_replies_payload(flow_cfg: dict) -> list[dict]:
    return [
        {"content_type": "text", "title": faq["title"], "payload": faq["payload"]}
        for faq in flow_cfg.get("faq_quick_replies", [])
    ]


def _start_flow(first_name: str | None) -> ReplyPlan:
    """First message to a SELL-intent lead: greeting, FAQ buttons, and the
    first question (property address), all in one send."""
    flow_cfg = _load_seller_flow_config()
    in_hours = is_business_hours()
    block = flow_cfg.get("business_hours" if in_hours else "after_hours", {})
    greetings = block.get("greetings") or ["Thanks for reaching out! What's the property address?"]
    text = random.choice(greetings).format(first_name=first_name or "there")

    return ReplyPlan(
        should_reply=True,
        text=text,
        delay_seconds=compute_delay(is_first_reply=True),
        intent=Intent.SELL,
        handoff=False,
        quick_replies=_quick_replies_payload(flow_cfg),
        new_stage=FlowStage.AWAITING_ADDRESS,
    )


# For the scripted fallback (AI unavailable): requests the client wants
# answered with the calendar reply, and price questions that get the
# walkthrough-then-calendar reply.
_SCHEDULING_REQUEST = re.compile(
    r"\b(?:re)?schedul\w*|\bmeet(?:ing)?\b|\bappointment\b|\bwalk[\s-]?through\b|\bcalendar\b"
    r"|\bcome (?:look|see|by|out)\b|\bvisit\b|\bset up a call\b|\bbook (?:a|an)\b",
    re.IGNORECASE,
)
_PRICE_QUESTION = re.compile(
    r"\b(?:pay|offer|price|worth|value|how much)\b", re.IGNORECASE
)


def _fallback_scheduling_plan(
    text: str, conversation: Conversation, intent: Intent, *, first_reply: bool, stage: FlowStage
) -> ReplyPlan | None:
    """When the AI agent is unavailable, a scheduling request or a price
    question still gets the client's calendar reply instead of being taken
    as the answer to the scripted flow's next question."""
    if _PRICE_QUESTION.search(text):
        reply = ai_agent.FALLBACK_REPLY
    elif _SCHEDULING_REQUEST.search(text):
        reply = ai_agent.SCHEDULE_REPLY
    else:
        return None
    name = _greeting_name(conversation)
    if first_reply and name:
        reply = (
            reply.replace("Thanks for reaching out.", f"Hi {name}, thanks for reaching out.", 1)
            if reply.startswith("Thanks for reaching out.")
            else f"Hi {name}! {reply}"
        )
    return ReplyPlan(
        should_reply=True,
        text=reply,
        delay_seconds=compute_delay(is_first_reply=first_reply),
        intent=intent,
        handoff=False,
        submit_lead=True,
        new_stage=stage,
    )


def _intent_from_agent(value: str | None, fallback: Intent) -> Intent:
    if value in {"sell", "partner", "buy"}:
        return Intent(value)
    return fallback


def _ai_plan(
    text: str,
    conversation: Conversation,
    history: list[Message] | None,
    fallback_intent: Intent,
    *,
    is_first_reply: bool,
    stage: FlowStage,
) -> ReplyPlan | None:
    """One AI agent turn as a ReplyPlan, or None if the agent is unavailable
    and the caller should fall back to the scripted path.

    ZIP codes in the message are looked up here, in code, and handed to the
    agent as facts - it never names a ZIP's city or county on its own.
    """
    mentions = find_zip_mentions(text)
    result = ai_agent.generate_reply(
        history or [],
        text,
        known_fields=_known_fields(conversation),
        location_facts=[describe_mention(m) for m in mentions] or None,
        first_name=_greeting_name(conversation),
    )
    if not result.success:
        logger.warning("AI agent unavailable (%s), falling back to the scripted flow", result.error)
        return None

    captured = dict(result.extracted)
    if mentions:
        # The ZIP they typed is authoritative over anything the model extracted.
        first = mentions[0]
        captured["zip_code"] = first.zip_code
        if first.info:
            captured["county"] = first.info.county
            if not captured.get("city") and not conversation.city:
                captured["city"] = first.info.city

    return ReplyPlan(
        should_reply=True,
        text=result.text,
        delay_seconds=compute_delay(is_first_reply=is_first_reply),
        intent=_intent_from_agent(result.intent, fallback_intent),
        handoff=result.handoff,
        submit_lead=result.submit_lead or result.handoff,
        opt_out=result.opt_out,
        new_stage=FlowStage.COMPLETE if result.handoff else stage,
        captured=captured or None,
    )


def _advance_flow(
    text: str,
    conversation: Conversation,
    quick_reply_payload: str | None,
    history: list[Message] | None = None,
) -> ReplyPlan:
    """Continue an in-progress conversation by one step."""
    flow_cfg = _load_seller_flow_config()
    stage = conversation.stage

    # A tapped FAQ button answers a side question - it doesn't count as the
    # answer to the pending flow question, so re-ask that question after.
    faqs_by_payload = {faq["payload"]: faq for faq in flow_cfg.get("faq_quick_replies", [])}
    if quick_reply_payload and quick_reply_payload in faqs_by_payload:
        answer = faqs_by_payload[quick_reply_payload]["answer"]
        current_prompts = flow_cfg.get("questions", {}).get(stage.value, {}).get("prompts") or [""]
        combined = f"{answer}\n\n{random.choice(current_prompts)}".strip()
        return ReplyPlan(
            should_reply=True,
            text=combined,
            delay_seconds=compute_delay(),
            intent=conversation.intent,
            handoff=False,
            new_stage=stage,
        )

    # AI agent path. Any failure here (disabled, missing key, network error,
    # malformed response) falls straight through to the scripted flow below -
    # the AI layer can only add a better experience, never remove the
    # working one.
    if settings.ai_agent_enabled and settings.openai_api_key:
        plan = _ai_plan(
            text, conversation, history, conversation.intent, is_first_reply=False, stage=stage
        )
        if plan is not None:
            return plan

    plan = _fallback_scheduling_plan(text, conversation, conversation.intent, first_reply=False, stage=stage)
    if plan is not None:
        return plan

    field = _STAGE_FIELD[stage]
    captured = {field: text.strip()[:500]}
    following = _next_stage(stage)

    if following is FlowStage.COMPLETE:
        return ReplyPlan(
            should_reply=True,
            text=flow_cfg.get("closing_message", "Thank you! Our team will review this and get back to you."),
            delay_seconds=compute_delay(),
            intent=conversation.intent,
            handoff=True,
            submit_lead=True,
            new_stage=FlowStage.COMPLETE,
            captured=captured,
        )

    next_prompts = flow_cfg.get("questions", {}).get(following.value, {}).get("prompts") or [""]
    return ReplyPlan(
        should_reply=True,
        text=random.choice(next_prompts),
        delay_seconds=compute_delay(),
        intent=conversation.intent,
        handoff=False,
        new_stage=following,
        captured=captured,
    )


def plan_reply(
    text: str,
    conversation: Conversation,
    first_name: str | None = None,
    quick_reply_payload: str | None = None,
    history: list[Message] | None = None,
) -> ReplyPlan:
    """Decide whether and how to auto-reply to one inbound message.

    The AI agent drives the conversation, falling back to the scripted
    question flow when it's unavailable. Once a conversation is handed off, a
    human owns the thread and later messages never get another automated
    reply. Someone who asked us to stop never gets another reply at all.
    """
    if conversation.opted_out:
        return ReplyPlan(False, intent=conversation.intent or Intent.UNKNOWN, reason="contact opted out")

    if is_opt_out_request(text):
        # One short confirmation, but only while the bot owns the conversation.
        can_confirm = settings.autoreply_enabled and conversation.status in {
            ConversationStatus.NEW,
            ConversationStatus.AUTO_REPLIED,
        }
        return ReplyPlan(
            should_reply=can_confirm,
            text=OPT_OUT_REPLY if can_confirm else None,
            delay_seconds=compute_delay(is_first_reply=True) if can_confirm else 0.0,
            intent=conversation.intent or Intent.UNKNOWN,
            handoff=False,
            opt_out=True,
            reason="asked us to stop",
        )

    if not settings.autoreply_enabled:
        return ReplyPlan(False, intent=detect_intent(text), reason="auto-reply disabled in config")

    if conversation.status in {ConversationStatus.HUMAN_HANDLED, ConversationStatus.CLOSED}:
        return ReplyPlan(False, intent=conversation.intent, reason=f"status is {conversation.status.value}")

    # Mid-conversation: this message answers (or asks about) the last question.
    if conversation.status is ConversationStatus.AUTO_REPLIED and conversation.stage is not FlowStage.COMPLETE:
        return _advance_flow(text, conversation, quick_reply_payload, history)

    if conversation.status is ConversationStatus.AWAITING_HUMAN:
        return ReplyPlan(
            False,
            intent=conversation.intent,
            reason="already auto-replied; conversation belongs to a human now",
        )

    # Fresh conversation (or one that has never triggered an auto-reply yet).
    intent = detect_intent(text)

    if settings.ai_agent_enabled and settings.openai_api_key:
        plan = _ai_plan(
            text, conversation, history, intent,
            is_first_reply=True, stage=FlowStage.AWAITING_ADDRESS,
        )
        if plan is not None:
            return plan

    plan = _fallback_scheduling_plan(
        text, conversation, intent, first_reply=True, stage=FlowStage.AWAITING_ADDRESS
    )
    if plan is not None:
        return plan

    if intent is Intent.SELL:
        return _start_flow(first_name)

    # Column defaults apply at INSERT, so a conversation not yet flushed has
    # auto_replies_sent = None rather than 0.
    already_sent = conversation.auto_replies_sent or 0
    if already_sent >= settings.autoreply_max_per_conversation:
        return ReplyPlan(
            False,
            intent=intent,
            reason="already auto-replied; conversation belongs to a human now",
        )

    return ReplyPlan(
        should_reply=True,
        text=choose_reply(intent, first_name),
        delay_seconds=compute_delay(is_first_reply=True),
        intent=intent,
        handoff=True,
    )


def _restore_history_from_facebook(session, conversation: Conversation, current_mid: str | None) -> None:
    """Fill in a conversation's past messages from Facebook.

    Called when the database has nothing this person has written - a new
    person, or a returning one whose history was lost when the database was
    reset (Render's free plan wipes it on every restart). Facebook keeps
    every conversation, so the agent still remembers what they said days ago,
    doesn't introduce itself again, and still honours a past stop request.
    """
    past = [m for m in get_conversation_history(conversation.psid) if m.mid != current_mid]
    if not past:
        return

    already_stored = set(
        session.scalars(select(Message.mid).where(Message.mid.in_([m.mid for m in past])))
    )
    for m in past:
        if m.mid in already_stored:
            continue
        session.add(
            Message(
                conversation_id=conversation.id,
                mid=m.mid,
                direction=MessageDirection.OUTBOUND if m.from_page else MessageDirection.INBOUND,
                text=m.text or "[photo or attachment]",
                is_auto=False,
                timestamp=m.created_at,
            )
        )
    session.flush()

    if any(not m.from_page and is_opt_out_request(m.text) for m in past):
        conversation.opted_out = True
        conversation.status = ConversationStatus.CLOSED
    logger.info("Restored %d past message(s) for %s from Facebook", len(past), conversation.psid)


def _save_lead_details(conversation_id: int, plan: ReplyPlan, psid: str) -> None:
    """Save what this message told us, and record the lead for the team,
    before the reply goes out.

    Saving first means a deploy or restart during the reply delay can't lose
    the answer, and the bot never says it passed details along that weren't
    actually saved: if this fails, the reply is swapped for an honest one and
    the conversation goes to a person.
    """
    lead = plan.submit_lead or plan.handoff
    if not (plan.captured or lead):
        return
    try:
        with session_scope() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                raise LookupError(f"conversation {conversation_id} not found")
            for field, value in (plan.captured or {}).items():
                setattr(conversation, field, value)
            if lead and conversation.lead_submitted_at is None:
                conversation.lead_submitted_at = utcnow()
    except Exception:
        logger.exception("Could not save lead details for %s", psid)
        if lead:
            plan.text = LEAD_NOT_SAVED_REPLY
            plan.quick_replies = None
            plan.handoff = True
            plan.new_stage = FlowStage.COMPLETE


def handle_inbound_message(
    psid: str,
    text: str,
    mid: str | None = None,
    sleep: bool = True,
    quick_reply_payload: str | None = None,
) -> ReplyPlan:
    """Process one inbound Messenger message end to end.

    Idempotent on `mid`: Facebook redelivers webhook events after any non-200
    response, and replying twice to the same message is exactly the kind of
    thing that reads as automated.
    """
    received_at = time.monotonic()

    with session_scope() as session:
        if mid and session.scalar(select(Message).where(Message.mid == mid).limit(1)):
            logger.info("Message %s already handled, ignoring redelivery", mid)
            return ReplyPlan(False, reason="duplicate webhook delivery")

        conversation = session.scalar(
            select(Conversation).where(Conversation.psid == psid).limit(1)
        )
        is_new = conversation is None

        if is_new:
            profile = get_user_profile(psid)
            full_name = " ".join(
                part for part in (profile.get("first_name"), profile.get("last_name")) if part
            )
            conversation = Conversation(
                psid=psid,
                name=full_name or None,
                status=ConversationStatus.NEW,
            )
            session.add(conversation)
            session.flush()

        has_their_messages = session.scalar(
            select(Message.id)
            .where(
                Message.conversation_id == conversation.id,
                Message.direction == MessageDirection.INBOUND,
            )
            .limit(1)
        )
        if not has_their_messages:
            _restore_history_from_facebook(session, conversation, mid)

        # Prior turns only - fetched before this message is added, so the AI
        # agent (if it runs) sees the same history a human reading the
        # thread would, with the current message passed separately.
        history = list(
            session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.timestamp)
            )
        )

        conversation.last_message_at = utcnow()
        session.add(
            Message(
                conversation_id=conversation.id,
                mid=mid,
                direction=MessageDirection.INBOUND,
                text=text,
                timestamp=utcnow(),
            )
        )

        first_name = _greeting_name(conversation)
        plan = plan_reply(text, conversation, first_name, quick_reply_payload, history)

        if plan.intent is not Intent.UNKNOWN:
            conversation.intent = plan.intent

        if plan.opt_out and not conversation.opted_out:
            conversation.opted_out = True
            conversation.status = ConversationStatus.CLOSED
            logger.info("%s asked us to stop; contact suppressed", psid)

        if not plan.should_reply:
            logger.info("Not auto-replying to %s: %s", psid, plan.reason)
            # A follow-up message from someone a human already owns still needs
            # a human, so make sure it is flagged.
            if conversation.status is ConversationStatus.AUTO_REPLIED:
                conversation.status = ConversationStatus.AWAITING_HUMAN
            return plan

        conversation_id = conversation.id

    _save_lead_details(conversation_id, plan, psid)

    # Outside any transaction: the pause must not hold a database session
    # open. Time already spent (the AI call) counts toward the delay.
    remaining = max(0.0, plan.delay_seconds - (time.monotonic() - received_at))
    logger.info("Replying to %s in %.0fs (intent=%s)", psid, remaining, plan.intent.value)
    if sleep:
        # Wait first with no indicator at all - mark_seen only fires close to
        # when the reply actually goes out, not the instant the message
        # arrives. A real person doesn't necessarily open a message the
        # second it lands, so "Seen" immediately followed by a long silence
        # is its own tell.
        typing_lead = min(remaining, 6.0)
        time.sleep(max(0.0, remaining - typing_lead))
        send_sender_action(psid, "mark_seen")
        send_sender_action(psid, "typing_on")
        time.sleep(typing_lead)
    else:
        send_sender_action(psid, "mark_seen")
        send_sender_action(psid, "typing_on")

    result = send_text(psid, plan.text, quick_replies=plan.quick_replies)

    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return plan

        if result.success:
            # Stage only advances on a confirmed send - if it failed, a human
            # picks up from exactly where the automation left off.
            if plan.new_stage is not None:
                conversation.stage = plan.new_stage
            conversation.auto_replies_sent += 1
            if not plan.opt_out:
                conversation.status = (
                    ConversationStatus.AWAITING_HUMAN if plan.handoff else ConversationStatus.AUTO_REPLIED
                )
            if plan.handoff:
                conversation.handoff_at = utcnow()
            session.add(
                Message(
                    conversation_id=conversation.id,
                    mid=result.message_id,
                    direction=MessageDirection.OUTBOUND,
                    text=plan.text,
                    is_auto=True,
                    timestamp=utcnow(),
                )
            )
            if plan.handoff:
                logger.info("Conversation %s handed off to a human", psid)
        else:
            if not plan.opt_out:
                conversation.status = ConversationStatus.AWAITING_HUMAN
            logger.error("Auto-reply failed for %s, flagged for human: %s", psid, result.error)

    return plan


def pending_handoffs() -> list[dict]:
    """Leads for the team, newest first: conversations the bot handed off,
    plus any where a lead was submitted while the bot kept helping. Anyone
    who asked us to stop is never listed - they must not be followed up."""
    with session_scope() as session:
        rows = session.scalars(
            select(Conversation)
            .where(
                or_(
                    Conversation.status == ConversationStatus.AWAITING_HUMAN,
                    Conversation.lead_submitted_at.is_not(None),
                ),
                ~Conversation.opted_out,
            )
            .order_by(Conversation.last_message_at.desc())
        ).all()
        return [
            {
                "psid": c.psid,
                "name": c.name or "(unknown)",
                "contact_name": c.contact_name,
                "intent": c.intent.value,
                "status": c.status.value,
                "address": c.property_address,
                "city": c.city,
                "zip_code": c.zip_code,
                "county": c.county,
                "condition": c.condition,
                "timeline": c.timeline,
                "reason_for_selling": c.reason_for_selling,
                "main_concern": c.main_concern,
                "asking_price": c.asking_price,
                "contact_preference": c.contact_preference,
                "phone_number": c.phone_number,
                "email": c.email,
                "best_time_to_call": c.best_time_to_call,
                "availability": c.availability,
                "company_name": c.company_name,
                "square_footage": c.square_footage,
                "sewer_type": c.sewer_type,
                "lead_summary": c.lead_summary,
                "lead_submitted_at": c.lead_submitted_at,
                "last_message_at": c.last_message_at,
                "window_open": c.window_open,
            }
            for c in rows
        ]
