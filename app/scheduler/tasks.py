"""APScheduler jobs that drive posting and analytics collection.

Two recurring jobs:

    publish_due_posts()     every minute  - publishes ScheduledPosts whose time
                                            has arrived, with bounded retries.
    refresh_post_analytics() every hour   - snapshots metrics for published posts.

The scheduler owns *when*; app.facebook.poster owns *how*.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import select

from app.config import POSTS_FILE, settings
from app.database.db import session_scope
from app.database.models import (
    PostAnalytics,
    PostStatus,
    PostType,
    ScheduledPost,
    utcnow,
)
from app.facebook.poster import FacebookAPIError, get_post_metrics, publish_post

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
# How long after publication we keep refreshing a post's metrics.
ANALYTICS_WINDOW = timedelta(days=30)


# --- Jobs ------------------------------------------------------------------


def publish_due_posts() -> int:
    """Publish every pending/retry post whose scheduled_time has passed.

    Returns the number of posts successfully published.
    """
    published = 0
    now = utcnow()

    with session_scope() as session:
        due = session.scalars(
            select(ScheduledPost)
            .where(
                ScheduledPost.status.in_([PostStatus.PENDING, PostStatus.RETRY]),
                ScheduledPost.scheduled_time <= now,
            )
            .order_by(ScheduledPost.scheduled_time)
        ).all()

        if not due:
            return 0

        logger.info("Found %d post(s) due for publication", len(due))

        for post in due:
            result = publish_post(message=post.content, image_url=post.media_url)

            if result.success:
                post.status = PostStatus.PUBLISHED
                post.fb_post_id = result.post_id
                post.published_at = utcnow()
                post.last_error = None
                published += 1
                logger.info("Published post id=%d as %s", post.id, result.post_id)
                continue

            post.retry_count += 1
            post.last_error = result.error

            if result.is_retryable and post.retry_count < MAX_RETRIES:
                post.status = PostStatus.RETRY
                logger.warning(
                    "Post id=%d failed (attempt %d/%d), will retry: %s",
                    post.id,
                    post.retry_count,
                    MAX_RETRIES,
                    result.error,
                )
            else:
                post.status = PostStatus.FAILED
                logger.error(
                    "Post id=%d failed permanently after %d attempt(s): %s",
                    post.id,
                    post.retry_count,
                    result.error,
                )

    return published


def hand_off_to_facebook() -> tuple[int, list[str]]:
    """Move every pending post onto Facebook's own scheduler.

    Once handed off, Facebook publishes each post server-side at its
    scheduled_time, so nothing on this machine needs to be running. Returns the
    number handed off and an error line for each post that could not be.
    """
    handed_off = 0
    errors: list[str] = []

    with session_scope() as session:
        pending = session.scalars(
            select(ScheduledPost)
            .where(ScheduledPost.status.in_([PostStatus.PENDING, PostStatus.RETRY]))
            .order_by(ScheduledPost.scheduled_time)
        ).all()

        for post in pending:
            try:
                result = publish_post(
                    message=post.content,
                    image_url=post.media_url,
                    scheduled_time=post.scheduled_time,
                )
            except FacebookAPIError as exc:
                # Usually a time under Facebook's 10-minute minimum lead.
                errors.append(f"post id={post.id}: {exc}")
                continue

            if not result.success:
                post.last_error = result.error
                errors.append(f"post id={post.id}: {result.error} (code={result.error_code})")
                continue

            post.status = PostStatus.SCHEDULED
            post.fb_post_id = result.post_id
            post.last_error = None
            handed_off += 1
            logger.info(
                "Post id=%d handed to Facebook's scheduler as %s", post.id, result.post_id
            )

    return handed_off, errors


def _mark_facebook_scheduled_as_published(session) -> None:
    """Facebook publishes handed-off posts itself; record that once time passes."""
    for post in session.scalars(
        select(ScheduledPost).where(
            ScheduledPost.status == PostStatus.SCHEDULED,
            ScheduledPost.scheduled_time <= utcnow(),
        )
    ).all():
        post.status = PostStatus.PUBLISHED
        post.published_at = post.scheduled_time


def refresh_post_analytics() -> int:
    """Snapshot metrics for recently published posts. Returns snapshot count."""
    taken = 0
    cutoff = utcnow() - ANALYTICS_WINDOW

    with session_scope() as session:
        _mark_facebook_scheduled_as_published(session)
        session.flush()
        posts = session.scalars(
            select(ScheduledPost).where(
                ScheduledPost.status == PostStatus.PUBLISHED,
                ScheduledPost.fb_post_id.is_not(None),
                ScheduledPost.published_at >= cutoff,
            )
        ).all()

        for post in posts:
            metrics = get_post_metrics(post.fb_post_id)
            if not metrics.success:
                logger.debug("Skipping analytics for post id=%d: %s", post.id, metrics.error)
                continue

            session.add(
                PostAnalytics(
                    post_id=post.id,
                    reach=metrics.reach,
                    engagement=metrics.engagement,
                    clicks=metrics.clicks,
                    fetched_at=utcnow(),
                )
            )
            taken += 1

    if taken:
        logger.info("Recorded %d analytics snapshot(s)", taken)
    return taken


# --- Seeding ---------------------------------------------------------------


def load_sample_posts(
    start_at: datetime | None = None,
    interval_hours: int = 24,
    skip_existing: bool = True,
) -> int:
    """Seed the three sample posts from data/posts.json into the queue.

    Posts are spaced `interval_hours` apart starting from `start_at`
    (default: one hour from now). Returns the number of posts inserted.
    """
    if not POSTS_FILE.exists():
        logger.error("Sample posts file not found: %s", POSTS_FILE)
        return 0

    payload = json.loads(POSTS_FILE.read_text(encoding="utf-8"))
    entries = payload.get("posts", [])
    base = start_at or (datetime.now(timezone.utc) + timedelta(hours=1))
    if base.tzinfo is not None:
        base = base.astimezone(timezone.utc).replace(tzinfo=None)

    inserted = 0
    with session_scope() as session:
        for index, entry in enumerate(entries):
            content = entry["content"]

            if skip_existing:
                exists = session.scalar(
                    select(ScheduledPost).where(ScheduledPost.content == content).limit(1)
                )
                if exists is not None:
                    logger.debug("Sample post %r already queued, skipping", entry.get("id"))
                    continue

            session.add(
                ScheduledPost(
                    content=content,
                    media_url=entry.get("media_url"),
                    post_type=PostType(entry["post_type"]),
                    scheduled_time=base + timedelta(hours=interval_hours * index),
                    status=PostStatus.PENDING,
                )
            )
            inserted += 1

    logger.info("Seeded %d sample post(s) from %s", inserted, POSTS_FILE.name)
    return inserted


# --- Scheduler wiring ------------------------------------------------------


def build_scheduler(blocking: bool = False) -> BackgroundScheduler | BlockingScheduler:
    """Create a scheduler with the recurring jobs registered."""
    cls = BlockingScheduler if blocking else BackgroundScheduler
    scheduler = cls(timezone=settings.timezone)

    scheduler.add_job(
        publish_due_posts,
        trigger="interval",
        minutes=1,
        id="publish_due_posts",
        name="Publish due posts",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_post_analytics,
        trigger="interval",
        hours=1,
        id="refresh_post_analytics",
        name="Refresh post analytics",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    logger.info("Scheduler configured with %d job(s), tz=%s", len(scheduler.get_jobs()), settings.timezone)
    return scheduler


def run_scheduler() -> None:
    """Run the scheduler in the foreground until interrupted."""
    scheduler = build_scheduler(blocking=True)
    logger.info("Scheduler starting. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")
