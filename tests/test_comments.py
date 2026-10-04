"""Tests for the comment auto-reply (public reply + private-reply DM).
All network calls are mocked.
"""

from __future__ import annotations

from unittest.mock import patch

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
from app.messenger.comment_responder import handle_inbound_comment, matches_keyword


def set_autoreply(monkeypatch, enabled: bool) -> None:
    from dataclasses import replace

    from app.config import settings as base

    monkeypatch.setattr(
        "app.messenger.comment_responder.settings", replace(base, autoreply_enabled=enabled)
    )


@pytest.fixture
def enabled(monkeypatch):
    set_autoreply(monkeypatch, True)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Do you buy houses for cash?", True),
        ("I want to sell my house", True),
        ("what's your offer like", True),
        ("nice photo!", False),
        ("", False),
    ],
)
def test_matches_keyword(text, expected):
    assert matches_keyword(text) is expected


def test_keyword_hit_posts_public_reply_and_opens_private_dm(db_session, enabled):
    with patch("app.messenger.comment_responder.reply_to_comment") as mock_public, \
         patch("app.messenger.comment_responder.send_private_reply") as mock_private:
        mock_public.return_value = SendResult(success=True, message_id="c_1")
        mock_private.return_value = SendResult(success=True, message_id="m_1")

        result = handle_inbound_comment("comment_1", "user_1", "Do you buy houses?", "Dana Lee")

    assert result == {"handled": True, "public_reply_sent": True, "private_reply_sent": True}
    mock_public.assert_called_once()
    mock_private.assert_called_once()

    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "user_1"))
    assert convo is not None
    assert convo.intent is Intent.SELL
    assert convo.stage is FlowStage.AWAITING_ADDRESS
    assert convo.status is ConversationStatus.AUTO_REPLIED

    messages = db_session.scalars(select(Message).order_by(Message.id)).all()
    assert len(messages) == 2
    assert all(m.direction is MessageDirection.OUTBOUND for m in messages)
    assert all(m.is_auto for m in messages)


def test_non_matching_comment_is_ignored(db_session, enabled):
    with patch("app.messenger.comment_responder.reply_to_comment") as mock_public, \
         patch("app.messenger.comment_responder.send_private_reply") as mock_private:
        result = handle_inbound_comment("comment_1", "user_1", "nice photo!", "Dana Lee")

    assert result == {"handled": False, "reason": "no trigger keyword"}
    mock_public.assert_not_called()
    mock_private.assert_not_called()


def test_duplicate_comment_delivery_is_ignored(db_session, enabled):
    with patch("app.messenger.comment_responder.reply_to_comment") as mock_public, \
         patch("app.messenger.comment_responder.send_private_reply") as mock_private:
        mock_public.return_value = SendResult(success=True, message_id="c_1")
        mock_private.return_value = SendResult(success=True, message_id="m_1")

        handle_inbound_comment("comment_1", "user_1", "I want to sell", "Dana")
        second = handle_inbound_comment("comment_1", "user_1", "I want to sell", "Dana")

    assert second == {"handled": False, "reason": "duplicate webhook delivery"}
    assert mock_public.call_count == 1
    assert mock_private.call_count == 1


def test_disabled_autoreply_skips_comment_handling(db_session, monkeypatch):
    set_autoreply(monkeypatch, False)
    with patch("app.messenger.comment_responder.reply_to_comment") as mock_public:
        result = handle_inbound_comment("comment_1", "user_1", "sell my house", "Dana")

    assert result == {"handled": False, "reason": "auto-reply disabled in config"}
    mock_public.assert_not_called()


def test_failed_private_reply_flags_for_human(db_session, enabled):
    with patch("app.messenger.comment_responder.reply_to_comment") as mock_public, \
         patch("app.messenger.comment_responder.send_private_reply") as mock_private:
        mock_public.return_value = SendResult(success=True, message_id="c_1")
        mock_private.return_value = SendResult(success=False, error="Outside window", error_code=10)

        result = handle_inbound_comment("comment_1", "user_1", "cash offer?", "Dana")

    assert result["private_reply_sent"] is False
    convo = db_session.scalar(select(Conversation).where(Conversation.psid == "user_1"))
    assert convo.status is ConversationStatus.AWAITING_HUMAN


def test_comment_from_the_page_itself_is_ignored(db_session, enabled):
    with patch("app.messenger.comment_responder.reply_to_comment") as mock_public:
        from app.config import settings

        result = handle_inbound_comment(
            "comment_1", settings.fb_page_id, "we buy houses for cash", "Elite Homes USA"
        )

    assert result == {"handled": False, "reason": "comment is from the Page itself"}
    mock_public.assert_not_called()


def test_comment_reply_copy_never_mentions_price():
    from app.messenger.comment_responder import _load_comment_reply_config

    config = _load_comment_reply_config()
    texts = [*config["public_replies"], config["private_reply"]]
    for text in texts:
        assert "$" not in text
