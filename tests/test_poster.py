"""Tests for app.facebook.poster - the Graph API payload is fully mocked.

The point of these tests is to pin down exactly what we send to Facebook:
endpoint, field names, and the published/scheduled_publish_time handling. A
regression there is invisible in code review but breaks posting in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.facebook.poster import (
    FacebookAPIError,
    PostResult,
    _parse_insights,
    get_post_metrics,
    publish_post,
)


def make_response(payload: dict, ok: bool = True, status: int = 200) -> MagicMock:
    """A stand-in for requests.Response carrying `payload`."""
    response = MagicMock()
    response.ok = ok
    response.status_code = status
    response.json.return_value = payload
    response.text = str(payload)
    return response


# --- Immediate text post ---------------------------------------------------


def test_publish_post_sends_correct_payload_for_text_post():
    with patch("app.facebook.poster.requests.post") as mock_post:
        mock_post.return_value = make_response({"id": "123456789_987654321"})

        result = publish_post("Thinking about selling your Jacksonville property?")

    assert result.success is True
    assert result.post_id == "123456789_987654321"
    assert result.scheduled is False

    mock_post.assert_called_once()
    url = mock_post.call_args.args[0]
    payload = mock_post.call_args.kwargs["data"]

    # Text posts go to /feed, not /photos.
    assert url.endswith("/123456789/feed")
    assert payload["message"] == "Thinking about selling your Jacksonville property?"
    assert payload["published"] == "true"
    assert payload["access_token"] == "test-page-token"
    assert "scheduled_publish_time" not in payload
    assert "url" not in payload


# --- Photo post ------------------------------------------------------------


def test_publish_post_with_image_uses_photos_endpoint_and_caption():
    with patch("app.facebook.poster.requests.post") as mock_post:
        mock_post.return_value = make_response({"id": "777", "post_id": "123_777"})

        result = publish_post("Check out this listing", image_url="https://cdn.test/house.jpg")

    assert result.success is True
    # /photos returns both id and post_id; we must prefer post_id.
    assert result.post_id == "123_777"

    url = mock_post.call_args.args[0]
    payload = mock_post.call_args.kwargs["data"]

    assert url.endswith("/123456789/photos")
    assert payload["url"] == "https://cdn.test/house.jpg"
    assert payload["caption"] == "Check out this listing"
    assert "message" not in payload


# --- Scheduled post --------------------------------------------------------


def test_publish_post_scheduled_sets_published_false_and_timestamp():
    when = datetime.now(timezone.utc) + timedelta(days=1)

    with patch("app.facebook.poster.requests.post") as mock_post:
        mock_post.return_value = make_response({"id": "123_555"})

        result = publish_post("Scheduled announcement", scheduled_time=when)

    assert result.success is True
    assert result.scheduled is True
    assert result.scheduled_for == when

    payload = mock_post.call_args.kwargs["data"]
    assert payload["published"] == "false"
    assert payload["scheduled_publish_time"] == int(when.timestamp())


def test_publish_post_treats_naive_scheduled_time_as_utc():
    naive = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(tzinfo=None)

    with patch("app.facebook.poster.requests.post") as mock_post:
        mock_post.return_value = make_response({"id": "123_556"})
        publish_post("Naive datetime", scheduled_time=naive)

    expected = int(naive.replace(tzinfo=timezone.utc).timestamp())
    assert mock_post.call_args.kwargs["data"]["scheduled_publish_time"] == expected


@pytest.mark.parametrize(
    "delta, fragment",
    [
        (timedelta(minutes=2), "at least 10 minutes"),
        (timedelta(days=200), "within 6 months"),
    ],
)
def test_publish_post_rejects_schedule_outside_facebook_window(delta, fragment):
    with patch("app.facebook.poster.requests.post") as mock_post:
        with pytest.raises(FacebookAPIError, match=fragment):
            publish_post("Too soon or too far", scheduled_time=datetime.now(timezone.utc) + delta)

    mock_post.assert_not_called()


# --- Error handling --------------------------------------------------------


def test_publish_post_captures_graph_api_error_without_raising():
    error_payload = {
        "error": {
            "message": "Invalid OAuth access token.",
            "type": "OAuthException",
            "code": 190,
            "error_subcode": 460,
        }
    }

    with patch("app.facebook.poster.requests.post") as mock_post:
        mock_post.return_value = make_response(error_payload, ok=False, status=400)
        result = publish_post("This will fail")

    assert result.success is False
    assert result.error == "Invalid OAuth access token."
    assert result.error_code == 190
    assert result.error_subcode == 460
    # An auth error must not be retried.
    assert result.is_retryable is False


def test_publish_post_marks_rate_limit_as_retryable():
    payload = {"error": {"message": "Application request limit reached", "code": 4}}

    with patch("app.facebook.poster.requests.post") as mock_post:
        mock_post.return_value = make_response(payload, ok=False, status=400)
        result = publish_post("Rate limited")

    assert result.success is False
    assert result.is_retryable is True


def test_publish_post_handles_network_error():
    import requests

    with patch("app.facebook.poster.requests.post", side_effect=requests.ConnectionError("boom")):
        result = publish_post("Network down")

    assert result.success is False
    assert "Network error" in result.error


def test_publish_post_rejects_empty_message():
    with pytest.raises(FacebookAPIError, match="cannot be empty"):
        publish_post("   ")


# --- Metrics ---------------------------------------------------------------


def test_get_post_metrics_parses_insights_response():
    payload = {
        "data": [
            {"name": "post_impressions_unique", "values": [{"value": 1420}]},
            {"name": "post_engaged_users", "values": [{"value": 87}]},
            {"name": "post_clicks", "values": [{"value": 34}]},
        ]
    }

    with patch("app.facebook.poster.requests.get") as mock_get:
        mock_get.return_value = make_response(payload)
        metrics = get_post_metrics("123_456")

    assert metrics.success is True
    assert (metrics.reach, metrics.engagement, metrics.clicks) == (1420, 87, 34)

    params = mock_get.call_args.kwargs["params"]
    assert params["access_token"] == "test-page-token"
    assert params["metric"] == "post_impressions_unique,post_engaged_users,post_clicks"


def test_get_post_metrics_returns_failure_on_api_error():
    payload = {"error": {"message": "Insights not available yet", "code": 100}}

    with patch("app.facebook.poster.requests.get") as mock_get:
        mock_get.return_value = make_response(payload, ok=False, status=400)
        metrics = get_post_metrics("123_456")

    assert metrics.success is False
    assert metrics.reach == 0
    assert "Insights not available" in metrics.error


def test_parse_insights_tolerates_missing_and_malformed_entries():
    parsed = _parse_insights(
        [
            {"name": "post_impressions_unique", "values": [{"value": 10}]},
            {"name": "post_engaged_users", "values": []},
            {"name": "unexpected_metric", "values": [{"value": 99}]},
        ]
    )

    assert parsed["post_impressions_unique"] == 10
    assert parsed["post_engaged_users"] == 0
    assert parsed["post_clicks"] == 0


def test_post_result_success_is_never_retryable():
    assert PostResult(success=True, post_id="1").is_retryable is False
