"""
AgentRun Database Model (Phase 5A).
Represents a testing execution session containing executions, metrics, and generated reports.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, PortableJSON, TimestampMixin, UTCDateTime, UUIDMixin

if TYPE_CHECKING:
    from backend.app.models.execution import TestExecution
    from backend.app.models.project import Project
    from backend.app.models.report import DiagnosticReportModel
    from backend.app.models.testcase import TestCase


class AgentRun(Base, UUIDMixin, TimestampMixin):
    """
    Testing execution session tracking lifecycle, concurrency, metrics, and diagnostic outputs.
    """
    __tablename__ = "agent_runs"

    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Parent project ID",
    )
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="pending",
        index=True,
        doc="Run state: pending, running, completed, failed, cancelled",
    )
    target_endpoints: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Optional list of target endpoint IDs or path filters selected for this run",
    )
    concurrency: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        doc="Bounded parallel execution workers (1 to 5)",
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime,
        nullable=True,
        doc="Timestamp when test execution began",
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime,
        nullable=True,
        doc="Timestamp when run terminated (completed or failed)",
    )
    duration_ms: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        doc="Total run execution duration in milliseconds",
    )
    summary: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Serialized RunSummaryStatistics snapshot from DiagnosticReport",
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Error message if run terminated in failed state",
    )

    # Relationships
    project: Mapped[Project] = relationship(
        "Project",
        back_populates="runs",
        lazy="selectin",
    )
    test_cases: Mapped[List[TestCase]] = relationship(
        "TestCase",
        back_populates="run",
        lazy="selectin",
    )
    executions: Mapped[List[TestExecution]] = relationship(
        "TestExecution",
        back_populates="run",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    report: Mapped[Optional[DiagnosticReportModel]] = relationship(
        "DiagnosticReportModel",
        back_populates="run",
        lazy="selectin",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<AgentRun id={self.id} project_id={self.project_id} status='{self.status}'>"
