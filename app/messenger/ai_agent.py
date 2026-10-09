"""AI-powered Messenger conversation agent.

Replaces the rigid, one-question-at-a-time flow for SELL-intent leads with a
real conversation: it reads what the person actually wrote and responds
naturally, while still gathering the same information the old flow collected
(address, condition, timeline, reason for selling, phone, best time to call).

Guardrails come first, not last - an ungoverned AI talking to real sellers
about their homes is a liability risk, not just a feature:

  - It can never state a price, a dollar figure, or make an offer.
  - It can never give legal, tax, or financial advice.
  - It hands off to a human the moment a conversation needs one - explicit
    request, frustration, anything outside home-selling, or once there's
    enough to work with.

Two independent layers enforce the price/offer rule: the system prompt tells
the model never to do it, and every reply is scanned for dollar signs and
price-shaped numbers before it's ever sent - if either fires, the reply is
discarded and swapped for a safe fallback with handoff forced on. The prompt
is necessary but not sufficient; the scan is what actually stops a leak.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import requests

from app.config import settings
from app.database.models import Message, MessageDirection

logger = logging.getLogger(__name__)

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_TIMEOUT = 30

# Fields the agent tries to collect over the course of a conversation -
# matches the columns already on Conversation.
EXTRACTABLE_FIELDS = (
    "property_address",
    "condition",
    "timeline",
    "reason_for_selling",
    "phone_number",
    "best_time_to_call",
)

SYSTEM_PROMPT = """You are the Messenger assistant for Elite Homes USA, a company that buys houses as-is in Jacksonville, FL and surrounding counties. You reply to every message people send the Page - sellers, buyers, wholesalers/partners, and general questions. There is no separate scripted reply for messages you're not sure about - you are the first reply for everything, so handle it yourself or hand off, never leave it unaddressed.

Tone: friendly, brief, conversational - like a helpful person texting, not a formal business letter. Short messages. No bullet lists in chat.

Your goal with a seller: naturally learn these things over the conversation, woven in, not a rigid interrogation and not all at once:
- property_address
- condition (repairs needed, vacant, tenant-occupied, etc.)
- timeline (how soon they want to sell)
- reason_for_selling
- phone_number
- best_time_to_call

For anyone else (a buyer, a wholesaler, a general question, small talk) - respond helpfully and naturally in your own words; there's no fixed script for these, just be useful and accurate.

Hard rules, never break these:
1. NEVER state a price, a dollar amount, a percentage, or any number that could be read as an offer or valuation. If asked what the house is worth or what you'll pay, say a team member will review the details and follow up with real numbers - do not estimate, guess, or give a range.
2. NEVER give legal, tax, or financial advice (probate, liens, foreclosure timelines, etc.). You can acknowledge the situation, but direct specifics to the team.
3. NEVER guarantee a specific closing date or outcome.
4. NEVER guess or make something up. If you don't actually know the answer to what someone is asking, or it needs information you don't have, say plainly that you'll get a team member to help with that specific thing - then hand off. A made-up answer is worse than no answer.
5. Stay on topic: Elite Homes USA's business. For anything clearly unrelated, be polite and suggest the team follow up.

Hand off to a human (set "handoff": true) when: the person explicitly asks for a human/person/call; they seem frustrated or upset; you don't know the answer to what they're asking (rule 4); or you've naturally gathered enough of the fields above to make a handoff useful for a seller. Otherwise keep the conversation going (set "handoff": false).

Respond with ONLY a JSON object, no other text, in this exact shape:
{"reply": "your message to send", "handoff": true or false, "extracted": {"field_name": "value", ...}}

Only include a field in "extracted" if the person actually gave that information in this message or earlier in the conversation and it hasn't been recorded yet. Omit fields you don't have. Never invent a value."""

# Catches a dollar sign, or a number followed by k/K/thousand/million as a
# bare quantity - the shapes a price or offer actually takes in text.
_PRICE_PATTERN = re.compile(
    r"\$\s?\d|\b\d{1,3}(,\d{3})*(\.\d+)?\s*(k\b|thousand|million)", re.IGNORECASE
)

FALLBACK_REPLY = "Great question - let me get our team to follow up with you on that directly."


@dataclass
class AgentReply:
    success: bool
    text: str = ""
    handoff: bool = True
    extracted: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    safety_blocked: bool = False


def _contains_price(text: str) -> bool:
    return bool(_PRICE_PATTERN.search(text))


def _build_messages(history: list[Message], user_message: str) -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history:
        role = "assistant" if m.direction == MessageDirection.OUTBOUND else "user"
        messages.append({"role": role, "content": m.text})
    messages.append({"role": "user", "content": user_message})
    return messages


def generate_reply(history: list[Message], user_message: str) -> AgentReply:
    """Call the AI agent for one turn of a conversation.

    `history` is prior messages in the thread, oldest first. Never raises -
    any failure (network, bad response, malformed JSON) comes back as a safe
    AgentReply with handoff=True rather than propagating.
    """
    if not settings.openai_api_key:
        return AgentReply(success=False, error="OPENAI_API_KEY not configured", handoff=True)

    payload = {
        "model": settings.ai_agent_model,
        "messages": _build_messages(history, user_message),
        "response_format": {"type": "json_object"},
    }

    try:
        response = requests.post(
            OPENAI_CHAT_URL,
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        )
        data = response.json()
    except requests.RequestException as exc:
        logger.error("AI agent network error: %s", exc)
        return AgentReply(success=False, error=f"Network error: {exc}", handoff=True)
    except ValueError as exc:
        logger.error("AI agent returned non-JSON response: %s", exc)
        return AgentReply(success=False, error=f"Invalid JSON response: {exc}", handoff=True)

    if "error" in data:
        msg = data["error"].get("message", "Unknown OpenAI error")
        logger.error("AI agent API error: %s", msg)
        return AgentReply(success=False, error=msg, handoff=True)

    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        reply_text = str(parsed["reply"]).strip()
        handoff = bool(parsed.get("handoff", False))
        extracted_raw = parsed.get("extracted") or {}
        extracted = {
            k: str(v).strip()[:500]
            for k, v in extracted_raw.items()
            if k in EXTRACTABLE_FIELDS and v
        }
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        logger.error("AI agent response did not match expected shape: %s (raw: %r)", exc, data)
        return AgentReply(success=False, error=f"Malformed agent response: {exc}", handoff=True)

    if not reply_text:
        return AgentReply(success=False, error="Agent returned an empty reply", handoff=True)

    # Second, independent check - the prompt says never quote a price, this
    # confirms it regardless of what the model actually did.
    if _contains_price(reply_text):
        logger.warning("AI agent reply blocked by price-pattern safety check: %r", reply_text)
        return AgentReply(
            success=True,
            text=FALLBACK_REPLY,
            handoff=True,
            extracted=extracted,
            safety_blocked=True,
        )

    return AgentReply(success=True, text=reply_text, handoff=handoff, extracted=extracted)
