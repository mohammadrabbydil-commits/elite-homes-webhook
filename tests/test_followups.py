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


def test_the_follow_up_comes_after_three_days_by_default():
    assert followups.FOLLOW_UP_AFTER_DAYS == settings.follow_up_after_days == 3


def test_a_conversation_quiet_for_three_days_is_due_with_the_clients_message():
    due = _evaluate(_stalled(3.5), NOW)

    assert due is not None
    assert due.name == "Dejon Ellis" and due.first_name == "Dejon"
    assert due.suggested_message == "Hi Dejon, we wanted to follow up. Do you have any questions?"
    assert due.days_silent == 3.5
    assert due.can_still_message is True
    assert due.days_left == pytest.approx(3.5)  # plenty of time inside Meta's 7 days
    assert due.last_page_message == "What city is the property in?"


def test_not_due_before_three_days():
    assert _evaluate(_stalled(2.9), NOW) is None


def test_the_follow_up_is_only_ever_sent_once_however_the_team_words_it():
    """Our automatic reply is one message. A second Page message since the
    customer last wrote means somebody already followed up."""
    already = _conversation([
        _msg("Just checking in, any questions about selling?", True, 3.2),   # the team's own wording
        _msg("What city is the property in?", True, 6.0),                   # our automatic reply
        _msg("I want to sell my house", False, 6.01),
    ])
    assert _evaluate(already, NOW) is None


def test_earlier_exchanges_do_not_count_as_a_follow_up():
    convo = _conversation([
        _msg("What shape is it in?", True, 3.5),
        _msg("123 Main St, Tampa", False, 3.6),
        _msg("Where is the property?", True, 3.7),
        _msg("I want to sell my house", False, 3.8),
    ])
    assert _evaluate(convo, NOW) is not None


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


# --- The automatic nudge (inside Meta's 24 hour window) -----------------------

from dataclasses import replace  # noqa: E402

from app.messenger.api import SendResult  # noqa: E402
from app.messenger.followups import _evaluate_nudge, run_nudges  # noqa: E402

# 2026-10-21 15:00 UTC is 11:00 in New York: inside business hours.
NUDGE_NOW = datetime(2026, 10, 21, 15, 0, 0)


def _nudge_convo(hours_quiet: float, hours_since_customer: float | None = None, **kw) -> dict:
    since = hours_since_customer if hours_since_customer is not None else hours_quiet + 0.03
    def stamp(hours):
        return (NUDGE_NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    convo = _conversation([
        {"message": "What city is the property in?", "from": {"id": settings.fb_page_id}, "created_time": stamp(hours_quiet)},
        {"message": "I want to sell my house", "from": {"id": "user_1"}, "created_time": stamp(since)},
    ], **kw)
    return convo


def _on(monkeypatch, **over):
    monkeypatch.setattr(followups, "settings", replace(settings, nudge_enabled=True, autoreply_enabled=True, **over))


def test_someone_quiet_for_20_hours_is_nudged(monkeypatch):
    _on(monkeypatch)
    n = _evaluate_nudge(_nudge_convo(21), NUDGE_NOW)
    assert n is not None
    assert n.text == "Hi Dejon, we wanted to follow up. Do you have any questions?"


@pytest.mark.parametrize("hours", [3, 19.9])
def test_not_nudged_too_soon(monkeypatch, hours):
    _on(monkeypatch)
    assert _evaluate_nudge(_nudge_convo(hours), NUDGE_NOW) is None


def test_never_nudged_once_metas_24_hour_window_is_nearly_over(monkeypatch):
    _on(monkeypatch)
    assert _evaluate_nudge(_nudge_convo(23.6), NUDGE_NOW) is None
    assert _evaluate_nudge(_nudge_convo(26), NUDGE_NOW) is None


def test_the_window_is_counted_from_the_customers_last_message(monkeypatch):
    """Our reply can come minutes after theirs; Meta counts from theirs."""
    _on(monkeypatch)
    assert _evaluate_nudge(_nudge_convo(21, hours_since_customer=23.7), NUDGE_NOW) is None


def test_the_hours_before_a_nudge_are_a_setting(monkeypatch):
    _on(monkeypatch, nudge_after_hours=2.0)
    assert _evaluate_nudge(_nudge_convo(2.5), NUDGE_NOW) is not None


def test_a_nudge_is_never_sent_twice(monkeypatch):
    _on(monkeypatch)
    convo = _nudge_convo(21)
    convo["messages"]["data"].insert(0, _msg("Hi Dejon, we wanted to follow up.", True, 0.1))
    assert _evaluate_nudge(convo, NUDGE_NOW) is None


def _run(monkeypatch, convos, now=NUDGE_NOW, **over):
    _on(monkeypatch, **over)
    sent = []
    monkeypatch.setattr(followups, "_conversations", lambda: convos)
    monkeypatch.setattr(
        followups, "send_text",
        lambda psid, text, quick_replies=None, messaging_type="RESPONSE": sent.append((psid, text, messaging_type)) or SendResult(True, "m"),
    )
    return run_nudges(now), sent


def test_run_sends_the_clients_message_as_an_update_inside_the_window(monkeypatch):
    summary, sent = _run(monkeypatch, [_nudge_convo(21)])
    assert summary["sent"] == 1 and summary["failed"] == 0
    assert sent == [("user_1", "Hi Dejon, we wanted to follow up. Do you have any questions?", "UPDATE")]


def test_run_does_nothing_unless_switched_on(monkeypatch):
    monkeypatch.setattr(followups, "settings", replace(settings, nudge_enabled=False, autoreply_enabled=True))
    monkeypatch.setattr(followups, "_conversations", lambda: [_nudge_convo(21)])
    assert run_nudges(NUDGE_NOW)["skipped"] == "nudges are switched off"
    monkeypatch.setattr(followups, "settings", replace(settings, nudge_enabled=True, autoreply_enabled=False))
    assert run_nudges(NUDGE_NOW)["skipped"] == "nudges are switched off"


def test_run_waits_for_business_hours(monkeypatch):
    night = datetime(2026, 10, 21, 5, 0, 0)  # 01:00 in New York
    summary, sent = _run(monkeypatch, [_nudge_convo(21)], now=night)
    assert summary["skipped"] == "outside business hours" and sent == []


def test_dry_run_reports_without_sending(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(followups, "_conversations", lambda: [_nudge_convo(21)])
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send"))
    monkeypatch.setattr(followups, "send_text", boom)
    summary = run_nudges(NUDGE_NOW, dry_run=True)
    assert summary["due"] == 1 and len(summary["would_send"]) == 1 and summary["sent"] == 0


def test_a_failed_send_is_counted_and_does_not_stop_the_others(monkeypatch):
    _on(monkeypatch)
    results = iter([SendResult(False, error="boom"), SendResult(True, "m")])
    monkeypatch.setattr(followups, "_conversations", lambda: [_nudge_convo(21, psid="a"), _nudge_convo(22, psid="b")])
    monkeypatch.setattr(followups, "send_text", lambda *a, **k: next(results))
    summary = run_nudges(NUDGE_NOW)
    assert summary["failed"] == 1 and summary["sent"] == 1


def test_one_run_never_sends_more_than_the_safety_limit(monkeypatch):
    convos = [_nudge_convo(21, psid=f"u{i}") for i in range(30)]
    summary, sent = _run(monkeypatch, convos)
    assert len(sent) == followups.NUDGE_MAX_PER_RUN == 20


def test_the_timer_endpoint_needs_its_own_key(monkeypatch):
    from fastapi.testclient import TestClient

    from app.messenger import webhook

    monkeypatch.setattr(webhook, "settings", replace(settings, nudge_key="secret-key", nudge_enabled=False))
    client = TestClient(webhook.create_app())
    assert client.get("/run-nudges", params={"key": "wrong"}).status_code == 403
    assert client.get("/run-nudges", params={"key": settings.fb_verify_token}).status_code == 403
    ok = client.get("/run-nudges", params={"key": "secret-key"})
    assert ok.status_code == 200 and ok.json()["skipped"] == "nudges are switched off"


def test_with_no_key_configured_the_endpoint_is_closed(monkeypatch):
    from fastapi.testclient import TestClient

    from app.messenger import webhook

    monkeypatch.setattr(webhook, "settings", replace(settings, nudge_key=""))
    client = TestClient(webhook.create_app())
    assert client.get("/run-nudges", params={"key": ""}).status_code == 403
