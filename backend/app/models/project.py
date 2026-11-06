"""
Project Database Model (Phase 5A).
Represents a tested API target with its OpenAPI specification, base URL, and configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.app.models.endpoint import Endpoint
    from backend.app.models.run import AgentRun


class Project(Base, UUIDMixin, TimestampMixin):
    """
    Project model storing API definitions, target servers, and historical test sessions.
    """
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        doc="Human-readable project name",
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Detailed project description or scope notes",
    )
    base_url: Mapped[Optional[str]] = mapped_column(
        String(1024),
        nullable=True,
        doc="Default target base URL for API test executions",
    )
    spec_raw: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Original OpenAPI specification text (JSON or YAML)",
    )
    spec_title: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc="API title extracted from parsed OpenAPI spec",
    )
    spec_version: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        doc="API version string extracted from parsed OpenAPI spec",
    )

    # Relationships
    endpoints: Mapped[List[Endpoint]] = relationship(
        "Endpoint",
        back_populates="project",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
        doc="Normalized API endpoint operations belonging to this project",
    )
    runs: Mapped[List[AgentRun]] = relationship(
        "AgentRun",
        back_populates="project",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
        doc="Historical agent execution runs executed on this project",
    )

    def __repr__(self) -> str:
        return f"<Project id={self.id} name='{self.name}' spec_title='{self.spec_title}'>"
