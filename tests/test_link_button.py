"""The "Book with us" button message sent through the Graph API (mocked)."""

from __future__ import annotations

from unittest.mock import Mock, patch

import requests

from app.messenger.api import send_link_button


def _response(body, ok=True):
    resp = Mock()
    resp.json.return_value = body
    resp.ok = ok
    return resp


def test_button_message_has_the_book_with_us_label_and_the_link():
    with patch("app.messenger.api.requests.post", return_value=_response({"message_id": "m_1"})) as mock_post:
        result = send_link_button("psid_1", "Here is our calendar link.", "https://example.com/schedule")

    payload = mock_post.call_args.kwargs["json"]
    template = payload["message"]["attachment"]["payload"]
    assert payload["recipient"] == {"id": "psid_1"}
    assert payload["messaging_type"] == "RESPONSE"
    assert template["template_type"] == "button"
    assert template["text"] == "Here is our calendar link."
    assert template["buttons"] == [
        {"type": "web_url", "url": "https://example.com/schedule", "title": "Book with us"}
    ]
    assert result.success and result.message_id == "m_1"


def test_long_text_is_cut_to_facebooks_limit():
    with patch("app.messenger.api.requests.post", return_value=_response({"message_id": "m_1"})) as mock_post:
        send_link_button("psid_1", "x" * 900, "https://example.com")

    assert len(mock_post.call_args.kwargs["json"]["message"]["attachment"]["payload"]["text"]) == 640


def test_facebook_error_is_reported_not_raised():
    body = {"error": {"message": "Invalid template", "code": 100}}
    with patch("app.messenger.api.requests.post", return_value=_response(body, ok=False)):
        result = send_link_button("psid_1", "text", "https://example.com")

    assert result.success is False and result.error == "Invalid template" and result.error_code == 100


def test_network_error_is_reported_not_raised():
    with patch("app.messenger.api.requests.post", side_effect=requests.RequestException("down")):
        result = send_link_button("psid_1", "text", "https://example.com")

    assert result.success is False
