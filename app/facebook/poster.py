"""Facebook Graph API publishing and metrics for the Elite Homes USA Page.

Two entry points matter here:

    publish_post(message, image_url=None, scheduled_time=None)
        Publishes immediately, or hands the post to Facebook's own scheduler
        when `scheduled_time` is supplied.

    get_post_metrics(post_id)
        Pulls reach / engagement / clicks for a published post.

Both return a structured result rather than raising on API errors, so the
scheduler can record a failure and retry without unwinding the job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30

# Facebook requires scheduled posts to be 10 minutes to 6 months out.
MIN_SCHEDULE_LEAD = timedelta(minutes=10)
MAX_SCHEDULE_LEAD = timedelta(days=180)

# Insight metric names we request for a post.
REACH_METRIC = "post_impressions_unique"
ENGAGEMENT_METRIC = "post_engaged_users"
CLICKS_METRIC = "post_clicks"


class FacebookAPIError(RuntimeError):
    """Raised for configuration problems that make a call impossible."""


@dataclass
class PostResult:
    """Outcome of a publish attempt."""

    success: bool
    post_id: str | None = None
    scheduled: bool = False
    scheduled_for: datetime | None = None
    error: str | None = None
    error_code: int | None = None
    error_subcode: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_retryable(self) -> bool:
        """Whether a failure is worth retrying.

        Rate limits (4, 17, 32, 613) and transient service errors (1, 2) are
        retryable. Auth and permission errors (102, 190, 200, 10) are not -
        retrying those just burns quota against a broken token.
        """
        if self.success:
            return False
        return self.error_code in {1, 2, 4, 17, 32, 613}


@dataclass
class PostMetrics:
    """Reach / engagement / clicks snapshot for a post."""

    post_id: str
    reach: int = 0
    engagement: int = 0
    clicks: int = 0
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    success: bool = True
    error: str | None = None


def _require_config() -> None:
    missing = settings.missing_graph_settings()
    if missing:
        raise FacebookAPIError(
            "Missing Graph API settings: "
            + ", ".join(missing)
            + ". Copy config/.env.example to config/.env and fill them in."
        )


def _parse_error(payload: dict) -> tuple[str, int | None, int | None]:
    err = payload.get("error", {})
    message = err.get("message", "Unknown Graph API error")
    return message, err.get("code"), err.get("error_subcode")


def _to_unix_timestamp(when: datetime) -> int:
    """Convert a datetime to a UNIX timestamp, assuming UTC if naive."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp())


def _validate_schedule(scheduled_time: datetime) -> datetime:
    """Ensure a scheduled time sits inside the window Facebook accepts."""
    when = scheduled_time
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    lead = when - datetime.now(timezone.utc)
    if lead < MIN_SCHEDULE_LEAD:
        raise FacebookAPIError(
            f"scheduled_time must be at least 10 minutes in the future (got {lead})."
        )
    if lead > MAX_SCHEDULE_LEAD:
        raise FacebookAPIError(
            f"scheduled_time must be within 6 months (got {lead.days} days out)."
        )
    return when


def publish_post(
    message: str,
    image_url: str | None = None,
    scheduled_time: datetime | None = None,
    page_id: str | None = None,
    access_token: str | None = None,
) -> PostResult:
    """Publish a post to the Page, immediately or on Facebook's schedule.

    Args:
        message: The post body.
        image_url: Optional publicly reachable image URL. When supplied the
            post goes to /{page-id}/photos instead of /{page-id}/feed.
        scheduled_time: When supplied, the post is created unpublished with
            `scheduled_publish_time` so Facebook publishes it server-side. Must
            be 10 minutes to 6 months out.
        page_id: Overrides FB_PAGE_ID.
        access_token: Overrides FB_PAGE_ACCESS_TOKEN.

    Returns:
        PostResult. Check `.success` rather than catching exceptions - API
        errors are captured, not raised.
    """
    if not message or not message.strip():
        raise FacebookAPIError("Post message cannot be empty.")

    token = access_token or settings.fb_page_access_token
    target_page = page_id or settings.fb_page_id
    if not token or not target_page:
        _require_config()

    # Photo posts use a different endpoint and field name than text posts.
    endpoint = "photos" if image_url else "feed"
    payload: dict[str, Any] = {"access_token": token}
    if image_url:
        payload["url"] = image_url
        payload["caption"] = message
    else:
        payload["message"] = message

    if scheduled_time is not None:
        when = _validate_schedule(scheduled_time)
        payload["published"] = "false"
        payload["scheduled_publish_time"] = _to_unix_timestamp(when)
        logger.info("Scheduling %s post on page %s for %s", endpoint, target_page, when.isoformat())
    else:
        payload["published"] = "true"
        logger.info("Publishing %s post on page %s immediately", endpoint, target_page)

    url = f"{settings.graph_base_url}/{target_page}/{endpoint}"

    try:
        response = requests.post(url, data=payload, timeout=DEFAULT_TIMEOUT)
        data = response.json()
    except requests.RequestException as exc:
        logger.error("Network error publishing post: %s", exc)
        return PostResult(success=False, error=f"Network error: {exc}")
    except ValueError as exc:
        logger.error("Non-JSON response publishing post: %s", exc)
        return PostResult(success=False, error=f"Invalid JSON response: {exc}")

    if "error" in data or not response.ok:
        message_text, code, subcode = _parse_error(data)
        logger.error("Graph API refused the post: %s (code=%s subcode=%s)", message_text, code, subcode)
        return PostResult(
            success=False,
            error=message_text,
            error_code=code,
            error_subcode=subcode,
            raw=data,
        )

    # /feed returns "id"; /photos returns "id" plus "post_id".
    post_id = data.get("post_id") or data.get("id")
    is_scheduled = scheduled_time is not None
    logger.info("Post %s (id=%s)", "scheduled" if is_scheduled else "published", post_id)

    return PostResult(
        success=True,
        post_id=post_id,
        scheduled=is_scheduled,
        scheduled_for=scheduled_time if is_scheduled else None,
        raw=data,
    )


def get_post_metrics(post_id: str, access_token: str | None = None) -> PostMetrics:
    """Fetch reach, engagement, and clicks for a published post.

    Returns PostMetrics with `success=False` and zeroed counters if the
    insights call fails (common for very fresh posts, which have no insights
    for a few minutes after publication).
    """
    if not post_id:
        raise FacebookAPIError("post_id is required.")

    token = access_token or settings.fb_page_access_token
    if not token:
        _require_config()

    metrics = ",".join([REACH_METRIC, ENGAGEMENT_METRIC, CLICKS_METRIC])
    url = f"{settings.graph_base_url}/{post_id}/insights"

    try:
        response = requests.get(
            url, params={"metric": metrics, "access_token": token}, timeout=DEFAULT_TIMEOUT
        )
        data = response.json()
    except requests.RequestException as exc:
        logger.error("Network error fetching metrics for %s: %s", post_id, exc)
        return PostMetrics(post_id=post_id, success=False, error=f"Network error: {exc}")
    except ValueError as exc:
        return PostMetrics(post_id=post_id, success=False, error=f"Invalid JSON response: {exc}")

    if "error" in data or not response.ok:
        message_text, code, _ = _parse_error(data)
        logger.warning("Could not fetch metrics for %s: %s (code=%s)", post_id, message_text, code)
        return PostMetrics(post_id=post_id, success=False, error=message_text)

    parsed = _parse_insights(data.get("data", []))
    logger.info(
        "Metrics for %s: reach=%d engagement=%d clicks=%d",
        post_id,
        parsed[REACH_METRIC],
        parsed[ENGAGEMENT_METRIC],
        parsed[CLICKS_METRIC],
    )

    return PostMetrics(
        post_id=post_id,
        reach=parsed[REACH_METRIC],
        engagement=parsed[ENGAGEMENT_METRIC],
        clicks=parsed[CLICKS_METRIC],
    )


def _parse_insights(entries: list[dict]) -> dict[str, int]:
    """Flatten the Graph insights payload into {metric_name: value}."""
    result = {REACH_METRIC: 0, ENGAGEMENT_METRIC: 0, CLICKS_METRIC: 0}
    for entry in entries:
        name = entry.get("name")
        if name not in result:
            continue
        values = entry.get("values") or []
        if not values:
            continue
        value = values[0].get("value", 0)
        result[name] = int(value) if isinstance(value, (int, float)) else 0
    return result


def delete_post(post_id: str, access_token: str | None = None) -> bool:
    """Delete a post (used to cancel a Facebook-side scheduled post)."""
    token = access_token or settings.fb_page_access_token
    if not token:
        _require_config()

    try:
        response = requests.delete(
            f"{settings.graph_base_url}/{post_id}",
            params={"access_token": token},
            timeout=DEFAULT_TIMEOUT,
        )
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Failed to delete post %s: %s", post_id, exc)
        return False

    if data.get("success"):
        logger.info("Deleted post %s", post_id)
        return True

    logger.error("Could not delete post %s: %s", post_id, data)
    return False
