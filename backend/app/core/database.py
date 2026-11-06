"""
Database Engine and Session Management (Phase 5A).
Supports asynchronous SQLite (aiosqlite) and PostgreSQL (asyncpg) via SQLAlchemy 2.0.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator, Optional

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.app.core.config import settings

logger = logging.getLogger(__name__)


def build_engine(database_url: Optional[str] = None, echo: bool = False) -> AsyncEngine:
    """
    Constructs an async SQLAlchemy engine with proper connection pooling and dialect settings.
    Enforces SQLite foreign keys and disables thread checking for aiosqlite.
    """
    url = database_url or settings.DATABASE_URL
    is_sqlite = "sqlite" in url.lower()

    connect_args = {}
    engine_kwargs = {"echo": echo, "future": True}

    if is_sqlite:
        connect_args["check_same_thread"] = False
        engine_kwargs["connect_args"] = connect_args
    else:
        # PostgreSQL pool parameters
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_size"] = 10
        engine_kwargs["max_overflow"] = 20

    eng = create_async_engine(url, **engine_kwargs)

    if is_sqlite:
        @event.listens_for(eng.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return eng


# Global application engine and session factory
engine: AsyncEngine = build_engine()
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency yielding a short-lived AsyncSession.
    Enforces explicit transaction boundaries: commits on success, rolls back on exception.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db(target_engine: Optional[AsyncEngine] = None) -> None:
    """Creates all database tables defined in metadata."""
    from backend.app.models.base import Base

    eng = target_engine or engine
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized successfully.")


async def drop_db(target_engine: Optional[AsyncEngine] = None) -> None:
    """Drops all database tables defined in metadata (primarily for tests)."""
    from backend.app.models.base import Base

    eng = target_engine or engine
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    logger.info("Database tables dropped successfully.")


async def close_db(target_engine: Optional[AsyncEngine] = None) -> None:
    """Disposes engine connection pool cleanly."""
    eng = target_engine or engine
    await eng.dispose()
    logger.info("Database connection closed.")
