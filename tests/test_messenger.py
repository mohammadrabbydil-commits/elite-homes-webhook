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
)
from app.messenger.api import SendResult
from app.messenger.responder import (
    _load_seller_flow_config,
    compute_delay,
    detect_intent,
    handle_inbound_message,
    is_business_hours,
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


def test_delay_stays_under_two_minutes():
    """Speed-to-lead matters more than looking unhurried."""
    assert max(compute_delay() for _ in range(100)) < 120


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
    assert "address" in plan.text.lower()

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
    assert "fee" in faq_plan.text.lower()
    assert "address" in faq_plan.text.lower()  # the still-outstanding question is repeated

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
        flow_cfg["business_hours"]["greeting"],
        flow_cfg["after_hours"]["greeting"],
        flow_cfg["closing_message"],
        *(q["prompt"] for q in flow_cfg["questions"].values()),
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
        mock_agent.return_value = AgentReply(
            success=True,
            text="Got it, what condition is it in?",
            handoff=False,
            extracted={"property_address": "456 Oak Ave"},
        )

        handle_inbound_message("psid_1", "SELL 456 Oak Ave", mid="mid_1", sleep=False)
        handle_inbound_message("psid_1", "it's a rental, needs some work", mid="mid_2", sleep=False)

    mock_agent.assert_called_once()
    history_arg, message_arg = mock_agent.call_args.args
    assert message_arg == "it's a rental, needs some work"
    assert len(history_arg) == 2  # the greeting out, and the trigger message in

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
        mock_agent.return_value = AgentReply(
            success=True,
            text="Thanks, our team will reach out shortly!",
            handoff=True,
            extracted={"phone_number": "904-555-0100"},
        )

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
    second_outbound = mock_send.call_args_list[1].args[1]
    assert "condition" in second_outbound.lower()
