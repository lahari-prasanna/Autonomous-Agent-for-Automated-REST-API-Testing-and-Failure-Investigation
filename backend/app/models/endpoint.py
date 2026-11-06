"""
Endpoint Database Model (Phase 5A).
Represents a normalized API operation (path, method, schemas) belonging to a Project.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, PortableJSON, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.app.models.execution import TestExecution
    from backend.app.models.project import Project
    from backend.app.models.testcase import TestCase


class Endpoint(Base, UUIDMixin, TimestampMixin):
    """
    Normalized OpenAPI endpoint operation extracted from the specification.
    """
    __tablename__ = "endpoints"

    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Parent project ID",
    )
    method: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        index=True,
        doc="Normalized uppercase HTTP method (e.g. GET, POST, PUT, DELETE)",
    )
    path: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
        index=True,
        doc="Normalized OpenAPI route template (e.g. /api/products/{id})",
    )
    operation_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        doc="Unique operation identifier from specification if defined",
    )
    summary: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Brief operation summary",
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Full operation description",
    )
    parameters: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Serialized list of ParameterDefinition dictionaries",
    )
    request_body: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Serialized RequestBodyDefinition dictionary",
    )
    responses: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Serialized dictionary of ResponseDefinition schemas mapped by status code",
    )

    # Relationships
    project: Mapped[Project] = relationship(
        "Project",
        back_populates="endpoints",
        lazy="selectin",
    )
    test_cases: Mapped[List[TestCase]] = relationship(
        "TestCase",
        back_populates="endpoint",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    executions: Mapped[List[TestExecution]] = relationship(
        "TestExecution",
        back_populates="endpoint",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_endpoints_project_method_path", "project_id", "method", "path"),
    )

    def __repr__(self) -> str:
        return f"<Endpoint id={self.id} method='{self.method}' path='{self.path}'>"
