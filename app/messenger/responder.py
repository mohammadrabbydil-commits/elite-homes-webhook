"""Humanised auto-reply logic for inbound Messenger conversations.

Design goals, in priority order:

1. **Speed wins deals.** Sellers message several buyers at once and the first
   real response usually holds the conversation. So the delay is short -
   seconds, not minutes.
2. **Never sound like a bot.** Randomised delay, a typing indicator, rotating
   phrasings, and different copy outside business hours.
3. **Capture a seller lead completely, then hand off.** A SELL-intent
   conversation runs a short question flow (address, condition, timeline,
   reason, phone, best time to call) and never makes an offer or states a
   price - it only gathers information for the team to call back on. Every
   other intent still gets exactly one acknowledgement, then a human owns the
   thread, so it can never get lost in a long automated back-and-forth.
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

from sqlalchemy import select

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
from app.messenger.api import get_user_profile, send_sender_action, send_text

logger = logging.getLogger(__name__)

# Keywords the posts ask people to send, plus natural phrasings.
INTENT_KEYWORDS: dict[Intent, tuple[str, ...]] = {
    Intent.SELL: (
        "sell", "selling", "sold", "my house", "my property", "inherited",
        "vacant", "landlord", "tenant", "as-is", "as is", "offer on my",
    ),
    Intent.BUY: ("buy", "buying", "buyer", "investor", "rental", "deal flow"),
    Intent.PARTNER: ("partner", "wholesale", "wholesaler", "under contract", "jv", "agent"),
}

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


def _known_fields(conversation: Conversation) -> dict[str, str]:
    """Already-captured columns on this conversation, so the AI agent is told
    these as facts instead of having to re-infer them from raw history."""
    return {
        field: value
        for field in EXTRACTABLE_FIELDS
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


def detect_intent(text: str) -> Intent:
    """Classify an inbound message by keyword.

    Checks SELL last-but-weighted: the Page is acquisition-focused, so a
    message mentioning both selling and investing is treated as a seller lead.
    """
    if not text:
        return Intent.UNKNOWN

    lowered = text.lower()
    scores: dict[Intent, int] = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", lowered))
        if hits:
            scores[intent] = hits

    if not scores:
        return Intent.UNKNOWN
    # The Page is acquisition-only, so any seller signal wins outright. A
    # message like "selling my rental, I'm an investor" scores higher on buyer
    # keywords but is plainly a seller lead, and those are the ones that matter.
    if Intent.SELL in scores:
        return Intent.SELL
    return max(scores, key=lambda k: scores[k])


def is_business_hours(now: datetime | None = None) -> bool:
    """Whether local time in the Page's timezone is inside business hours."""
    zone = ZoneInfo(settings.timezone)
    local = (now or datetime.now(zone)).astimezone(zone)
    return settings.business_hours_start <= local.hour < settings.business_hours_end


def compute_delay() -> float:
    """A randomised pause before replying.

    Randomised so consecutive leads never see the same interval - a fixed delay
    is as obvious a tell as an instant reply.
    """
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
    text = block.get("greeting", "Thanks for reaching out! What's the property address?").format(
        first_name=first_name or "there"
    )

    return ReplyPlan(
        should_reply=True,
        text=text,
        delay_seconds=compute_delay(),
        intent=Intent.SELL,
        handoff=False,
        quick_replies=_quick_replies_payload(flow_cfg),
        new_stage=FlowStage.AWAITING_ADDRESS,
    )


def _advance_flow(
    text: str,
    conversation: Conversation,
    quick_reply_payload: str | None,
    history: list[Message] | None = None,
) -> ReplyPlan:
    """Continue an in-progress seller question flow by one step."""
    flow_cfg = _load_seller_flow_config()
    stage = conversation.stage

    # A tapped FAQ button answers a side question - it doesn't count as the
    # answer to the pending flow question, so re-ask that question after.
    faqs_by_payload = {faq["payload"]: faq for faq in flow_cfg.get("faq_quick_replies", [])}
    if quick_reply_payload and quick_reply_payload in faqs_by_payload:
        answer = faqs_by_payload[quick_reply_payload]["answer"]
        current_prompt = flow_cfg.get("questions", {}).get(stage.value, {}).get("prompt", "")
        combined = f"{answer}\n\n{current_prompt}".strip()
        return ReplyPlan(
            should_reply=True,
            text=combined,
            delay_seconds=compute_delay(),
            intent=conversation.intent,
            handoff=False,
            new_stage=stage,
        )

    # AI agent path: free-text replies go to the model instead of the rigid
    # one-question-at-a-time script, when enabled and configured. Any
    # failure here (disabled, missing key, network error, malformed
    # response) falls straight through to the rigid flow below - the AI
    # layer can only add a better experience, never remove the working one.
    if settings.ai_agent_enabled and settings.openai_api_key:
        agent_result = ai_agent.generate_reply(
            history or [], text, known_fields=_known_fields(conversation)
        )
        if agent_result.success:
            return ReplyPlan(
                should_reply=True,
                text=agent_result.text,
                delay_seconds=compute_delay(),
                intent=conversation.intent,
                handoff=agent_result.handoff,
                new_stage=FlowStage.COMPLETE if agent_result.handoff else stage,
                captured=agent_result.extracted or None,
            )
        logger.warning(
            "AI agent unavailable (%s), falling back to the scripted flow", agent_result.error
        )

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
            new_stage=FlowStage.COMPLETE,
            captured=captured,
        )

    next_prompt = flow_cfg.get("questions", {}).get(following.value, {}).get("prompt", "")
    return ReplyPlan(
        should_reply=True,
        text=next_prompt,
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

    A SELL-intent lead runs the multi-step question flow until it completes.
    Every other intent gets exactly one acknowledgement; after that, or once
    the flow completes, a human owns the thread and later messages never get
    another automated reply.
    """
    if not settings.autoreply_enabled:
        return ReplyPlan(False, intent=detect_intent(text), reason="auto-reply disabled in config")

    if conversation.status in {ConversationStatus.HUMAN_HANDLED, ConversationStatus.CLOSED}:
        return ReplyPlan(False, intent=conversation.intent, reason=f"status is {conversation.status.value}")

    # Mid-flow: this message answers (or asks about) the outstanding question.
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

    # AI agent handles the very first reply too, for every intent - not just
    # SELL leads continuing a flow. Same fallback discipline as mid-flow: any
    # failure here falls straight through to the scripted paths below.
    if settings.ai_agent_enabled and settings.openai_api_key:
        agent_result = ai_agent.generate_reply(
            history or [], text, known_fields=_known_fields(conversation)
        )
        if agent_result.success:
            return ReplyPlan(
                should_reply=True,
                text=agent_result.text,
                delay_seconds=compute_delay(),
                intent=intent,
                handoff=agent_result.handoff,
                new_stage=FlowStage.COMPLETE if agent_result.handoff else FlowStage.AWAITING_ADDRESS,
                captured=agent_result.extracted or None,
            )
        logger.warning(
            "AI agent unavailable for first reply (%s), falling back to the scripted path",
            agent_result.error,
        )

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
        delay_seconds=compute_delay(),
        intent=intent,
        handoff=True,
    )


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

        first_name = (conversation.name or "").split()[0] if conversation.name else None
        plan = plan_reply(text, conversation, first_name, quick_reply_payload, history)

        if plan.intent is not Intent.UNKNOWN:
            conversation.intent = plan.intent

        if not plan.should_reply:
            logger.info("Not auto-replying to %s: %s", psid, plan.reason)
            # A follow-up message from someone a human already owns still needs
            # a human, so make sure it is flagged.
            if conversation.status is ConversationStatus.AUTO_REPLIED:
                conversation.status = ConversationStatus.AWAITING_HUMAN
            return plan

        conversation_id = conversation.id

    # Outside the transaction: the pause must not hold a database session open.
    logger.info(
        "Replying to %s in %.0fs (intent=%s)", psid, plan.delay_seconds, plan.intent.value
    )
    send_sender_action(psid, "mark_seen")
    if sleep:
        # Wait first, then show typing for the last stretch, so the indicator
        # appears when a person would actually start typing.
        typing_lead = min(plan.delay_seconds, 6.0)
        time.sleep(max(0.0, plan.delay_seconds - typing_lead))
        send_sender_action(psid, "typing_on")
        time.sleep(typing_lead)
    else:
        send_sender_action(psid, "typing_on")

    result = send_text(psid, plan.text, quick_replies=plan.quick_replies)

    with session_scope() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return plan

        # The person's answer was received regardless of whether our next
        # question makes it out, so save it either way.
        if plan.captured:
            for field, value in plan.captured.items():
                setattr(conversation, field, value)

        if result.success:
            # Stage only advances on a confirmed send - if it failed, a human
            # picks up from exactly where the automation left off.
            if plan.new_stage is not None:
                conversation.stage = plan.new_stage
            conversation.auto_replies_sent += 1
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
            conversation.status = ConversationStatus.AWAITING_HUMAN
            logger.error("Auto-reply failed for %s, flagged for human: %s", psid, result.error)

    return plan


def pending_handoffs() -> list[dict]:
    """Conversations waiting on a human, newest first. Feeds the dashboard."""
    with session_scope() as session:
        rows = session.scalars(
            select(Conversation)
            .where(Conversation.status == ConversationStatus.AWAITING_HUMAN)
            .order_by(Conversation.last_message_at.desc())
        ).all()
        return [
            {
                "psid": c.psid,
                "name": c.name or "(unknown)",
                "intent": c.intent.value,
                "address": c.property_address,
                "condition": c.condition,
                "timeline": c.timeline,
                "reason_for_selling": c.reason_for_selling,
                "phone_number": c.phone_number,
                "best_time_to_call": c.best_time_to_call,
                "last_message_at": c.last_message_at,
                "window_open": c.window_open,
            }
            for c in rows
        ]
