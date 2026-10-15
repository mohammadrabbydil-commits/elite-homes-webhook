"""Tests for reading Messenger history from Facebook. Network calls are mocked."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock, patch

import requests

from app.config import settings
from app.messenger.api import get_conversation_history


def _response(body: dict) -> Mock:
    resp = Mock()
    resp.json.return_value = body
    return resp


def _message(mid: str, text: str, from_page: bool, when: str) -> dict:
    sender = settings.fb_page_id if from_page else "user_123"
    return {"id": mid, "message": text, "from": {"id": sender}, "created_time": when}


def test_history_is_returned_oldest_first_with_who_sent_each_message():
    body = {"data": [{"messages": {"data": [
        _message("m_3", "My name is Dana", False, "2026-10-12T10:02:00+0000"),
        _message("m_2", "Hi! What's your name?", True, "2026-10-12T10:01:00+0000"),
        _message("m_1", "  Hi there  ", False, "2026-10-12T10:00:00+0000"),
    ]}}]}
    with patch("app.messenger.api.requests.get", return_value=_response(body)):
        history = get_conversation_history("user_123")

    assert [m.mid for m in history] == ["m_1", "m_2", "m_3"]
    assert [m.from_page for m in history] == [False, True, False]
    assert history[0].text == "Hi there"
    assert history[0].created_at == datetime(2026, 10, 12, 10, 0)


def test_older_pages_are_followed_up_to_the_limit():
    first = {"data": [{"messages": {
        "data": [_message("m_2", "newer", False, "2026-10-12T10:01:00+0000")],
        "paging": {"next": "https://graph.facebook.com/next-page"},
    }}]}
    second = {
        "data": [_message("m_1", "older", False, "2026-10-12T10:00:00+0000")],
        "paging": {"next": "https://graph.facebook.com/another-page"},
    }
    with patch("app.messenger.api.requests.get", side_effect=[_response(first), _response(second)]) as mock_get:
        history = get_conversation_history("user_123", max_messages=2)

    assert [m.text for m in history] == ["older", "newer"]
    assert mock_get.call_count == 2  # stopped at the limit, never fetched a third page


def test_no_conversation_yet_returns_an_empty_history():
    with patch("app.messenger.api.requests.get", return_value=_response({"data": []})):
        assert get_conversation_history("user_123") == []


def test_facebook_error_returns_an_empty_history():
    body = {"error": {"message": "Invalid OAuth access token"}}
    with patch("app.messenger.api.requests.get", return_value=_response(body)):
        assert get_conversation_history("user_123") == []


def test_network_error_returns_an_empty_history():
    with patch("app.messenger.api.requests.get", side_effect=requests.RequestException("down")):
        assert get_conversation_history("user_123") == []
