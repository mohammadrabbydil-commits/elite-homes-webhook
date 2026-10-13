"""Tests for the SQLAlchemy models and database initialisation."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect, select

from app.database.models import (
    ActionResult,
    ActionType,
    OutreachLog,
    OutreachProspect,
    PostAnalytics,
    PostStatus,
    PostType,
    ProspectStatus,
    ScheduledPost,
    Segment,
    utcnow,
)


def test_all_tables_are_created(db_session):
    tables = set(inspect(db_session.get_bind()).get_table_names())
    assert tables == {
        "scheduled_posts",
        "outreach_prospects",
        "outreach_logs",
        "post_analytics",
        "conversations",
        "messages",
    }


def test_scheduled_post_defaults(db_session):
    post = ScheduledPost(
        content="Thinking about selling your Jacksonville property?",
        post_type=PostType.HOMEOWNER,
        scheduled_time=utcnow() + timedelta(hours=1),
    )
    db_session.add(post)
    db_session.commit()

    stored = db_session.scalar(select(ScheduledPost))
    assert stored.status is PostStatus.PENDING
    assert stored.retry_count == 0
    assert stored.media_url is None
    assert stored.fb_post_id is None
    assert stored.published_at is None
    assert isinstance(stored.created_at, datetime)


def test_scheduled_post_enums_round_trip(db_session):
    for post_type in PostType:
        db_session.add(
            ScheduledPost(
                content=f"Post for {post_type.value}",
                post_type=post_type,
                scheduled_time=utcnow(),
            )
        )
    db_session.commit()

    stored = db_session.scalars(select(ScheduledPost)).all()
    assert {p.post_type for p in stored} == set(PostType)


def test_prospect_profile_url_is_unique(db_session, sample_prospect):
    duplicate = OutreachProspect(
        name="Someone Else",
        profile_url=sample_prospect.profile_url,
        segment=Segment.BUYER,
    )
    db_session.add(duplicate)

    with pytest.raises(Exception):
        db_session.commit()
    db_session.rollback()


def test_outreach_log_relationship(db_session, sample_prospect):
    db_session.add(
        OutreachLog(
            prospect_id=sample_prospect.id,
            action_type=ActionType.MESSAGE,
            result=ActionResult.SENT,
            details="dry run",
        )
    )
    db_session.commit()

    prospect = db_session.get(OutreachProspect, sample_prospect.id)
    assert len(prospect.logs) == 1
    assert prospect.logs[0].action_type is ActionType.MESSAGE
    assert prospect.logs[0].prospect is prospect


def test_post_analytics_relationship_and_cascade(db_session):
    post = ScheduledPost(
        content="Published post",
        post_type=PostType.BUYER,
        scheduled_time=utcnow(),
        status=PostStatus.PUBLISHED,
        fb_post_id="123_456",
        published_at=utcnow(),
    )
    db_session.add(post)
    db_session.commit()

    db_session.add(PostAnalytics(post_id=post.id, reach=500, engagement=42, clicks=7))
    db_session.commit()

    assert len(post.analytics) == 1
    assert post.analytics[0].reach == 500

    # Deleting the post removes its analytics via cascade.
    db_session.delete(post)
    db_session.commit()
    assert db_session.scalars(select(PostAnalytics)).all() == []


def test_prospect_status_transitions(db_session, sample_prospect):
    assert sample_prospect.status is ProspectStatus.PENDING

    sample_prospect.status = ProspectStatus.CONTACTED
    sample_prospect.contacted_at = utcnow()
    sample_prospect.message_sent = "Hi Dana - I work with Elite Homes USA Group..."
    db_session.commit()

    stored = db_session.get(OutreachProspect, sample_prospect.id)
    assert stored.status is ProspectStatus.CONTACTED
    assert stored.message_sent.startswith("Hi Dana")


def test_init_db_is_idempotent(tmp_path, monkeypatch):
    """Calling init_db twice must not error."""
    from sqlalchemy import create_engine

    from app.database import db as db_module
    from app.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'idempotent.db'}", future=True)
    monkeypatch.setattr(db_module, "engine", engine)

    db_module.init_db()
    db_module.init_db()

    assert "scheduled_posts" in inspect(engine).get_table_names()
    assert len(Base.metadata.tables) == 6
    engine.dispose()


def test_session_scope_rolls_back_on_error(db_session, monkeypatch):
    from app.database import db as db_module

    with pytest.raises(ValueError):
        with db_module.session_scope() as session:
            session.add(
                ScheduledPost(
                    content="Should be rolled back",
                    post_type=PostType.PARTNER,
                    scheduled_time=utcnow(),
                )
            )
            raise ValueError("boom")

    with db_module.session_scope() as session:
        assert session.scalars(select(ScheduledPost)).all() == []


def test_init_db_adds_new_columns_to_an_older_database(tmp_path, monkeypatch):
    """A database created before a column existed gets it added in place,
    with existing rows kept - instead of every query failing with
    "no such column"."""
    from sqlalchemy import create_engine, text

    from app.database import db as db_module
    from app.database.models import Conversation, ConversationStatus, FlowStage

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", future=True)
    with engine.begin() as conn:
        # The conversations table as it looked before the lead and flow columns.
        conn.execute(text(
            "CREATE TABLE conversations ("
            " id INTEGER PRIMARY KEY, psid VARCHAR(64) NOT NULL UNIQUE, name VARCHAR(255),"
            " intent VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL,"
            " property_address VARCHAR(512), auto_replies_sent INTEGER NOT NULL,"
            " first_message_at DATETIME NOT NULL, last_message_at DATETIME NOT NULL,"
            " handoff_at DATETIME, created_at DATETIME NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO conversations (psid, intent, status, auto_replies_sent,"
            " first_message_at, last_message_at, created_at)"
            " VALUES ('old_psid', 'SELL', 'AWAITING_HUMAN', 1,"
            " '2026-10-01 10:00:00', '2026-10-01 10:00:00', '2026-10-01 10:00:00')"
        ))
    monkeypatch.setattr(db_module, "engine", engine)

    db_module.init_db()

    columns = {col["name"] for col in inspect(engine).get_columns("conversations")}
    for name in ("stage", "zip_code", "county", "contact_preference", "lead_submitted_at", "opted_out"):
        assert name in columns

    from sqlalchemy.orm import Session

    with Session(engine) as session:
        convo = session.scalar(select(Conversation).where(Conversation.psid == "old_psid"))
        assert convo.status is ConversationStatus.AWAITING_HUMAN
        assert convo.stage is FlowStage.AWAITING_ADDRESS
        assert convo.opted_out is False
        assert convo.zip_code is None
    engine.dispose()


def test_init_db_is_safe_to_run_twice(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from app.database import db as db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'twice.db'}", future=True)
    monkeypatch.setattr(db_module, "engine", engine)

    db_module.init_db()
    db_module.init_db()

    assert "opted_out" in {col["name"] for col in inspect(engine).get_columns("conversations")}
    engine.dispose()
