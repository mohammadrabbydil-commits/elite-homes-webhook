"""Shared pytest fixtures.

Every test runs against an isolated on-disk SQLite database and a stubbed
Graph API configuration, so nothing here touches the network or the real
elite_homes.db.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Point config at a throwaway database before app.config is imported anywhere.
import os  # noqa: E402

os.environ.setdefault("FB_APP_ID", "test-app-id")
os.environ.setdefault("FB_APP_SECRET", "test-app-secret")
os.environ.setdefault("FB_PAGE_ACCESS_TOKEN", "test-page-token")
os.environ.setdefault("FB_PAGE_ID", "123456789")


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """A Session bound to a fresh SQLite file, with app modules rebound to it."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import db as db_module
    from app.database.models import Base

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)

    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def sample_prospect(db_session):
    """One persisted OutreachProspect in the homeowner segment."""
    from app.database.models import OutreachProspect, ProspectStatus, Segment

    prospect = OutreachProspect(
        name="Dana Whitfield",
        profile_url="https://facebook.com/dana.whitfield.test",
        source_group="Jacksonville Real Estate Investors",
        keyword_match="vacant",
        segment=Segment.HOMEOWNER,
        status=ProspectStatus.PENDING,
    )
    db_session.add(prospect)
    db_session.commit()
    return prospect
