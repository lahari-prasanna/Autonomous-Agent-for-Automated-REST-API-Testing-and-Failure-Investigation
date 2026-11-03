"""
Structured schemas and failure taxonomy for deterministic failure classification (Phase 4A).
Defines serializable classification outcomes, allowlisted evidence models,
and conservative, idempotency-aware retry policies.
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class FailureCategory(str, Enum):
    """
    Deterministic failure category taxonomy.
    Covers workflow lifecycle, transport security, contract evaluation completeness,
    server defects, protocol errors, and contract schema violations.
    """
    # Layer 1: Workflow Lifecycle & Pipeline Configuration
    SPEC_OR_OPERATION_FAILURE = "spec_or_operation_failure"
    REQUEST_PREPARATION_FAILURE = "request_preparation_failure"

    # Layer 2: Transport & Security Interception
    SSRF_SECURITY_BLOCK = "ssrf_security_block"
    REQUEST_TIMEOUT = "request_timeout"
    NETWORK_FAILURE = "network_failure"
    TRANSPORT_FAILURE = "transport_failure"

    # Layer 3: Contract Evaluation Incomplete
    EVALUATION_INCOMPLETE = "evaluation_incomplete"

    # Layer 4: Server-Side Defect (5xx)
    SERVER_ERROR = "server_error"

    # Layer 5: Protocol & Unexpected Status
    UNEXPECTED_HTTP_STATUS = "unexpected_http_status"

    # Layer 6: Client / Protocol Status Failures
    AUTHENTICATION_FAILURE = "authentication_failure"
    AUTHORIZATION_FAILURE = "authorization_failure"
    REQUEST_VALIDATION_FAILURE = "request_validation_failure"
    RESOURCE_NOT_FOUND = "resource_not_found"
    RATE_LIMITING = "rate_limiting"

    # Layer 7: Response Contract Violations
    RESPONSE_CONTENT_TYPE_MISMATCH = "response_content_type_mismatch"
    RESPONSE_SCHEMA_MISMATCH = "response_schema_mismatch"

    # Layer 8: Fallback
    UNKNOWN_FAILURE = "unknown_failure"


class FailureSeverity(str, Enum):
    """Severity classification of the detected failure."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RetryPolicy(str, Enum):
    """
    Conservative retry policy.
    Distinguishes protocol-idempotent operations from unsafe non-idempotent ones,
    deterministic parameter failures, and unrecoverable policy blocks.
    """
    SAFE_TO_RETRY = "safe_to_retry"                 # Transient failure on idempotent method (GET, HEAD, OPTIONS, PUT, DELETE)
    UNSAFE_NON_IDEMPOTENT = "unsafe_non_idempotent" # Transient failure on non-idempotent method (POST, PATCH)
    REQUIRES_MODIFICATION = "requires_modification" # Deterministic failure requiring input changes (400, 422, 401, 404)
    DO_NOT_RETRY = "do_not_retry"                   # Unrecoverable error (SSRF block, bad spec, evaluation incomplete, 500 crash)


class RetryGuidance(BaseModel):
    """
    Structured retry guidance decoupled from failure classification.
    Explicitly recognizes that HTTP method idempotency is a heuristic,
    not an absolute guarantee of application-level retry safety.
    """
    model_config = ConfigDict(populate_by_name=True)

    policy: RetryPolicy = Field(..., description="High-level retry safety classification")
    is_idempotent_method: bool = Field(..., description="True if HTTP method is GET, HEAD, OPTIONS, PUT, or DELETE")
    suggested_backoff_seconds: Optional[float] = Field(
        default=None, description="Suggested delay in seconds (e.g. from Retry-After header or exponential backoff)"
    )
    reason: str = Field(..., description="Conservative architectural rationale for retry determination")


class SanitizedFailureEvidence(BaseModel):
    """
    Strict allowlist-based diagnostic evidence.
    Guarantees zero exposure of credentials, auth tokens, cookies,
    raw request/response bodies, URL query values, and raw exception messages.
    """
    model_config = ConfigDict(populate_by_name=True)

    http_method: Optional[str] = Field(default=None, description="Normalized HTTP method (e.g. 'GET', 'POST')")
    sanitized_url: Optional[str] = Field(default=None, description="URL with credentials and query values stripped")
    path_template: Optional[str] = Field(default=None, description="Normalized OpenAPI route template (e.g. '/api/products/{id}')")
    status_code: Optional[int] = Field(default=None, description="HTTP status code (0 if transport failure)")
    latency_ms: Optional[float] = Field(default=None, description="Request execution latency in milliseconds")
    matched_status_key: Optional[str] = Field(default=None, description="OpenAPI response definition matched (e.g. '200', 'default', or None)")
    expected_status_codes: List[str] = Field(default_factory=list, description="List of response status codes documented in spec")
    failed_assertion_types: List[str] = Field(default_factory=list, description="Identifiers of failed assertion checks")
    json_paths: List[str] = Field(default_factory=list, description="RFC 6901 JSON pointer paths of schema failures")
    schema_keywords: List[str] = Field(default_factory=list, description="JSON Schema keywords violated (e.g. 'required', 'minimum')")
    contract_violation_summaries: List[str] = Field(default_factory=list, description="Sanitized, rule-based contract failure descriptions")
    observed_content_type: Optional[str] = Field(default=None, description="Observed response Content-Type MIME (e.g. 'text/html')")
    expected_content_types: List[str] = Field(default_factory=list, description="Documented response Content-Type MIMEs (e.g. ['application/json'])")
    error_type: Optional[str] = Field(default=None, description="Exception or error class name (no raw message/traceback)")
    lifecycle_step: Optional[str] = Field(default=None, description="Pipeline step where execution halted")


class FailureClassification(BaseModel):
    """
    Structured outcome of deterministic failure classification.
    Cleanly serializable for logging, diagnostic reporting, and downstream agent consumption.
    """
    model_config = ConfigDict(populate_by_name=True)

    has_failure: bool = Field(..., description="True if a test or pipeline failure occurred")
    primary_category: Optional[FailureCategory] = Field(
        default=None, description="Primary root-cause failure category"
    )
    secondary_categories: List[FailureCategory] = Field(
        default_factory=list, description="Associated secondary failure categories"
    )
    severity: FailureSeverity = Field(
        default=FailureSeverity.LOW, description="Severity assessment of the failure"
    )
    retry_guidance: RetryGuidance = Field(
        ..., description="Idempotency-aware structured retry policy"
    )
    summary: str = Field(..., description="Concise, safe, human- and LLM-readable summary")
    evidence: SanitizedFailureEvidence = Field(
        default_factory=SanitizedFailureEvidence, description="Allowlist-extracted diagnostic facts"
    )

