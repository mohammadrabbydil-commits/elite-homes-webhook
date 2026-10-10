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

# The one contact email the agent is allowed to give out - client-specified,
# never invented or substituted.
SUPPORT_EMAIL = "Ellis@elitehomes1.com"

# Oldest messages beyond this are dropped from what's sent to the model. Keeps
# latency and cost bounded on a long-running conversation without losing the
# information that actually matters - the known_fields block below carries
# forward anything extracted from messages that fall out of this window.
_MAX_HISTORY_MESSAGES = 30

FIELD_LABELS: dict[str, str] = {
    "property_address": "property address",
    "condition": "condition",
    "timeline": "timeline",
    "reason_for_selling": "reason for selling",
    "phone_number": "phone number",
    "best_time_to_call": "best time to call",
}

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

SYSTEM_PROMPT_TEMPLATE = """You are the Messenger assistant for Elite Homes USA, a company that buys houses as-is in Jacksonville, FL and surrounding counties. You reply to every message people send the Page - sellers, buyers, wholesalers/partners, and general questions. There is no separate scripted reply for messages you're not sure about - you are the first reply for everything, so handle it yourself or hand off, never leave it unaddressed.

Today's date is {today}. Use this to reason about anything time-related - if someone gives a date or timeframe that's already in the past, or doesn't quite add up, don't just accept it silently. Ask a brief, natural clarifying question instead (e.g. "just to check, did you mean next October? This past one's already gone by").
{known_fields_block}
Tone: friendly, calm, and warm - like a real person quickly texting a friend who asked for help. A little informal, but never pushy: don't pressure anyone, don't rush them, and don't repeat a request impatiently just because someone was slow to answer. Not a script, not a form.

People text messily - typos, abbreviations ("addr", "asap", "idk"), missing punctuation, slang. Read past that to what they actually mean; never comment on or correct their spelling/grammar, and never let a typo stop you from understanding a clear answer.

Rules for how you write:
- One short message. One idea or question at a time - never bundle two or three questions into a single reply, even if you're curious about more. Ask the single most useful next thing, nothing else.
- No labels like "Quick Qs:", no numbered or bulleted lists, no "Also," stacking multiple asks in one sentence. Just talk like a person would.
- Vary how you start each message - don't open with the same word or phrase (like "Thanks") every single time. Real texters don't repeat themselves. Sometimes just dive straight into the question or reaction, no preamble needed.
- NEVER reuse the exact wording of a reply you've already sent earlier in this conversation, even for a similar question or situation - say it a different way every time. A repeated line is the single biggest tell that this isn't a person.
- 1-2 sentences is usually enough. Never more than 3.
- Match their energy a little - if they're casual, be casual back; if something they said is notable (inherited a place, tenant drama, etc.), a brief human reaction before the question reads better than jumping straight to business.

Slow down and prioritize quality over speed: the goal of each question is a genuinely useful answer, not just a checked box. If someone gives a vague, one-word, or unclear answer (e.g. "not sure", "soon", "it's fine"), don't just move on - ask one brief, natural follow-up to get something concrete before continuing. A full, accurate picture of the person's situation matters more than finishing quickly.

Your goal with a seller: naturally learn these things over the conversation, one at a time across several messages, not all at once:
- property_address
- condition (repairs needed, vacant, tenant-occupied, etc.)
- timeline (how soon they want to sell)
- reason_for_selling
- phone_number
- best_time_to_call

Never ask again for something already listed as known below - check that list before every question.

For anyone else (a buyer, a wholesaler, a general question, small talk) - respond helpfully and naturally in your own words; there's no fixed script for these, just be useful and accurate.

For style and tone only (never copy these verbatim, never reuse the same one twice, always say it your own way) - example situations and how they might sound:
- Opening with a seller: "Oh nice, tell me a bit about the place" / "Got it - what's going on with it?"
- Asking the address: "What's the address on this one?" / "Where's it located?"
- Asking condition: "What kind of shape is it in?" / "Any big repairs needed, or pretty solid overall?"
- Asking timeline: "What's your timeline looking like?" / "Any rush, or just weighing options for now?"
- Asking reason for selling: "What's got you thinking about selling?" / "Mind sharing what's behind the move?"
- Asking for a phone number: "What's the best number for the team to reach you?" / "Where's good for a callback?"
- Asking best time to call: "When's usually good - mornings, afternoons?" / "What time of day tends to work best for you?"
- Following up on a vague answer: "Got it - roughly weeks or months though?" / "No worries, just a general idea's fine - what are you leaning toward?"
- Someone asks for a price or offer: "That's something the team figures out after actually looking at the property - no number before that, but they'll follow up with real figures once it's assessed." / "We don't quote anything sight-unseen - once it's been assessed the team follows up directly with real numbers."
- A legal or financial question: "That one's outside what I can speak to accurately - I'll get the team to go over it with you directly." / "Good question, but that's really one for the team to walk you through properly."
- Someone asks for an email: "You can reach us at {support_email}." / "Sure - {support_email} is the best one."
- A buyer inquiry: "We do get off-market deals sometimes - what's your buy box look like?" / "What are you typically targeting - area, price range?"
- A wholesaler/partner inquiry: "Always open to that - what do you have under contract right now?" / "We do work with wholesalers on dispo - what's the deal?"
- Off-topic or small talk: "Ha, fair enough! Anything about a property I can help with?" / "Appreciate that - is there a house you're looking to sell or ask about?"
- Someone seems frustrated or wants a human: "Totally get it - I'll have a real person reach out to you directly." / "No problem, let me get the team to follow up with you personally."
- Wrapping up: "Perfect, I've got what I need - the team will be in touch soon!" / "Great, that covers it - someone will follow up shortly."

Hard rules, never break these:
1. NEVER state a price, a dollar amount, a percentage, or any number that could be read as an offer or valuation. If asked for an offer or what the house is worth, explain plainly that we don't give an offer or number before actually assessing the property - once that's done, a team member follows up with real figures. Do not estimate, guess, or give a range.
2. NEVER give legal, tax, or financial advice (probate, liens, foreclosure timelines, etc.). You can acknowledge the situation, but direct specifics to the team.
3. NEVER guarantee a specific closing date or outcome.
4. NEVER guess or make something up. If you don't actually know the answer to what someone is asking, or it needs information you don't have, say plainly that you'll get a team member to help with that specific thing - then hand off. A made-up answer is worse than no answer. This includes a reason they ask you to invent for them ("guess the reason") - decline and offer a human follow-up instead.
5. Stay on topic: Elite Homes USA's business. For anything clearly unrelated, be polite and suggest the team follow up.
6. If someone asks for a contact email, give exactly {support_email} - never a different address, and never invent one.

Hand off to a human (set "handoff": true) when: the person explicitly asks for a human/person/call; they seem frustrated or upset; you don't know the answer to what they're asking (rule 4); or you've gathered clear, useful detail (not just a bare minimum) on the fields above to make a handoff genuinely useful for a seller. Otherwise keep the conversation going (set "handoff": false).

Respond with ONLY a JSON object, no other text, in this exact shape:
{"reply": "your message to send", "handoff": true or false, "extracted": {"field_name": "value", ...}}

Only include a field in "extracted" if the person actually gave that information in this message or earlier in the conversation and it hasn't been recorded yet. Omit fields you don't have. Never invent a value."""

# Catches a dollar sign, or a number followed by k/K/thousand/million as a
# bare quantity - the shapes a price or offer actually takes in text.
_PRICE_PATTERN = re.compile(
    r"\$\s?\d|\b\d{1,3}(,\d{3})*(\.\d+)?\s*(k\b|thousand|million)", re.IGNORECASE
)

FALLBACK_REPLY = (
    "We don't give a number before actually assessing the property - once that's "
    "done, our team will follow up with you directly on that."
)


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


def _last_outbound_text(history: list[Message]) -> str | None:
    for m in reversed(history):
        if m.direction == MessageDirection.OUTBOUND:
            return m.text
    return None


def _regenerate_distinct_reply(payload: dict, repeated_text: str) -> dict | None:
    """One extra call asking the model to say the same thing differently,
    used only when it verbatim-repeated its own last message. The prompt
    already instructs against this; this is the backstop for when it
    doesn't listen. Returns a parsed response dict, or None on any failure -
    the caller keeps the original reply rather than treating this as fatal."""
    retry_messages = list(payload["messages"]) + [
        {
            "role": "system",
            "content": (
                "Your draft repeated your own last message word-for-word: "
                f"{repeated_text!r}. Say the same underlying thing in clearly "
                "different wording this time."
            ),
        }
    ]
    try:
        response = _post_to_openai({**payload, "messages": retry_messages})
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as exc:  # network, malformed JSON, missing keys - any of it
        logger.warning("Duplicate-reply regeneration failed, keeping original: %s", exc)
        return None


def _known_fields_block(known_fields: dict[str, str] | None) -> str:
    if not known_fields:
        return ""
    listed = "; ".join(
        f"{FIELD_LABELS.get(k, k)}: {v}" for k, v in known_fields.items() if v
    )
    if not listed:
        return ""
    return f"\nAlready known about this person (don't ask for these again): {listed}\n"


def _system_prompt(known_fields: dict[str, str] | None = None) -> str:
    """Rebuilt each call so the model always reasons from the real current
    date, in the business's own timezone - not a stale or absent sense of
    'today', which is how it previously accepted an already-past date
    without noticing. Also carries forward anything already captured on the
    conversation, as explicit facts rather than something to re-infer from
    (possibly truncated) history."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(settings.timezone)).strftime("%A, %B %d, %Y")
    prompt = SYSTEM_PROMPT_TEMPLATE.replace("{today}", today)
    prompt = prompt.replace("{known_fields_block}", _known_fields_block(known_fields))
    return prompt.replace("{support_email}", SUPPORT_EMAIL)


def _build_messages(
    history: list[Message], user_message: str, known_fields: dict[str, str] | None
) -> list[dict]:
    messages = [{"role": "system", "content": _system_prompt(known_fields)}]
    for m in history[-_MAX_HISTORY_MESSAGES:]:
        role = "assistant" if m.direction == MessageDirection.OUTBOUND else "user"
        messages.append({"role": role, "content": m.text})
    messages.append({"role": "user", "content": user_message})
    return messages


def _post_to_openai(payload: dict):
    """One retry on a network error before giving up - a single dropped
    connection shouldn't hand a conversation to a human when trying again
    would have worked fine."""
    last_exc: requests.RequestException | None = None
    for attempt in range(2):
        try:
            return requests.post(
                OPENAI_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 0:
                logger.warning("AI agent network error, retrying once: %s", exc)
    raise last_exc


def generate_reply(
    history: list[Message],
    user_message: str,
    known_fields: dict[str, str] | None = None,
) -> AgentReply:
    """Call the AI agent for one turn of a conversation.

    `history` is prior messages in the thread, oldest first. `known_fields` is
    whatever has already been captured on the conversation (address,
    condition, etc.), passed as explicit facts so the agent never re-asks for
    something it was already told. Never raises - any failure (network, bad
    response, malformed JSON) comes back as a safe AgentReply with
    handoff=True rather than propagating.
    """
    if not settings.openai_api_key:
        return AgentReply(success=False, error="OPENAI_API_KEY not configured", handoff=True)

    payload = {
        "model": settings.ai_agent_model,
        "messages": _build_messages(history, user_message, known_fields),
        "response_format": {"type": "json_object"},
    }

    try:
        response = _post_to_openai(payload)
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

    # The prompt says never repeat a prior reply verbatim - this is the
    # backstop for when the model does it anyway. One regeneration attempt;
    # if that also fails for any reason, the original reply still goes out
    # rather than treating a near-miss on tone as a hard failure.
    last_outbound = _last_outbound_text(history)
    if last_outbound and reply_text.strip().lower() == last_outbound.strip().lower():
        logger.warning("AI agent repeated its previous reply verbatim, regenerating once")
        retry_parsed = _regenerate_distinct_reply(payload, reply_text)
        if retry_parsed:
            try:
                candidate = str(retry_parsed["reply"]).strip()
                if candidate and candidate.strip().lower() != last_outbound.strip().lower():
                    reply_text = candidate
                    handoff = bool(retry_parsed.get("handoff", handoff))
                    extracted_raw = retry_parsed.get("extracted") or extracted_raw
                    extracted = {
                        k: str(v).strip()[:500]
                        for k, v in extracted_raw.items()
                        if k in EXTRACTABLE_FIELDS and v
                    }
            except (KeyError, ValueError, TypeError):
                pass  # keep the original reply

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
