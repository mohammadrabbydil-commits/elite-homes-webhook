"""Tests for the Messenger auto-reply. All network calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.database.models import (
    Conversation,
    ConversationStatus,
    FlowStage,
    Intent,
    Message,
    MessageDirection,
    utcnow,
)
from app.messenger.ai_agent import AgentReply
from app.messenger.api import SendResult
from app.messenger.responder import (
    LEAD_NOT_SAVED_REPLY,
    OPT_OUT_REPLY,
    _load_seller_flow_config,
    compute_delay,
    detect_intent,
    handle_inbound_message,
    is_business_hours,
    is_opt_out_request,
    pending_handoffs,
    plan_reply,
)

EASTERN = ZoneInfo("America/New_York")


def set_autoreply(monkeypatch, enabled: bool) -> None:
    """Override autoreply_enabled.

    Settings is a frozen dataclass, so it cannot be mutated in place - swap in
    a modified copy on the module under test instead.
    """
    from dataclasses import replace

    from app.config import settings as base

    monkeypatch.setattr(
        "app.messenger.responder.settings", replace(base, autoreply_enabled=enabled)
    )


# --- Intent detection ------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("SELL 4218 Hendricks Ave", Intent.SELL),
        ("I want to sell my house", Intent.SELL),
        ("I inherited a property and it is vacant", Intent.SELL),
        ("BUY - looking for rentals in Duval", Intent.BUY),
        ("I am a wholesaler with a deal under contract", Intent.PARTNER),
        ("hello", Intent.UNKNOWN),
        ("", Intent.UNKNOWN),
    ],
)
def test_detect_intent(text, expected):
    assert detect_intent(text) is expected


def test_mixed_intent_resolves_to_sell():
    """Acquisition-only Page: any seller signal wins, even when outscored."""
    assert detect_intent("I want to sell my rental, I'm an investor") is Intent.SELL


# --- Humanisation ----------------------------------------------------------


def test_delay_is_randomised_within_configured_bounds():
    from app.config import settings

    delays = [compute_delay() for _ in range(50)]
    assert all(settings.autoreply_min_delay_seconds <= d <= settings.autoreply_max_delay_seconds
               for d in delays)
    # A fixed delay is as obvious a bot tell as an instant reply.
    assert len(set(round(d, 3) for d in delays)) > 40


def test_first_reply_delay_is_faster_than_later_replies():
    """Client preference: the first reply lands around 35s so a lead doesn't
    feel ignored; every reply after that is slower and more deliberate so it
    reads as human rather than instant."""
    from app.config import settings

    first_delays = [compute_delay(is_first_reply=True) for _ in range(50)]
    later_delays = [compute_delay(is_first_reply=False) for _ in range(50)]

    assert all(
        settings.autoreply_first_min_delay_seconds <= d <= settings.autoreply_first_max_delay_seconds
        for d in first_delays
    )
    assert all(
        settings.autoreply_min_delay_seconds <= d <= settings.autoreply_max_delay_seconds
        for d in later_delays
    )
    assert max(first_delays) < min(later_delays)


@pytest.mark.parametrize(
    "hour, expected",
    [(2, False), (7, False), (8, True), (12, True), (19, True), (20, False), (23, False)],
)
def test_business_hours_boundaries(hour, expected):
    when = datetime(2026, 9, 23, hour, 0, tzinfo=EASTERN)
    assert is_business_hours(when) is expected


def test_after_hours_reply_differs_from_business_hours():
    from app.messenger.responder import choose_reply

    with patch("app.messenger.responder.is_business_hours", return_value=True):
        day = {choose_reply(Intent.SELL, "Dana") for _ in range(30)}
    with patch("app.messenger.responder.is_business_hours", return_value=False):
        night = {choose_reply(Intent.SELL, "Dana") for _ in range(30)}

    assert not (day & night)
    assert any("morning" in reply for reply in night)


def test_replies_rotate_so_leads_do_not_see_identical_text():
    from app.messenger.responder import choose_reply

    with patch("app.messenger.responder.is_business_hours", return_value=True):
        assert len({choose_reply(Intent.SELL, "Dana") for _ in range(40)}) > 1


def test_reply_uses_first_name_and_falls_back_gracefully():
    from app.messenger.responder import choose_reply

    with patch("app.messenger.responder.is_business_hours", return_value=True):
        assert "Dana" in choose_reply(Intent.SELL, "Dana")
        assert "there" in choose_reply(Intent.SELL, None)


# --- Reply planning --------------------------------------------------------


def test_no_reply_when_disabled(db_session, monkeypatch):
    set_autoreply(monkeypatch, False)
    plan = plan_reply("SELL 123 Main St", Conversation(psid="p1"))
    assert plan.should_reply is False
    assert "disabled" in plan.reason


def test_only_one_auto_reply_per_conversation(db_session, monkeypatch):
    set_autoreply(monkeypatch, True)
    convo = Conversation(psid="p1", auto_replies_sent=1)
    plan = plan_reply("still there?", convo)

    assert plan.should_reply is False
    assert "human" in plan.reason


def test_no_reply_once_a_human_owns_the_thread(db_session, monkeypatch):
    set_autoreply(monkeypatch, True)
    convo = Conversation(psid="p1", status=ConversationStatus.HUMAN_HANDLED)
    assert plan_reply("hello again", convo).should_reply is False


# --- End to end ------------------------------------------------------------


@pytest.fixture
def enabled(monkeypatch):
    set_autoreply(monkeypatch, True)


def test_inbound_message_creates_conversation_and_replies(db_session, enabled):
    """A non-SELL intent still gets exactly one acknowledgement, then handoff."""
    with patch("app.messenger.responder.get_user_profile", return_value={"first_name": "Dana"}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send:
        mock_send.return_value = SendResult(success=True, message_id="m_1")
        plan = handle_inbound_message("psid_1", "BUY - looking for rentals", mid="mid_1", sleep=False)

    assert plan.should_reply is True
    assert plan.intent is Intent.BUY

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    assert convo.intent is Intent.BUY
    assert convo.auto_replies_sent == 1
    assert convo.status is ConversationStatus.AWAITING_HUMAN
    assert convo.handoff_at is not None

    messages = db_session.scalars(select(Message).order_by(Message.id)).all()
    assert [m.direction for m in messages] == [
        MessageDirection.INBOUND,
        MessageDirection.OUTBOUND,
    ]
    assert messages[1].is_auto is True


def test_typing_indicator_is_sent_before_the_reply(db_session, enabled):
    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action") as mock_action, \
         patch("app.messenger.responder.send_text") as mock_send:
        mock_send.return_value = SendResult(success=True, message_id="m_1")
        handle_inbound_message("psid_1", "SELL now", mid="mid_1", sleep=False)

    actions = [call.args[1] for call in mock_action.call_args_list]
    assert actions == ["mark_seen", "typing_on"]


def test_mark_seen_fires_near_the_reply_not_on_receipt(db_session, enabled):
    """mark_seen must not fire the instant the message arrives - a long
    'Seen' silence is as much a bot tell as an instant reply. It should land
    right before typing_on, after the main silent wait."""
    calls: list[tuple] = []

    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action",
               side_effect=lambda psid, action: calls.append(("action", action))), \
         patch("app.messenger.responder.send_text") as mock_send, \
         patch("app.messenger.responder.time.sleep",
               side_effect=lambda seconds: calls.append(("sleep", seconds))), \
         patch("app.messenger.responder.compute_delay", return_value=40.0):
        mock_send.return_value = SendResult(success=True, message_id="m_1")
        handle_inbound_message("psid_1", "SELL now", mid="mid_1", sleep=True)

    assert [kind for kind, _ in calls] == ["sleep", "action", "action", "sleep"]
    assert calls[1] == ("action", "mark_seen")
    assert calls[2] == ("action", "typing_on")
    # The silent wait before mark_seen is the long stretch, not the typing tail.
    assert calls[0][1] > calls[3][1]


def test_duplicate_webhook_delivery_is_ignored(db_session, enabled):
    """Facebook redelivers events; replying twice is an obvious bot tell."""
    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send:
        mock_send.return_value = SendResult(success=True, message_id="m_1")

        handle_inbound_message("psid_1", "SELL 123 Main", mid="mid_dup", sleep=False)
        second = handle_inbound_message("psid_1", "SELL 123 Main", mid="mid_dup", sleep=False)

    assert second.should_reply is False
    assert "duplicate" in second.reason
    assert mock_send.call_count == 1


def test_second_message_flags_for_human_without_replying(db_session, enabled):
    """A non-SELL conversation never gets a second automated reply."""
    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send:
        mock_send.return_value = SendResult(success=True, message_id="m_1")

        handle_inbound_message("psid_1", "BUY investor here", mid="mid_1", sleep=False)
        second = handle_inbound_message("psid_1", "anyone there?", mid="mid_2", sleep=False)

    assert second.should_reply is False
    assert mock_send.call_count == 1

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    assert convo.status is ConversationStatus.AWAITING_HUMAN
    # Both inbound messages are still recorded for the human picking it up.
    inbound = db_session.scalars(
        select(Message).where(Message.direction == MessageDirection.INBOUND)
    ).all()
    assert len(inbound) == 2


# --- Seller question-intake flow --------------------------------------------


def test_sell_message_starts_flow_with_greeting_and_faq_buttons(db_session, enabled):
    with patch("app.messenger.responder.get_user_profile", return_value={"first_name": "Dana"}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send:
        mock_send.return_value = SendResult(success=True, message_id="m_1")
        plan = handle_inbound_message("psid_1", "SELL 4218 Hendricks Ave", mid="mid_1", sleep=False)

    assert plan.should_reply is True
    assert plan.intent is Intent.SELL
    assert plan.quick_replies and len(plan.quick_replies) == 3
    # Introduces the virtual assistant, then asks where the property is.
    assert "virtual assistant" in plan.text.lower()
    assert any(w in plan.text.lower() for w in ("located", "city or zip", "where's the house"))

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    assert convo.status is ConversationStatus.AUTO_REPLIED  # not handed off yet
    assert convo.stage is FlowStage.AWAITING_ADDRESS
    assert convo.handoff_at is None

    # send_text received the quick replies, not just the greeting text.
    _, kwargs = mock_send.call_args
    assert kwargs["quick_replies"] and len(kwargs["quick_replies"]) == 3


def test_flow_collects_every_answer_then_hands_off(db_session, enabled):
    messages = [
        "SELL 4218 Hendricks Ave",      # trigger -> starts the flow, asks for the address
        "4218 Hendricks Ave",           # -> property_address
        "Needs a new roof",             # -> condition
        "Within 30 days",               # -> timeline
        "Inherited it, don't want it",  # -> reason_for_selling
        "904-555-0100",                 # -> phone_number
        "Weekday afternoons",           # -> best_time_to_call, completes the flow
    ]
    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send:
        mock_send.side_effect = [
            SendResult(success=True, message_id=f"m_{i}") for i in range(len(messages))
        ]
        plans = [
            handle_inbound_message("psid_1", text, mid=f"mid_{i}", sleep=False)
            for i, text in enumerate(messages)
        ]

    # Every step replies, and only the last one hands off.
    assert all(p.should_reply for p in plans)
    assert [p.handoff for p in plans] == [False, False, False, False, False, False, True]

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    assert convo.stage is FlowStage.COMPLETE
    assert convo.status is ConversationStatus.AWAITING_HUMAN
    assert convo.handoff_at is not None
    assert convo.property_address == "4218 Hendricks Ave"
    assert convo.condition == "Needs a new roof"
    assert convo.timeline == "Within 30 days"
    assert convo.reason_for_selling == "Inherited it, don't want it"
    assert convo.phone_number == "904-555-0100"
    assert convo.best_time_to_call == "Weekday afternoons"

    closing = _load_seller_flow_config()["closing_message"]
    assert plans[-1].text == closing
    from app.messenger.ai_agent import MEETING_LINK

    assert MEETING_LINK in closing


def test_faq_quick_reply_answers_without_advancing_the_flow(db_session, enabled):
    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send:
        mock_send.side_effect = [
            SendResult(success=True, message_id="m_1"),
            SendResult(success=True, message_id="m_2"),
        ]
        handle_inbound_message("psid_1", "SELL 123 Main", mid="mid_1", sleep=False)
        faq_plan = handle_inbound_message(
            "psid_1", "Do I pay any fees?", mid="mid_2", sleep=False,
            quick_reply_payload="FAQ_FEES",
        )

    assert faq_plan.should_reply is True
    # The client's approved fees answer - no invented "no fees" claim.
    assert "no cost to have us come take a look" in faq_plan.text.lower()
    # the still-outstanding question is repeated - several phrasings are possible
    assert any(w in faq_plan.text.lower() for w in ("address", "located", "city or zip"))

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    assert convo.stage is FlowStage.AWAITING_ADDRESS  # unchanged - FAQ tap isn't an answer
    assert convo.property_address is None


def test_failed_send_during_flow_does_not_advance_stage(db_session, enabled):
    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send:
        mock_send.side_effect = [
            SendResult(success=True, message_id="m_1"),
            SendResult(success=False, error="Outside window", error_code=10),
        ]
        handle_inbound_message("psid_1", "SELL 123 Main", mid="mid_1", sleep=False)
        handle_inbound_message("psid_1", "4218 Hendricks Ave", mid="mid_2", sleep=False)

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    # The answer was still saved even though our follow-up question failed to send.
    assert convo.property_address == "4218 Hendricks Ave"
    assert convo.stage is FlowStage.AWAITING_ADDRESS  # did not advance to AWAITING_CONDITION
    assert convo.status is ConversationStatus.AWAITING_HUMAN


def test_seller_flow_copy_never_mentions_price():
    """The flow gathers information - it must never quote a figure or make an offer."""
    flow_cfg = _load_seller_flow_config()
    texts = [
        *flow_cfg["business_hours"]["greetings"],
        *flow_cfg["after_hours"]["greetings"],
        flow_cfg["closing_message"],
        *(prompt for q in flow_cfg["questions"].values() for prompt in q["prompts"]),
        *(f["answer"] for f in flow_cfg["faq_quick_replies"]),
    ]
    for text in texts:
        assert "$" not in text


def test_failed_send_still_flags_for_human(db_session, enabled):
    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send:
        mock_send.return_value = SendResult(success=False, error="Outside window", error_code=10)
        handle_inbound_message("psid_1", "SELL 123 Main", mid="mid_1", sleep=False)

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    assert convo.status is ConversationStatus.AWAITING_HUMAN
    assert convo.auto_replies_sent == 0


def test_send_result_identifies_closed_messaging_window():
    assert SendResult(False, error_code=10).outside_window is True
    assert SendResult(False, error_code=190).outside_window is False


# --- Webhook ---------------------------------------------------------------


def test_signature_verification_accepts_valid_and_rejects_forged():
    import hashlib
    import hmac

    from app.config import settings
    from app.messenger.webhook import verify_signature

    payload = b'{"object":"page"}'
    good = hmac.new(
        settings.fb_app_secret.encode(), payload, hashlib.sha256
    ).hexdigest()

    assert verify_signature(payload, f"sha256={good}") is True
    assert verify_signature(payload, "sha256=" + "0" * 64) is False
    assert verify_signature(payload, None) is False
    assert verify_signature(b'{"object":"tampered"}', f"sha256={good}") is False


# --- AI agent integration (full pipeline, AI call mocked) -------------------


def _enable_ai_agent(monkeypatch):
    from dataclasses import replace

    from app.config import settings as base

    monkeypatch.setattr(
        "app.messenger.responder.settings",
        replace(base, autoreply_enabled=True, ai_agent_enabled=True, openai_api_key="test-key"),
    )


def test_ai_agent_drives_the_very_first_reply_when_enabled(db_session, monkeypatch):
    """The AI now handles message #1 too, not just continuations."""
    from app.messenger.ai_agent import AgentReply

    _enable_ai_agent(monkeypatch)

    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send, \
         patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_send.return_value = SendResult(success=True, message_id="m_1")
        mock_agent.return_value = AgentReply(
            success=True,
            text="Thanks for reaching out! What's the property address?",
            handoff=False,
            extracted={},
        )

        handle_inbound_message("psid_1", "SELL 456 Oak Ave", mid="mid_1", sleep=False)

    mock_agent.assert_called_once()
    history_arg, message_arg = mock_agent.call_args.args
    assert message_arg == "SELL 456 Oak Ave"
    assert history_arg == []  # nothing before the first message

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    assert convo.status is ConversationStatus.AUTO_REPLIED  # handoff=False, still ongoing


def test_ai_agent_drives_the_second_reply_when_enabled(db_session, monkeypatch):
    from app.messenger.ai_agent import AgentReply

    _enable_ai_agent(monkeypatch)

    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send, \
         patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_send.side_effect = [
            SendResult(success=True, message_id="m_1"),
            SendResult(success=True, message_id="m_2"),
        ]
        mock_agent.side_effect = [
            AgentReply(success=True, text="Thanks! What's the address?", handoff=False),
            AgentReply(
                success=True,
                text="Got it, what condition is it in?",
                handoff=False,
                extracted={"property_address": "456 Oak Ave"},
            ),
        ]

        handle_inbound_message("psid_1", "SELL 456 Oak Ave", mid="mid_1", sleep=False)
        handle_inbound_message("psid_1", "it's a rental, needs some work", mid="mid_2", sleep=False)

    assert mock_agent.call_count == 2
    history_arg, message_arg = mock_agent.call_args_list[1].args
    assert message_arg == "it's a rental, needs some work"
    assert len(history_arg) == 2  # the first AI reply out, and the trigger message in

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    assert convo.property_address == "456 Oak Ave"
    assert convo.status is ConversationStatus.AUTO_REPLIED  # handoff=False, still ongoing


def test_ai_agent_handoff_ends_the_conversation(db_session, monkeypatch):
    from app.messenger.ai_agent import AgentReply

    _enable_ai_agent(monkeypatch)

    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send, \
         patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_send.side_effect = [
            SendResult(success=True, message_id="m_1"),
            SendResult(success=True, message_id="m_2"),
        ]
        mock_agent.side_effect = [
            AgentReply(success=True, text="Thanks! What's the address?", handoff=False),
            AgentReply(
                success=True,
                text="Thanks, our team will reach out shortly!",
                handoff=True,
                extracted={"phone_number": "904-555-0100"},
            ),
        ]

        handle_inbound_message("psid_1", "SELL 456 Oak Ave", mid="mid_1", sleep=False)
        handle_inbound_message("psid_1", "call me at 904-555-0100", mid="mid_2", sleep=False)

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    assert convo.phone_number == "904-555-0100"
    assert convo.stage is FlowStage.COMPLETE
    assert convo.status is ConversationStatus.AWAITING_HUMAN


def test_ai_agent_failure_falls_back_to_scripted_flow(db_session, monkeypatch):
    _enable_ai_agent(monkeypatch)

    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send, \
         patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        from app.messenger.ai_agent import AgentReply

        mock_send.side_effect = [
            SendResult(success=True, message_id="m_1"),
            SendResult(success=True, message_id="m_2"),
        ]
        mock_agent.return_value = AgentReply(success=False, error="API down", handoff=True)

        handle_inbound_message("psid_1", "SELL 456 Oak Ave", mid="mid_1", sleep=False)
        handle_inbound_message("psid_1", "4218 Hendricks Ave", mid="mid_2", sleep=False)

    # Falls back to the rigid script's next question (condition) rather than failing silently.
    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_1"))
    assert convo.stage is FlowStage.AWAITING_CONDITION
    assert convo.property_address == "4218 Hendricks Ave"


def test_ai_agent_handles_a_long_conversation_without_re_asking_known_fields(db_session, monkeypatch):
    """Stress-test a longer, multi-turn conversation: fields accumulate
    correctly across many turns, each call is told what's already known, and
    the conversation still ends in a clean handoff."""
    from app.messenger.ai_agent import AgentReply

    _enable_ai_agent(monkeypatch)

    turns = [
        ("SELL my house please", AgentReply(success=True, text="Sure, what's the address?", handoff=False)),
        ("123 Main St", AgentReply(
            success=True, text="Got it, what condition is it in?", handoff=False,
            extracted={"property_address": "123 Main St"},
        )),
        ("needs a new roof", AgentReply(
            success=True, text="How soon are you looking to sell?", handoff=False,
            extracted={"condition": "needs a new roof"},
        )),
        ("within 2 months", AgentReply(
            success=True, text="What's driving the move?", handoff=False,
            extracted={"timeline": "within 2 months"},
        )),
        ("relocating for work", AgentReply(
            success=True, text="Best number to reach you?", handoff=False,
            extracted={"reason_for_selling": "relocating for work"},
        )),
        ("904-555-0100", AgentReply(
            success=True, text="And a good time to call?", handoff=False,
            extracted={"phone_number": "904-555-0100"},
        )),
        ("mornings work best", AgentReply(
            success=True, text="Perfect, our team will reach out shortly!", handoff=True,
            extracted={"best_time_to_call": "mornings work best"},
        )),
    ]

    with patch("app.messenger.responder.get_user_profile", return_value={}), \
         patch("app.messenger.responder.send_sender_action", return_value=True), \
         patch("app.messenger.responder.send_text") as mock_send, \
         patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_send.side_effect = [SendResult(success=True, message_id=f"m_{i}") for i in range(len(turns))]
        mock_agent.side_effect = [reply for _, reply in turns]

        for i, (text, _) in enumerate(turns):
            handle_inbound_message("psid_long", text, mid=f"mid_long_{i}", sleep=False)

    # Every call after the first was told exactly what had been captured so far.
    known_fields_per_call = [call.kwargs["known_fields"] for call in mock_agent.call_args_list]
    assert known_fields_per_call[0] == {}  # nothing captured before the first turn
    assert known_fields_per_call[1] == {}  # turn 0 extracted nothing
    assert known_fields_per_call[2] == {"property_address": "123 Main St"}  # turn 1's capture
    assert known_fields_per_call[-1] == {
        "property_address": "123 Main St",
        "condition": "needs a new roof",
        "timeline": "within 2 months",
        "reason_for_selling": "relocating for work",
        "phone_number": "904-555-0100",
    }

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "psid_long"))
    assert convo.property_address == "123 Main St"
    assert convo.condition == "needs a new roof"
    assert convo.timeline == "within 2 months"
    assert convo.reason_for_selling == "relocating for work"
    assert convo.phone_number == "904-555-0100"
    assert convo.best_time_to_call == "mornings work best"
    assert convo.status is ConversationStatus.AWAITING_HUMAN
    assert convo.stage is FlowStage.COMPLETE


# --- Client service policy: opt-out, Florida ZIPs, lead submission ----------


def _patched_send(send_results):
    """The usual Graph API patches, with send_text returning `send_results`."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("app.messenger.responder.get_user_profile", return_value={}))
    stack.enter_context(patch("app.messenger.responder.send_sender_action", return_value=True))
    mock_send = stack.enter_context(patch("app.messenger.responder.send_text"))
    mock_send.side_effect = send_results
    return stack, mock_send


def _convo(db_session, psid="psid_1") -> Conversation:
    db_session.expire_all()
    return db_session.scalar(select(Conversation).where(Conversation.psid == psid))


@pytest.mark.parametrize(
    "text",
    [
        "STOP", "stop", "Stop!", "unsubscribe", "opt out",
        "Please stop texting me", "stop messaging me please", "don't contact me again",
        "Do not text me.", "remove me from your list", "leave me alone",
    ],
)
def test_stop_requests_are_recognised(text):
    assert is_opt_out_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Can you stop by this weekend?",
        "Don't text me, call me instead",  # a contact preference, not a stop request
        "I don't want to stop the sale",
        "Do you buy houses?",
        "",
    ],
)
def test_ordinary_messages_are_not_stop_requests(text):
    assert is_opt_out_request(text) is False


def test_stop_request_is_confirmed_once_then_suppressed_for_good(db_session, enabled):
    stack, mock_send = _patched_send(
        [SendResult(success=True, message_id="m_1"), SendResult(success=True, message_id="m_2")]
    )
    with stack:
        handle_inbound_message("psid_1", "SELL my house", mid="mid_1", sleep=False)
        stop_plan = handle_inbound_message("psid_1", "STOP", mid="mid_2", sleep=False)
        later = handle_inbound_message("psid_1", "hello again?", mid="mid_3", sleep=False)

    assert stop_plan.should_reply is True
    assert mock_send.call_args_list[1].args[1] == OPT_OUT_REPLY
    assert later.should_reply is False
    assert later.reason == "contact opted out"
    assert mock_send.call_count == 2

    convo = _convo(db_session)
    assert convo.opted_out is True
    assert convo.status is ConversationStatus.CLOSED


def test_stop_request_never_reaches_the_ai_agent(db_session, monkeypatch):
    _enable_ai_agent(monkeypatch)
    stack, mock_send = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        handle_inbound_message("psid_1", "please stop messaging me", mid="mid_1", sleep=False)

    mock_agent.assert_not_called()
    assert mock_send.call_args.args[1] == OPT_OUT_REPLY


def test_stop_request_is_recorded_without_a_reply_when_a_human_owns_the_thread(db_session, enabled):
    db_session.add(Conversation(psid="psid_1", status=ConversationStatus.HUMAN_HANDLED))
    db_session.commit()

    stack, mock_send = _patched_send([])
    with stack:
        plan = handle_inbound_message("psid_1", "stop texting me", mid="mid_1", sleep=False)

    assert plan.should_reply is False
    mock_send.assert_not_called()
    assert _convo(db_session).opted_out is True


def test_agent_detected_opt_out_suppresses_the_contact(db_session, monkeypatch):
    """Backstop for stop requests the keyword check doesn't catch."""
    _enable_ai_agent(monkeypatch)
    stack, _ = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.return_value = AgentReply(
            success=True, text="Understood, we won't reach out again.", handoff=False, opt_out=True
        )
        handle_inbound_message("psid_1", "I'm done hearing from you people", mid="mid_1", sleep=False)

    convo = _convo(db_session)
    assert convo.opted_out is True
    assert convo.status is ConversationStatus.CLOSED


def test_ai_agent_is_never_called_once_a_human_owns_the_thread(db_session, monkeypatch):
    _enable_ai_agent(monkeypatch)
    db_session.add(Conversation(psid="psid_1", status=ConversationStatus.AWAITING_HUMAN))
    db_session.commit()

    stack, mock_send = _patched_send([])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        plan = handle_inbound_message("psid_1", "any update?", mid="mid_1", sleep=False)

    assert plan.should_reply is False
    mock_agent.assert_not_called()
    mock_send.assert_not_called()


def test_zip_is_looked_up_in_code_and_given_to_the_agent_as_a_fact(db_session, monkeypatch):
    _enable_ai_agent(monkeypatch)
    stack, _ = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.return_value = AgentReply(success=True, text="Yes, we cover that area!", handoff=False)
        handle_inbound_message("psid_1", "I have a house in 32114", mid="mid_1", sleep=False)

    facts = mock_agent.call_args.kwargs["location_facts"]
    assert len(facts) == 1
    assert "Daytona Beach" in facts[0] and "Volusia County" in facts[0]

    convo = _convo(db_session)
    assert (convo.zip_code, convo.county, convo.city) == ("32114", "Volusia County", "Daytona Beach")


def test_city_they_named_is_kept_when_it_differs_from_the_zip(db_session, monkeypatch):
    _enable_ai_agent(monkeypatch)
    stack, _ = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.return_value = AgentReply(
            success=True, text="Just to check, is it in Ocala or Jacksonville?", handoff=False,
            extracted={"city": "Ocala"},
        )
        handle_inbound_message("psid_1", "it's in Ocala, zip 32209", mid="mid_1", sleep=False)

    convo = _convo(db_session)
    assert convo.city == "Ocala"
    assert (convo.zip_code, convo.county) == ("32209", "Duval County")


def test_unlisted_florida_zip_is_covered_without_naming_a_county(db_session, monkeypatch):
    _enable_ai_agent(monkeypatch)
    stack, _ = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.return_value = AgentReply(success=True, text="Yes, we cover all of Florida!", handoff=False)
        handle_inbound_message("psid_1", "my zip is 32999", mid="mid_1", sleep=False)

    (fact,) = mock_agent.call_args.kwargs["location_facts"]
    assert "still in our service area" in fact

    convo = _convo(db_session)
    assert convo.zip_code == "32999"
    assert convo.county is None and convo.city is None


def test_message_without_a_zip_sends_no_location_facts(db_session, monkeypatch):
    _enable_ai_agent(monkeypatch)
    stack, _ = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.return_value = AgentReply(success=True, text="We do cover Daytona!", handoff=False)
        handle_inbound_message("psid_1", "I have a property in Daytona", mid="mid_1", sleep=False)

    assert mock_agent.call_args.kwargs["location_facts"] is None


def test_zip_details_carry_into_the_next_turn_as_known_facts(db_session, monkeypatch):
    _enable_ai_agent(monkeypatch)
    stack, _ = _patched_send(
        [SendResult(success=True, message_id="m_1"), SendResult(success=True, message_id="m_2")]
    )
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.side_effect = [
            AgentReply(success=True, text="Yes, we cover that area! What's your timeline?", handoff=False),
            AgentReply(success=True, text="Got it. What kind of shape is it in?", handoff=False),
        ]
        handle_inbound_message("psid_1", "32114", mid="mid_1", sleep=False)
        handle_inbound_message("psid_1", "next month", mid="mid_2", sleep=False)

    known = mock_agent.call_args_list[1].kwargs["known_fields"]
    assert known == {"zip_code": "32114", "city": "Daytona Beach", "county": "Volusia County"}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Do you buy houses?", Intent.SELL),
        ("Are you still buying in Jacksonville?", Intent.SELL),
        ("I have a property under contract. Are you buying?", Intent.PARTNER),
        ("Got a deal for you: 3/2 in Arlington, asking $95k, ARV $180k.", Intent.PARTNER),
        ("What's your buy box?", Intent.PARTNER),
        ("Off-market deal on the Westside. Want the details?", Intent.PARTNER),
        ("Can you send proof of funds?", Intent.PARTNER),
    ],
)
def test_intent_for_messages_from_the_client_test_sheet(text, expected):
    assert detect_intent(text) is expected


def test_agent_intent_overrides_the_keyword_guess(db_session, monkeypatch):
    _enable_ai_agent(monkeypatch)
    stack, _ = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.return_value = AgentReply(
            success=True, text="Thanks, we'll review this one.", handoff=False,
            intent="partner", submit_lead=True,
        )
        handle_inbound_message(
            "psid_1", "1234 Main St, Jacksonville FL, $120k. Interested?", mid="mid_1", sleep=False
        )

    assert _convo(db_session).intent is Intent.PARTNER


def test_request_for_a_person_submits_the_lead_while_the_bot_keeps_helping(db_session, monkeypatch):
    """A lead goes to the team without waiting for every detail."""
    _enable_ai_agent(monkeypatch)
    stack, _ = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.return_value = AgentReply(
            success=True,
            text="Of course. What's the best number to reach you, and when's a good time to call?",
            handoff=False,
            submit_lead=True,
        )
        handle_inbound_message("psid_1", "Can I talk to someone on the phone?", mid="mid_1", sleep=False)

    convo = _convo(db_session)
    assert convo.lead_submitted_at is not None
    assert convo.status is ConversationStatus.AUTO_REPLIED  # the bot can still collect the number
    assert [row["psid"] for row in pending_handoffs()] == ["psid_1"]


def test_lead_details_are_saved_before_the_reply_is_sent(db_session, monkeypatch):
    from app.database.db import session_scope

    _enable_ai_agent(monkeypatch)
    seen_at_send_time = {}

    def send_and_check(psid, text, quick_replies=None):
        with session_scope() as session:
            convo = session.scalar(select(Conversation).where(Conversation.psid == psid))
            seen_at_send_time["phone"] = convo.phone_number
            seen_at_send_time["lead"] = convo.lead_submitted_at
        return SendResult(success=True, message_id="m_1")

    stack, _ = _patched_send(send_and_check)
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.return_value = AgentReply(
            success=True, text="I've passed your details to our team.", handoff=False,
            submit_lead=True, extracted={"phone_number": "904-555-0100"},
        )
        handle_inbound_message("psid_1", "904-555-0100, text me", mid="mid_1", sleep=False)

    assert seen_at_send_time["phone"] == "904-555-0100"
    assert seen_at_send_time["lead"] is not None


def test_failed_lead_save_never_claims_the_details_were_passed_along(db_session, monkeypatch):
    from contextlib import contextmanager

    from app.messenger import responder

    _enable_ai_agent(monkeypatch)
    real_scope = responder.session_scope
    calls = {"n": 0}

    @contextmanager
    def scope_failing_on_the_lead_save():
        calls["n"] += 1
        if calls["n"] == 2:  # 1 = record the message, 2 = save the lead, 3 = after sending
            raise RuntimeError("database is locked")
        with real_scope() as session:
            yield session

    monkeypatch.setattr(responder, "session_scope", scope_failing_on_the_lead_save)

    stack, mock_send = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.return_value = AgentReply(
            success=True, text="I've passed your details to our team.", handoff=False,
            submit_lead=True, extracted={"phone_number": "904-555-0100"},
        )
        plan = handle_inbound_message("psid_1", "904-555-0100", mid="mid_1", sleep=False)

    assert mock_send.call_args.args[1] == LEAD_NOT_SAVED_REPLY
    assert plan.handoff is True
    assert _convo(db_session).status is ConversationStatus.AWAITING_HUMAN


def test_pending_handoffs_lists_leads_but_never_anyone_who_opted_out(db_session):
    db_session.add_all([
        Conversation(psid="handed_off", status=ConversationStatus.AWAITING_HUMAN),
        Conversation(psid="opted_out", status=ConversationStatus.AWAITING_HUMAN, opted_out=True),
        Conversation(psid="submitted", status=ConversationStatus.AUTO_REPLIED, lead_submitted_at=utcnow()),
        Conversation(psid="just_chatting", status=ConversationStatus.AUTO_REPLIED),
    ])
    db_session.commit()

    assert sorted(row["psid"] for row in pending_handoffs()) == ["handed_off", "submitted"]


def test_time_spent_thinking_counts_toward_the_reply_delay(db_session, enabled):
    """The client's ~35s first reply is measured from when the message
    arrived, so the AI's own processing time isn't added on top."""
    import itertools

    clock = itertools.chain([100.0], itertools.repeat(112.0))
    sleeps: list[float] = []
    stack, _ = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, \
         patch("app.messenger.responder.time.monotonic", side_effect=lambda: next(clock)), \
         patch("app.messenger.responder.time.sleep", side_effect=sleeps.append), \
         patch("app.messenger.responder.compute_delay", return_value=40.0):
        handle_inbound_message("psid_1", "SELL now", mid="mid_1", sleep=True)

    assert sum(sleeps) == pytest.approx(28.0)  # 40s target minus 12s already spent


def test_fallback_and_comment_copy_never_uses_a_dash():
    import json
    import re

    from app.config import TEMPLATES_FILE

    payload = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
    texts: list[str] = []

    def collect(node):
        if isinstance(node, str):
            texts.append(node)
        elif isinstance(node, list):
            for item in node:
                collect(item)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key != "_comment":
                    collect(value)

    for block in ("auto_reply", "seller_flow", "comment_reply"):
        collect(payload[block])

    from app.messenger.responder import LEAD_NOT_SAVED_REPLY, OPT_OUT_REPLY
    from app.messenger.ai_agent import FALLBACK_REPLY, TEAM_FOLLOW_UP

    texts += [OPT_OUT_REPLY, LEAD_NOT_SAVED_REPLY, FALLBACK_REPLY, TEAM_FOLLOW_UP]
    assert texts
    for text in texts:
        assert not re.search(r"[‒-―−﹘﹣－]|\s-\s", text), text


# --- Memory across database resets (history restored from Facebook) ----------


def _past(mid, text, from_page, day, hour=12):
    from datetime import datetime

    from app.messenger.api import PastMessage

    return PastMessage(mid=mid, text=text, from_page=from_page, created_at=datetime(2026, 10, day, hour))


def test_returning_person_gets_their_history_restored_from_facebook(db_session, monkeypatch):
    """The database was reset, but Facebook still has the conversation, so the
    agent sees their name from days ago and knows it already introduced itself."""
    _enable_ai_agent(monkeypatch)
    facebook_history = [
        _past("fb_1", "Hi, I want to sell my house in Tampa", False, 12, 10),
        _past("fb_2", "Hi! I'm Elite Homes USA Group's virtual assistant. What's your name?", True, 12, 11),
        _past("fb_3", "My name is Dana", False, 12, 12),
        _past("mid_today", "Do you remember me?", False, 15),  # the message being handled now
    ]
    monkeypatch.setattr("app.messenger.responder.get_conversation_history", lambda psid: facebook_history)

    stack, _ = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.return_value = AgentReply(success=True, text="Of course, Dana! How can I help?", handoff=False)
        handle_inbound_message("psid_1", "Do you remember me?", mid="mid_today", sleep=False)

    history_arg, message_arg = mock_agent.call_args.args
    assert message_arg == "Do you remember me?"
    assert [(m.direction, m.text) for m in history_arg] == [
        (MessageDirection.INBOUND, "Hi, I want to sell my house in Tampa"),
        (MessageDirection.OUTBOUND, "Hi! I'm Elite Homes USA Group's virtual assistant. What's your name?"),
        (MessageDirection.INBOUND, "My name is Dana"),
    ]

    stored = db_session.scalars(select(Message).order_by(Message.timestamp)).all()
    assert [m.mid for m in stored][:3] == ["fb_1", "fb_2", "fb_3"]
    assert sum(1 for m in stored if m.mid == "mid_today") == 1  # stored once, as the live message


def test_restored_history_is_not_fetched_again_once_the_database_has_it(db_session, monkeypatch):
    _enable_ai_agent(monkeypatch)
    calls = []

    def history(psid):
        calls.append(psid)
        return [_past("fb_1", "My name is Dana", False, 12)]

    monkeypatch.setattr("app.messenger.responder.get_conversation_history", history)
    stack, _ = _patched_send(
        [SendResult(success=True, message_id="m_1"), SendResult(success=True, message_id="m_2")]
    )
    with stack, patch("app.messenger.ai_agent.generate_reply") as mock_agent:
        mock_agent.side_effect = [
            AgentReply(success=True, text="Hi Dana! Where's the property?", handoff=False),
            AgentReply(success=True, text="Thanks! What shape is it in?", handoff=False),
        ]
        handle_inbound_message("psid_1", "hello again", mid="mid_1", sleep=False)
        handle_inbound_message("psid_1", "It's in Tampa", mid="mid_2", sleep=False)

    assert calls == ["psid_1"]
    assert "My name is Dana" in [m.text for m in mock_agent.call_args_list[1].args[0]]


def test_past_stop_request_is_still_honoured_after_a_reset(db_session, enabled):
    monkeypatch_history = [
        _past("fb_1", "I have a house in Ocala", False, 12, 10),
        _past("fb_2", "STOP", False, 12, 11),
    ]
    with patch("app.messenger.responder.get_conversation_history", return_value=monkeypatch_history):
        stack, mock_send = _patched_send([])
        with stack:
            plan = handle_inbound_message("psid_1", "hi", mid="mid_1", sleep=False)

    assert plan.should_reply is False
    mock_send.assert_not_called()
    convo = _convo(db_session)
    assert convo.opted_out is True
    assert convo.status is ConversationStatus.CLOSED


def test_messages_already_stored_are_not_duplicated_when_restoring(db_session, enabled):
    """A comment's private reply is already stored under its Facebook id."""
    convo = Conversation(psid="psid_1", status=ConversationStatus.AUTO_REPLIED, stage=FlowStage.AWAITING_ADDRESS)
    db_session.add(convo)
    db_session.flush()
    db_session.add(Message(conversation_id=convo.id, mid="fb_dm", direction=MessageDirection.OUTBOUND, text="Thanks for commenting!", is_auto=True))
    db_session.commit()

    history = [
        _past("fb_dm", "Thanks for commenting!", True, 14),
        _past("fb_older", "I commented on your post about selling", False, 13),
    ]
    with patch("app.messenger.responder.get_conversation_history", return_value=history):
        stack, _ = _patched_send([SendResult(success=True, message_id="m_1")])
        with stack:
            handle_inbound_message("psid_1", "123 Main St, Tampa", mid="mid_1", sleep=False)

    mids = [m.mid for m in db_session.scalars(select(Message)).all()]
    assert mids.count("fb_dm") == 1
    assert "fb_older" in mids


def test_no_facebook_history_still_replies_normally(db_session, enabled):
    stack, mock_send = _patched_send([SendResult(success=True, message_id="m_1")])
    with stack:
        plan = handle_inbound_message("psid_1", "SELL now", mid="mid_1", sleep=False)

    assert plan.should_reply is True
    mock_send.assert_called_once()
