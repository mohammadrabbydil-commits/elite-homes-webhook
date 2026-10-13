"""SQLite connection, session management, and schema initialisation."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import enum

from sqlalchemy import Column, create_engine, event, inspect, text
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


def _sql_default(column: Column) -> str | None:
    """A model column's scalar default as a SQL literal, or None if it has none.

    Converted through the column's own type first, because that's the form
    SQLAlchemy reads back - an Enum column stores the member *name*
    ("AWAITING_ADDRESS"), not its value.
    """
    default = column.default
    if default is None or not default.is_scalar:
        return None
    processor = column.type.bind_processor(engine.dialect)
    value = processor(default.arg) if processor else default.arg
    if isinstance(value, enum.Enum):
        value = value.name
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _add_missing_columns() -> None:
    """Add model columns that an existing table doesn't have yet.

    `create_all()` never alters a table that already exists, so a column added
    to a model later is missing from any database created before it, and every
    query on that model then fails with "no such column". Nothing is dropped
    or rewritten - columns are only added.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" ' + str(
                    column.type.compile(dialect=engine.dialect)
                )
                default = _sql_default(column)
                if default is not None:
                    # NOT NULL is only allowed on an added column that has a default.
                    ddl += f" DEFAULT {default}" + ("" if column.nullable else " NOT NULL")
                conn.execute(text(ddl))
                logger.info("Added missing column %s.%s", table.name, column.name)


def init_db() -> None:
    """Create every table that does not already exist, and add any columns
    an existing table is missing."""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
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
