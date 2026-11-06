"""
TestCase Database Model (Phase 5A).
Represents a generated test case definition with structured input requests and expected assertions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, PortableJSON, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.app.models.endpoint import Endpoint
    from backend.app.models.execution import TestExecution
    from backend.app.models.run import AgentRun


class TestCase(Base, UUIDMixin, TimestampMixin):
    """
    Generated test case input parameters, expected HTTP responses, and assertion rules.
    """
    __tablename__ = "test_cases"
    __test__ = False  # Prevent pytest from collecting this model as a test suite

    endpoint_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Target endpoint operation ID",
    )
    run_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Optional run association where this test case was generated",
    )
    test_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="standard",
        index=True,
        doc="Test strategy category: standard, boundary, error_case, variation",
    )
    request_spec: Mapped[Any] = mapped_column(
        PortableJSON,
        nullable=False,
        doc="Structured ExecutionRequest dictionary (method, url, query params, headers, payload)",
    )
    expected_status: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        doc="Expected HTTP response status code",
    )
    assertions: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Expected assertion rules and validation criteria",
    )

    # Relationships
    endpoint: Mapped[Endpoint] = relationship(
        "Endpoint",
        back_populates="test_cases",
        lazy="selectin",
    )
    run: Mapped[Optional[AgentRun]] = relationship(
        "AgentRun",
        back_populates="test_cases",
        lazy="selectin",
    )
    executions: Mapped[List[TestExecution]] = relationship(
        "TestExecution",
        back_populates="test_case",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<TestCase id={self.id} endpoint_id={self.endpoint_id} test_type='{self.test_type}'>"
