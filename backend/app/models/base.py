"""
SQLAlchemy 2.0 Declarative Base and Shared Column Types (Phase 5A).
Provides dialect-agnostic JSONB/JSON mapping, timezone-aware UTC datetimes, UUID primary keys, and UTC timestamps.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from sqlalchemy import DateTime, JSON, String, TypeDecorator
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Portable JSON: compiles to JSONB on PostgreSQL and JSON on SQLite
PortableJSON = JSON().with_variant(postgresql.JSONB, "postgresql")


def utc_now() -> datetime:
    """Returns current datetime in UTC timezone."""
    return datetime.now(timezone.utc)


def generate_uuid() -> str:
    """Generates a canonical 36-character UUID string."""
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator):
    """
    Timezone-aware UTC DateTime type.
    Ensures datetimes are consistently stored and returned with UTC timezone across SQLite and PostgreSQL.
    """
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is not None:
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                else:
                    value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is not None:
            if isinstance(value, datetime) and value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    """Base declarative class for all SQLAlchemy 2.0 database models."""
    pass


class UUIDMixin:
    """Provides a standardized 36-character UUID string primary key."""
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=generate_uuid,
        sort_order=-10,
    )


class TimestampMixin:
    """Provides timezone-aware UTC created_at and updated_at audit timestamps."""
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
