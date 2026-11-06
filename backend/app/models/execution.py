"""
TestExecution Database Model (Phase 5A).
Records an executed HTTP request attempt, transport status, latency, and assertion outcomes.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, PortableJSON, TimestampMixin, UTCDateTime, UUIDMixin, utc_now

if TYPE_CHECKING:
    from backend.app.models.endpoint import Endpoint
    from backend.app.models.investigation import FailureInvestigationModel
    from backend.app.models.run import AgentRun
    from backend.app.models.testcase import TestCase


class TestExecution(Base, UUIDMixin, TimestampMixin):
    """
    HTTP exchange record capturing transport metrics, assertion checks, and failure facts.
    """
    __tablename__ = "test_executions"
    __test__ = False  # Prevent pytest from collecting this model as a test suite

    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Parent run session ID",
    )
    endpoint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Target endpoint operation ID",
    )
    test_case_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("test_cases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Optional source TestCase ID",
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        doc="Attempt index (1 for initial, 2+ for retries or variations)",
    )
    execution_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="initial",
        index=True,
        doc="Execution classification: initial, transient_retry, variation_test",
    )
    dispatched: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        doc="True if socket transmission occurred; False if blocked by SSRF or pre-dispatch error",
    )
    status_code: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc="HTTP response status code (None if transport timeout or connection error)",
    )
    latency_ms: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        doc="Round-trip request latency in milliseconds",
    )
    passed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        doc="True if execution satisfied all contract assertions",
    )
    request_snapshot: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Sanitized request details (masked credentials, safe headers, parameters)",
    )
    response_snapshot: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Sanitized response details (status, safe headers, body summary)",
    )
    assertion_results: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="List of evaluated assertion outcomes from AssertionReport",
    )
    failure_classification: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Sanitized failure diagnostic facts from FailureClassification",
    )
    executed_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utc_now,
        nullable=False,
        doc="Timestamp when request exchange occurred",
    )

    # Relationships
    run: Mapped[AgentRun] = relationship(
        "AgentRun",
        back_populates="executions",
        lazy="selectin",
    )
    endpoint: Mapped[Endpoint] = relationship(
        "Endpoint",
        back_populates="executions",
        lazy="selectin",
    )
    test_case: Mapped[Optional[TestCase]] = relationship(
        "TestCase",
        back_populates="executions",
        lazy="selectin",
    )
    investigations: Mapped[List[FailureInvestigationModel]] = relationship(
        "FailureInvestigationModel",
        back_populates="execution",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<TestExecution id={self.id} run_id={self.run_id} type='{self.execution_type}' status={self.status_code} passed={self.passed}>"
