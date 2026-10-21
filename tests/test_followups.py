"""Tests for the 5 day follow-up list. Facebook is mocked."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from app.config import settings
from app.messenger import followups
from app.messenger.ai_agent import CLOSING_LINE
from app.messenger.followups import FollowUp, _evaluate, due_followups, suggested_message
from app.messenger.responder import OPT_OUT_REPLY

NOW = datetime(2026, 10, 21, 12, 0, 0)


def _ts(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _msg(text: str, from_page: bool, days_ago: float) -> dict:
    sender = settings.fb_page_id if from_page else "user_1"
    return {"message": text, "from": {"id": sender}, "created_time": _ts(days_ago)}


def _conversation(messages: list[dict], name: str = "Dejon Ellis", psid: str = "user_1") -> dict:
    return {
        "participants": {"data": [{"id": settings.fb_page_id, "name": "Elite Homes"}, {"id": psid, "name": name}]},
        "messages": {"data": messages},  # newest first, as Facebook returns them
    }


def _stalled(days_quiet: float = 5.5, last: str = "What city is the property in?", psid: str = "user_1") -> dict:
    return _conversation([
        _msg(last, True, days_quiet),
        _msg("I want to sell my house", False, days_quiet + 0.01),
    ], psid=psid)


def test_a_conversation_quiet_for_five_days_is_due_with_the_clients_message():
    due = _evaluate(_stalled(5.5), NOW)

    assert due is not None
    assert due.name == "Dejon Ellis" and due.first_name == "Dejon"
    assert due.suggested_message == "Hi Dejon, we wanted to follow up. Do you have any questions?"
    assert due.days_silent == 5.5
    assert due.can_still_message is True
    assert due.days_left == pytest.approx(1.5)
    assert due.last_page_message == "What city is the property in?"


def test_not_due_before_five_days():
    assert _evaluate(_stalled(4.9), NOW) is None


def test_past_metas_seven_day_window_it_is_listed_but_cannot_be_messaged():
    due = _evaluate(_stalled(8.4), NOW)
    assert due is not None
    assert due.can_still_message is False and due.days_left < 0


def test_when_their_message_is_the_latest_it_is_our_turn_not_a_follow_up():
    convo = _conversation([_msg("ok, here's the address", False, 6), _msg("What city?", True, 6.1)])
    assert _evaluate(convo, NOW) is None


@pytest.mark.parametrize(
    "last",
    [
        f"Thanks, Dana. I've passed your details to our team. {CLOSING_LINE}",
        OPT_OUT_REPLY,
        "Hi Dejon, we wanted to follow up. Do you have any questions?",
    ],
)
def test_closed_stopped_or_already_followed_up_conversations_are_skipped(last):
    assert _evaluate(_stalled(6, last=last), NOW) is None


def test_someone_who_asked_us_to_stop_is_never_followed_up():
    convo = _conversation([
        _msg("What city is the property in?", True, 6),
        _msg("please stop messaging me", False, 6.1),
        _msg("I want to sell", False, 6.2),
    ])
    assert _evaluate(convo, NOW) is None


def test_a_conversation_where_they_never_wrote_is_ignored():
    assert _evaluate(_conversation([_msg("Hello from our post", True, 6)]), NOW) is None


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Dana Whitfield", "Hi Dana, we wanted to follow up. Do you have any questions?"),
        ("Sunshine Homes LLC", "Hi, we wanted to follow up. Do you have any questions?"),
    ],
)
def test_a_business_name_is_never_used_to_greet(name, expected):
    assert _evaluate(_stalled(6) | {"participants": {"data": [
        {"id": settings.fb_page_id, "name": "Elite Homes"}, {"id": "user_1", "name": name}]}}, NOW).suggested_message == expected


def test_suggested_message_has_no_dash():
    for name in ("Dana", None):
        assert "—" not in suggested_message(name) and " - " not in suggested_message(name)


def _page(data, next_url=None):
    resp = Mock()
    body = {"data": data}
    if next_url:
        body["paging"] = {"next": next_url}
    resp.json.return_value = body
    return resp


def test_the_list_puts_people_about_to_leave_the_window_first_and_closed_ones_last():
    conversations = [
        _stalled(5.2) | {"participants": {"data": [{"id": "p", "name": "Early Person"}, {"id": "a", "name": "Early Person"}]}},
        _stalled(6.8) | {"participants": {"data": [{"id": "p", "name": "Urgent Person"}, {"id": "b", "name": "Urgent Person"}]}},
        _stalled(9.0) | {"participants": {"data": [{"id": "p", "name": "Closed Person"}, {"id": "c", "name": "Closed Person"}]}},
        _stalled(1.0),  # not due
    ]
    with patch("app.messenger.followups.requests.get", return_value=_page(conversations)):
        names = [f.name for f in due_followups(NOW)]

    assert names == ["Urgent Person", "Early Person", "Closed Person"]


def test_more_pages_of_conversations_are_followed():
    pages = [_page([_stalled(6)], next_url="https://graph.facebook.com/next"), _page([_stalled(7, psid="user_2")])]
    with patch("app.messenger.followups.requests.get", side_effect=pages) as mock_get:
        assert len(due_followups(NOW)) == 2
    assert mock_get.call_count == 2


def test_a_facebook_error_gives_an_empty_list_not_a_crash():
    resp = Mock()
    resp.json.return_value = {"error": {"message": "Invalid OAuth access token"}}
    with patch("app.messenger.followups.requests.get", return_value=resp):
        assert due_followups(NOW) == []


def test_the_team_page_needs_the_key_and_lists_who_is_due(monkeypatch):
    from fastapi.testclient import TestClient

    from app.messenger.webhook import create_app

    fake = FollowUp(
        psid="user_1", name="Dejon Ellis", first_name="Dejon", last_message_at=NOW, days_silent=5.5,
        last_page_message="What city is the property in?",
        suggested_message="Hi Dejon, we wanted to follow up. Do you have any questions?",
    )
    monkeypatch.setattr("app.messenger.followups.due_followups", lambda *a, **k: [fake])
    client = TestClient(create_app())

    assert client.get("/followups", params={"key": "wrong"}).status_code == 403
    html = client.get("/followups", params={"key": settings.fb_verify_token}).text
    assert "Dejon Ellis" in html and "we wanted to follow up" in html and "Copy message" in html
    data = client.get("/followups", params={"key": settings.fb_verify_token, "format": "json"}).json()
    assert data["count"] == 1 and data["followups"][0]["can_message"] is True
