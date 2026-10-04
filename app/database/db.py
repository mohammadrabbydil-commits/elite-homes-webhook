"""SQLite connection, session management, and schema initialisation."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database.models import Base

logger = logging.getLogger(__name__)

# `check_same_thread=False` lets the APScheduler worker threads share the engine.
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine: Engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Enable FK enforcement and WAL mode on SQLite (off by default)."""
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def init_db() -> None:
    """Create every table that does not already exist."""
    Base.metadata.create_all(bind=engine)
    logger.info(
        "Database initialised at %s (%d tables)",
        settings.database_url,
        len(Base.metadata.tables),
    )


def drop_db() -> None:
    """Drop every table. Destructive - POC/test use only."""
    Base.metadata.drop_all(bind=engine)
    logger.warning("All tables dropped from %s", settings.database_url)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope around a series of operations.

    Commits on success, rolls back on exception, always closes.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI-style dependency yielding a session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
