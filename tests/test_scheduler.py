"""Tests for the scheduler jobs, with the Graph API mocked out."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import select

from app.database.models import (
    PostAnalytics,
    PostStatus,
    PostType,
    ScheduledPost,
    utcnow,
)
from app.facebook.poster import FacebookAPIError, PostMetrics, PostResult
from app.scheduler.tasks import (
    MAX_RETRIES,
    build_scheduler,
    hand_off_to_facebook,
    load_sample_posts,
    publish_due_posts,
    refresh_post_analytics,
)


def queue_post(session, *, minutes_offset: int, status=PostStatus.PENDING, retry_count=0):
    post = ScheduledPost(
        content=f"Post due at {minutes_offset} minutes",
        post_type=PostType.HOMEOWNER,
        scheduled_time=utcnow() + timedelta(minutes=minutes_offset),
        status=status,
        retry_count=retry_count,
    )
    session.add(post)
    session.commit()
    return post


# --- publish_due_posts -----------------------------------------------------


def test_publish_due_posts_publishes_only_due_posts(db_session):
    due = queue_post(db_session, minutes_offset=-5)
    future = queue_post(db_session, minutes_offset=60)

    with patch("app.scheduler.tasks.publish_post") as mock_publish:
        mock_publish.return_value = PostResult(success=True, post_id="123_999")
        published = publish_due_posts()

    assert published == 1
    assert mock_publish.call_count == 1

    db_session.expire_all()
    assert db_session.get(ScheduledPost, due.id).status is PostStatus.PUBLISHED
    assert db_session.get(ScheduledPost, due.id).fb_post_id == "123_999"
    assert db_session.get(ScheduledPost, due.id).published_at is not None
    assert db_session.get(ScheduledPost, future.id).status is PostStatus.PENDING


def test_publish_due_posts_returns_zero_when_nothing_due(db_session):
    queue_post(db_session, minutes_offset=120)

    with patch("app.scheduler.tasks.publish_post") as mock_publish:
        assert publish_due_posts() == 0

    mock_publish.assert_not_called()


def test_retryable_failure_moves_post_to_retry(db_session):
    post = queue_post(db_session, minutes_offset=-1)

    with patch("app.scheduler.tasks.publish_post") as mock_publish:
        mock_publish.return_value = PostResult(
            success=False, error="Rate limit", error_code=4
        )
        assert publish_due_posts() == 0

    db_session.expire_all()
    stored = db_session.get(ScheduledPost, post.id)
    assert stored.status is PostStatus.RETRY
    assert stored.retry_count == 1
    assert stored.last_error == "Rate limit"


def test_non_retryable_failure_fails_immediately(db_session):
    post = queue_post(db_session, minutes_offset=-1)

    with patch("app.scheduler.tasks.publish_post") as mock_publish:
        mock_publish.return_value = PostResult(
            success=False, error="Invalid OAuth access token.", error_code=190
        )
        publish_due_posts()

    db_session.expire_all()
    stored = db_session.get(ScheduledPost, post.id)
    assert stored.status is PostStatus.FAILED
    assert stored.retry_count == 1


def test_retries_are_capped_at_max_retries(db_session):
    post = queue_post(
        db_session,
        minutes_offset=-1,
        status=PostStatus.RETRY,
        retry_count=MAX_RETRIES - 1,
    )

    with patch("app.scheduler.tasks.publish_post") as mock_publish:
        mock_publish.return_value = PostResult(success=False, error="Rate limit", error_code=4)
        publish_due_posts()

    db_session.expire_all()
    stored = db_session.get(ScheduledPost, post.id)
    assert stored.retry_count == MAX_RETRIES
    assert stored.status is PostStatus.FAILED


def test_retry_status_posts_are_picked_up(db_session):
    post = queue_post(db_session, minutes_offset=-10, status=PostStatus.RETRY, retry_count=1)

    with patch("app.scheduler.tasks.publish_post") as mock_publish:
        mock_publish.return_value = PostResult(success=True, post_id="123_retry")
        assert publish_due_posts() == 1

    db_session.expire_all()
    assert db_session.get(ScheduledPost, post.id).status is PostStatus.PUBLISHED


# --- refresh_post_analytics ------------------------------------------------


def test_refresh_post_analytics_records_snapshot(db_session):
    post = ScheduledPost(
        content="Published post",
        post_type=PostType.BUYER,
        scheduled_time=utcnow() - timedelta(hours=2),
        status=PostStatus.PUBLISHED,
        fb_post_id="123_456",
        published_at=utcnow() - timedelta(hours=1),
    )
    db_session.add(post)
    db_session.commit()

    with patch("app.scheduler.tasks.get_post_metrics") as mock_metrics:
        mock_metrics.return_value = PostMetrics(
            post_id="123_456", reach=800, engagement=60, clicks=15
        )
        assert refresh_post_analytics() == 1

    snapshot = db_session.scalar(select(PostAnalytics))
    assert (snapshot.reach, snapshot.engagement, snapshot.clicks) == (800, 60, 15)
    assert snapshot.post_id == post.id


def test_refresh_post_analytics_skips_failed_metric_fetch(db_session):
    db_session.add(
        ScheduledPost(
            content="Fresh post",
            post_type=PostType.PARTNER,
            scheduled_time=utcnow(),
            status=PostStatus.PUBLISHED,
            fb_post_id="123_789",
            published_at=utcnow(),
        )
    )
    db_session.commit()

    with patch("app.scheduler.tasks.get_post_metrics") as mock_metrics:
        mock_metrics.return_value = PostMetrics(
            post_id="123_789", success=False, error="Insights not available yet"
        )
        assert refresh_post_analytics() == 0

    assert db_session.scalars(select(PostAnalytics)).all() == []


def test_refresh_post_analytics_ignores_unpublished_posts(db_session):
    queue_post(db_session, minutes_offset=60)

    with patch("app.scheduler.tasks.get_post_metrics") as mock_metrics:
        assert refresh_post_analytics() == 0

    mock_metrics.assert_not_called()


# --- Seeding and wiring ----------------------------------------------------


def test_load_sample_posts_seeds_the_acquisition_set(db_session):
    assert load_sample_posts() == 6

    posts = db_session.scalars(select(ScheduledPost).order_by(ScheduledPost.scheduled_time)).all()
    assert len(posts) == 6
    # Client confirmed 2026-09-23: acquisition only, no buyer or partner content.
    assert all(p.post_type is PostType.HOMEOWNER for p in posts)
    assert posts[0].content.startswith("Need to sell")
    assert all(p.status is PostStatus.PENDING for p in posts)

    # Posts are spaced 24 hours apart by default.
    assert (posts[1].scheduled_time - posts[0].scheduled_time) == timedelta(hours=24)


def test_load_sample_posts_is_idempotent(db_session):
    assert load_sample_posts() == 6
    assert load_sample_posts() == 0
    assert len(db_session.scalars(select(ScheduledPost)).all()) == 6


def test_build_scheduler_registers_both_jobs():
    # build_scheduler() configures but does not start the scheduler, so there is
    # nothing to shut down here.
    scheduler = build_scheduler()
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {"publish_due_posts", "refresh_post_analytics"}

    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert jobs["publish_due_posts"].trigger.interval == timedelta(minutes=1)
    assert jobs["refresh_post_analytics"].trigger.interval == timedelta(hours=1)


# --- hand_off_to_facebook --------------------------------------------------


def test_hand_off_schedules_pending_posts_on_facebook(db_session):
    post = queue_post(db_session, minutes_offset=180)

    with patch("app.scheduler.tasks.publish_post") as mock_publish:
        mock_publish.return_value = PostResult(success=True, post_id="123_777", scheduled=True)
        handed_off, errors = hand_off_to_facebook()

    assert (handed_off, errors) == (1, [])
    assert mock_publish.call_args.kwargs["scheduled_time"] == post.scheduled_time

    db_session.expire_all()
    stored = db_session.get(ScheduledPost, post.id)
    assert stored.status is PostStatus.SCHEDULED
    assert stored.fb_post_id == "123_777"


def test_hand_off_leaves_post_pending_when_facebook_rejects_it(db_session):
    post = queue_post(db_session, minutes_offset=5)

    with patch("app.scheduler.tasks.publish_post") as mock_publish:
        mock_publish.side_effect = FacebookAPIError("too soon")
        handed_off, errors = hand_off_to_facebook()

    assert handed_off == 0
    assert len(errors) == 1
    db_session.expire_all()
    assert db_session.get(ScheduledPost, post.id).status is PostStatus.PENDING


def test_publish_due_posts_never_republishes_facebook_scheduled_posts(db_session):
    queue_post(db_session, minutes_offset=-5, status=PostStatus.SCHEDULED)

    with patch("app.scheduler.tasks.publish_post") as mock_publish:
        assert publish_due_posts() == 0

    mock_publish.assert_not_called()


def test_analytics_job_marks_facebook_scheduled_posts_published_once_due(db_session):
    due = queue_post(db_session, minutes_offset=-5, status=PostStatus.SCHEDULED)
    future = queue_post(db_session, minutes_offset=60, status=PostStatus.SCHEDULED)

    with patch("app.scheduler.tasks.get_post_metrics") as mock_metrics:
        mock_metrics.return_value = PostMetrics(post_id="x", success=False, error="n/a")
        refresh_post_analytics()

    db_session.expire_all()
    assert db_session.get(ScheduledPost, due.id).status is PostStatus.PUBLISHED
    assert db_session.get(ScheduledPost, due.id).published_at == due.scheduled_time
    assert db_session.get(ScheduledPost, future.id).status is PostStatus.SCHEDULED
