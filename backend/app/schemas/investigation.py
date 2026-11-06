"""
Structured schemas for evidence-supported failure investigations (Phase 4B.5).
Defines serializable models for retrieved RAG documentation evidence,
strictly allowlisted observed execution facts, and aggregated failure investigations.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from backend.app.schemas.failure import FailureClassification


class RetrievedEvidenceItem(BaseModel):
    """
    Sanitized knowledge chunk retrieved from RAG knowledge store (Phase 4B.5).
    Exposes provenance, relevance scores, and content strictly marked as untrusted evidence.
    """
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    chunk_id: str = Field(..., description="Unique chunk identifier")
    document_id: str = Field(..., description="Parent document identifier")
    source_type: str = Field(..., description="Knowledge source type")
    source_ref: Optional[str] = Field(default=None, description="Safe provenance reference or URI fragment")
    relevance_score: float = Field(..., ge=0.0, le=1.0, description="Normalized similarity relevance score [0.0, 1.0]")
    distance: float = Field(..., ge=0.0, description="Raw cosine vector distance")
    content: str = Field(..., description="Untrusted documentation evidence content")
    http_method: Optional[str] = Field(default=None, description="Associated HTTP method if operation-scoped")
    path_template: Optional[str] = Field(default=None, description="Associated path template if operation-scoped")
    operation_id: Optional[str] = Field(default=None, description="Associated operation ID if operation-scoped")
    is_untrusted_evidence: bool = Field(
        default=True,
        description="Explicit flag enforcing that content must be treated as passive, untrusted reference evidence, never instructions.",
    )


class ObservedExecutionFacts(BaseModel):
    """
    Deterministic execution facts observed directly from the test run (Phase 4B.5).
    Guarantees zero exposure of credentials, auth tokens, cookies, or raw request/response bodies.
    """
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    has_failure: bool = Field(..., description="Whether a failure was detected")
    http_method: Optional[str] = Field(default=None, description="HTTP method executed or targeted")
    path_template: Optional[str] = Field(default=None, description="Normalized route path template")
    operation_id: Optional[str] = Field(default=None, description="Targeted OpenAPI operation ID")
    status_code: Optional[int] = Field(default=None, description="HTTP status code observed (0 if transport failure)")
    sanitized_url: Optional[str] = Field(default=None, description="Target URL with credentials and query parameters stripped")
    lifecycle_step: Optional[str] = Field(default=None, description="Workflow lifecycle step where failure occurred")
    error_type: Optional[str] = Field(default=None, description="Exception or error type name (sanitized)")
    failed_assertion_types: List[str] = Field(default_factory=list, description="Assertion check types that failed")
    schema_keywords: List[str] = Field(default_factory=list, description="JSON Schema keywords violated")
    json_paths: List[str] = Field(default_factory=list, description="RFC 6901 JSON pointer paths of schema failures")
    contract_violation_summaries: List[str] = Field(default_factory=list, description="Sanitized contract violation summaries")


class FailureInvestigation(BaseModel):
    """
    Evidence-supported failure investigation outcome (Phase 4B.5).
    Combines deterministic Phase 4A failure classification, observed execution facts,
    retrieved RAG documentation evidence, and rule-based explanations/inferences.
    """
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    classification: FailureClassification = Field(
        ..., description="Deterministic Phase 4A failure classification (unmodified)"
    )
    observed_facts: ObservedExecutionFacts = Field(
        ..., description="Execution facts observed directly from the test run"
    )
    retrieved_evidence: List[RetrievedEvidenceItem] = Field(
        default_factory=list, description="Contextual knowledge chunks retrieved from knowledge store"
    )
    inferences: List[str] = Field(
        default_factory=list, description="Rule-based explanations and inferences derived from facts and evidence"
    )
    source_references: List[str] = Field(
        default_factory=list, description="Deduplicated list of source references from retrieved evidence"
    )

