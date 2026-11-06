"""
FailureInvestigation Database Model (Phase 5A).
Stores failure investigations, evidence chunks, and actionable remediation steps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.models.base import Base, PortableJSON, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from backend.app.models.execution import TestExecution
    from backend.app.models.run import AgentRun


class FailureInvestigationModel(Base, UUIDMixin, TimestampMixin):
    """
    RAG-contextualized diagnostic investigation facts, retrieved standard chunks, and remediation hypotheses.
    """
    __tablename__ = "failure_investigations"

    execution_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("test_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Failed test execution ID investigated",
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Parent agent run ID",
    )
    failure_category: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        index=True,
        doc="Primary failure classification category (e.g. server_error, request_validation_failure)",
    )
    hypotheses: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Serialized list of RootCauseHypothesis dictionaries with likelihood and evidence",
    )
    retrieved_evidence: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Serialized list of DetachedRetrievedEvidenceItem chunks",
    )
    recommendations: Mapped[Optional[Any]] = mapped_column(
        PortableJSON,
        nullable=True,
        doc="Serialized list of InvestigationRecommendation dictionaries",
    )
    investigation_degraded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        doc="True if investigation operated in fallback degraded mode due to knowledge store unavailability",
    )

    # Relationships
    execution: Mapped[TestExecution] = relationship(
        "TestExecution",
        back_populates="investigations",
        lazy="selectin",
    )
    run: Mapped[AgentRun] = relationship(
        "AgentRun",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<FailureInvestigationModel id={self.id} execution_id={self.execution_id} category='{self.failure_category}'>"
