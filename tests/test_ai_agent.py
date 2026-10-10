"""Tests for the AI Messenger agent. All OpenAI calls are mocked - no network."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from app.messenger import ai_agent


def _openai_response(reply: str, handoff: bool = False, extracted: dict | None = None) -> Mock:
    body = {"reply": reply, "handoff": handoff, "extracted": extracted or {}}
    resp = Mock()
    resp.json.return_value = {"choices": [{"message": {"content": json.dumps(body)}}]}
    return resp


@pytest.fixture
def with_key(monkeypatch):
    from app.config import settings as base

    monkeypatch.setattr(ai_agent, "settings", replace(base, openai_api_key="test-key"))


def test_missing_key_fails_closed(monkeypatch):
    from app.config import settings as base

    monkeypatch.setattr(ai_agent, "settings", replace(base, openai_api_key=""))
    result = ai_agent.generate_reply([], "hello")
    assert result.success is False
    assert result.handoff is True


def test_normal_reply_extracts_fields(with_key):
    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        mock_post.return_value = _openai_response(
            "Thanks! What condition is it in?",
            handoff=False,
            extracted={"property_address": "123 Main St"},
        )
        result = ai_agent.generate_reply([], "I want to sell my house at 123 Main St")

    assert result.success is True
    assert result.handoff is False
    assert result.text == "Thanks! What condition is it in?"
    assert result.extracted == {"property_address": "123 Main St"}


def test_price_in_reply_is_blocked_even_if_model_ignored_the_prompt(with_key):
    """The prompt says never quote a price - this is the independent check
    that catches it anyway if the model does it."""
    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        mock_post.return_value = _openai_response("We could offer around $150,000 for it.")
        result = ai_agent.generate_reply([], "What will you pay me?")

    assert result.success is True
    assert result.safety_blocked is True
    assert result.handoff is True
    assert result.text == ai_agent.FALLBACK_REPLY
    assert "$" not in result.text


@pytest.mark.parametrize(
    "text",
    [
        "We could offer around $150,000 for it.",
        "Maybe 150k for a house like that.",
        "Something like 150 thousand is typical.",
    ],
)
def test_price_pattern_catches_common_phrasings(text):
    assert ai_agent._contains_price(text) is True


def test_ordinary_text_does_not_trip_the_price_check():
    assert ai_agent._contains_price("What's the best phone number to reach you?") is False
    assert ai_agent._contains_price("We buy houses as-is, no repairs needed.") is False


def test_unrecognized_extracted_field_is_dropped(with_key):
    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        mock_post.return_value = _openai_response(
            "Got it.", extracted={"property_address": "123 Main St", "favorite_color": "blue"}
        )
        result = ai_agent.generate_reply([], "hi")

    assert result.extracted == {"property_address": "123 Main St"}
    assert "favorite_color" not in result.extracted


def test_network_error_fails_closed(with_key):
    import requests

    with patch("app.messenger.ai_agent.requests.post", side_effect=requests.RequestException("boom")):
        result = ai_agent.generate_reply([], "hi")

    assert result.success is False
    assert result.handoff is True


def test_malformed_json_content_fails_closed(with_key):
    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        resp = Mock()
        resp.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
        mock_post.return_value = resp
        result = ai_agent.generate_reply([], "hi")

    assert result.success is False
    assert result.handoff is True


def test_openai_error_response_fails_closed(with_key):
    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        resp = Mock()
        resp.json.return_value = {"error": {"message": "invalid_api_key"}}
        mock_post.return_value = resp
        result = ai_agent.generate_reply([], "hi")

    assert result.success is False
    assert "invalid_api_key" in result.error


def test_empty_reply_fails_closed(with_key):
    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        mock_post.return_value = _openai_response("")
        result = ai_agent.generate_reply([], "hi")

    assert result.success is False
    assert result.handoff is True


def test_conversation_history_is_passed_in_role_order(with_key):
    from app.database.models import Message, MessageDirection

    history = [
        Message(direction=MessageDirection.INBOUND, text="hi"),
        Message(direction=MessageDirection.OUTBOUND, text="hello, what's up?"),
    ]
    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        mock_post.return_value = _openai_response("ok")
        ai_agent.generate_reply(history, "I want to sell")

    sent_messages = mock_post.call_args.kwargs["json"]["messages"]
    assert sent_messages[0]["role"] == "system"
    assert sent_messages[1] == {"role": "user", "content": "hi"}
    assert sent_messages[2] == {"role": "assistant", "content": "hello, what's up?"}
    assert sent_messages[3] == {"role": "user", "content": "I want to sell"}


def test_known_fields_are_injected_into_the_system_prompt(with_key):
    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        mock_post.return_value = _openai_response("Got it, what condition is it in?")
        ai_agent.generate_reply(
            [], "it's a rental", known_fields={"property_address": "123 Main St"}
        )

    system_content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "123 Main St" in system_content
    assert "already known" in system_content.lower()


def test_no_known_fields_block_when_nothing_captured_yet(with_key):
    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        mock_post.return_value = _openai_response("ok")
        ai_agent.generate_reply([], "hi", known_fields=None)
        ai_agent.generate_reply([], "hi", known_fields={})

    for call in mock_post.call_args_list:
        system_content = call.kwargs["json"]["messages"][0]["content"]
        assert "already known" not in system_content.lower()


def test_long_history_is_truncated_to_the_most_recent_messages(with_key):
    from app.database.models import Message, MessageDirection

    history = [
        Message(direction=MessageDirection.INBOUND, text=f"message {i}") for i in range(50)
    ]
    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        mock_post.return_value = _openai_response("ok")
        ai_agent.generate_reply(history, "latest message")

    sent_messages = mock_post.call_args.kwargs["json"]["messages"]
    # 1 system + 30 history + 1 current
    assert len(sent_messages) == 32
    assert sent_messages[1]["content"] == "message 20"  # oldest 20 dropped
    assert sent_messages[-2]["content"] == "message 49"


def test_network_error_retries_once_then_succeeds(with_key):
    import requests

    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        mock_post.side_effect = [requests.RequestException("boom"), _openai_response("ok")]
        result = ai_agent.generate_reply([], "hi")

    assert mock_post.call_count == 2
    assert result.success is True
    assert result.text == "ok"


def test_network_error_fails_closed_after_retry_also_fails(with_key):
    import requests

    with patch("app.messenger.ai_agent.requests.post") as mock_post:
        mock_post.side_effect = requests.RequestException("boom")
        result = ai_agent.generate_reply([], "hi")

    assert mock_post.call_count == 2
    assert result.success is False
    assert result.handoff is True
