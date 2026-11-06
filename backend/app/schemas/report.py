"""
Validated, deeply immutable Pydantic schemas for Phase 4C Diagnostic Reporting.
"""

from enum import Enum
import math
import re
from typing import Any, Literal, Optional, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.schemas.failure import FailureCategory, FailureSeverity, RetryPolicy
from backend.app.schemas.retest import ParameterPerturbation, PerturbationStrategy, PolicyAuthorization, RetestAction
from backend.app.schemas.sanitizer import ReportDataSanitizer


HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
VALID_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "TRACE"}


class OverallRunStatus(str, Enum):
    PASSED = "passed"
    RESOLVED_AFTER_TRANSIENT_RETRY = "resolved_after_transient_retry"
    FAILED = "failed"
    ERROR = "error"


class AttemptOutcomeSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatched: bool = Field(..., description="True only if HTTP exchange was sent across the network")
    uncertain_delivery: bool = Field(default=False)
    status_code: Optional[int] = Field(default=None)  # None if no HTTP response received
    latency_ms: Optional[float] = Field(default=None)
    passed: bool = Field(...)
    failure_category: Optional[FailureCategory] = Field(default=None)
    failed_assertions: Tuple[str, ...] = Field(default_factory=tuple)
    error_type: Optional[str] = Field(default=None)

    @field_validator("failed_assertions", mode="before")
    @classmethod
    def _clean_assertions(cls, v: Any) -> Tuple[str, ...]:
        if not isinstance(v, (list, tuple)):
            raise TypeError("failed_assertions must be a sequence")
        return tuple(ReportDataSanitizer.sanitize_text(s) for s in v)

    @field_validator("error_type", mode="before")
    @classmethod
    def _clean_error(cls, v: Optional[str]) -> Optional[str]:
        return ReportDataSanitizer.sanitize_text(v) if v else None

    @field_validator("latency_ms", mode="before")
    @classmethod
    def _clean_latency(cls, v: Optional[float]) -> Optional[float]:
        if v is not None:
            return ReportDataSanitizer.ensure_finite_non_negative(v, "latency_ms")
        return None


class FollowupDecisionAuditRecord(BaseModel):
    """Unified follow-up decision ledger item."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    followup_number: int = Field(..., ge=1, le=2)
    action: RetestAction = Field(...)
    retry_of: Optional[str] = Field(default=None)  # "initial" or candidate_id
    target_candidate_id: Optional[str] = Field(default=None)
    eligibility_policy: RetryPolicy = Field(...)
    reason_category: Literal[
        "not_eligible",
        "skipped_due_to_retry_after",
        "skipped_due_to_deadline",
        "budget_exhausted",
        "authorization_denied",
        "schema_invalid",
        "dispatched_transient_retry",
        "dispatched_variation_test",
        "dispatch_failed",
    ] = Field(...)
    sanitized_reason: str = Field(...)
    requested_delay_seconds: Optional[float] = Field(default=None)
    remaining_deadline_seconds: Optional[float] = Field(default=None)
    dispatched: bool = Field(...)

    @field_validator("target_candidate_id", "retry_of", "sanitized_reason", mode="before")
    @classmethod
    def _clean_strings(cls, v: Optional[str]) -> Optional[str]:
        return ReportDataSanitizer.sanitize_text(v) if v else None

    @field_validator("requested_delay_seconds", "remaining_deadline_seconds", mode="before")
    @classmethod
    def _clean_sec(cls, v: Optional[float]) -> Optional[float]:
        if v is not None:
            return ReportDataSanitizer.ensure_finite_non_negative(v, "delay_or_deadline")
        return None


class TransientRetryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_number: int = Field(..., ge=1, le=2)
    retry_of: str = Field(...)  # "initial" or candidate_id
    backoff_delay_seconds: float = Field(..., ge=0.0, le=5.0)
    canonical_request_fingerprint: str = Field(...)
    outcome: AttemptOutcomeSummary = Field(...)

    @field_validator("retry_of", mode="before")
    @classmethod
    def _clean_rof(cls, v: str) -> str:
        return ReportDataSanitizer.sanitize_text(v)

    @field_validator("canonical_request_fingerprint")
    @classmethod
    def _validate_fp(cls, v: str) -> str:
        if not HEX_64_PATTERN.match(v):
            raise ValueError(f"Fingerprint must be exactly 64 hex characters (got '{v}')")
        return v

    @field_validator("backoff_delay_seconds", mode="before")
    @classmethod
    def _clean_delay(cls, v: float) -> float:
        return ReportDataSanitizer.ensure_finite_non_negative(v, "backoff_delay_seconds")


class VariationCandidateRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(...)
    strategy: PerturbationStrategy = Field(...)
    target_parameter: str = Field(...)
    parameter_location: Literal["path", "query", "header", "body"] = Field(...)
    applicable_constraint: str = Field(...)
    canonical_request_fingerprint: Optional[str] = Field(default=None)
    schema_validation_passed: bool = Field(...)
    authorization_approved: bool = Field(...)
    budget_approved: bool = Field(...)
    approved_for_dispatch: bool = Field(...)
    dispatched: bool = Field(...)
    rejection_reason: Optional[str] = Field(default=None)
    dispatch_error: Optional[str] = Field(default=None)

    @field_validator("candidate_id", "target_parameter", "applicable_constraint", "rejection_reason", "dispatch_error", mode="before")
    @classmethod
    def _clean_candidate_strings(cls, v: Optional[str]) -> Optional[str]:
        return ReportDataSanitizer.sanitize_text(v) if v else None

    @field_validator("canonical_request_fingerprint")
    @classmethod
    def _val_fp(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not HEX_64_PATTERN.match(v):
            raise ValueError("Fingerprint must be 64 hex characters")
        return v

    @model_validator(mode="after")
    def verify_candidate_gates(self) -> "VariationCandidateRecord":
        computed_approval = self.schema_validation_passed and self.authorization_approved and self.budget_approved
        if self.approved_for_dispatch != computed_approval:
            raise ValueError(
                f"Candidate '{self.candidate_id}' approved_for_dispatch={self.approved_for_dispatch} does not match gates"
            )
        if not self.approved_for_dispatch and not self.rejection_reason:
            raise ValueError(f"Rejected candidate '{self.candidate_id}' must provide a rejection_reason")
        if self.approved_for_dispatch and self.rejection_reason is not None:
            raise ValueError(f"Approved candidate '{self.candidate_id}' cannot have a rejection_reason")
        if self.dispatched and not self.approved_for_dispatch:
            raise ValueError(f"Candidate '{self.candidate_id}' dispatched without approval")
        if self.approved_for_dispatch and not self.dispatched and not self.dispatch_error:
            raise ValueError(f"Candidate '{self.candidate_id}' approved but undispatched must provide dispatch_error")
        if self.dispatched and self.dispatch_error is not None:
            raise ValueError(f"Dispatched candidate '{self.candidate_id}' cannot have dispatch_error")
        return self


class VariationTestRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    variation_number: int = Field(..., ge=1, le=2)
    candidate_id: str = Field(...)
    canonical_request_fingerprint: str = Field(...)
    perturbations_applied: Tuple[ParameterPerturbation, ...] = Field(...)
    is_non_idempotent_mutation: bool = Field(default=False)
    authorization: Optional[PolicyAuthorization] = Field(default=None)
    outcome: AttemptOutcomeSummary = Field(...)

    @field_validator("candidate_id", mode="before")
    @classmethod
    def _clean_cid(cls, v: str) -> str:
        return ReportDataSanitizer.sanitize_text(v)

    @field_validator("canonical_request_fingerprint")
    @classmethod
    def _val_vfp(cls, v: str) -> str:
        if not HEX_64_PATTERN.match(v):
            raise ValueError("Fingerprint must be 64 hex characters")
        return v

    @field_validator("perturbations_applied", mode="before")
    @classmethod
    def _clean_perts(cls, v: Any) -> Tuple[ParameterPerturbation, ...]:
        if not isinstance(v, (list, tuple)):
            raise TypeError("perturbations_applied must be a sequence")
        return tuple(v)

    @model_validator(mode="after")
    def verify_test_integrity(self) -> "VariationTestRecord":
        if not all(p.pre_execution_validated for p in self.perturbations_applied):
            raise ValueError(f"Variation test '{self.candidate_id}' contains unvalidated perturbations")
        if self.is_non_idempotent_mutation:
            if self.authorization is None or not self.authorization.is_authorized:
                raise ValueError(
                    f"Variation test '{self.candidate_id}' non-idempotent mutation lacks valid PolicyAuthorization"
                )
        if not self.outcome.dispatched:
            raise ValueError(f"Variation test '{self.candidate_id}' recorded with outcome.dispatched=False")
        return self


class RemediationRecommendation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    priority: FailureSeverity = Field(...)
    target: Literal["server_implementation", "client_request", "openapi_spec"] = Field(...)
    finding: str = Field(...)
    action: str = Field(...)
    reference_urls: Tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("finding", "action", mode="before")
    @classmethod
    def _clean_rec_text(cls, v: str) -> str:
        return ReportDataSanitizer.sanitize_text(v)

    @field_validator("reference_urls", mode="before")
    @classmethod
    def _clean_urls(cls, v: Any) -> Tuple[str, ...]:
        if not isinstance(v, (list, tuple)):
            raise TypeError("reference_urls must be a sequence")
        valid = [ReportDataSanitizer.sanitize_source_reference(u) for u in v]
        return tuple(u for u in valid if u)


class DetachedRetrievedEvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(...)
    document_id: str = Field(...)
    source_type: str = Field(...)
    source_ref: Optional[str] = Field(default=None)
    relevance_score: float = Field(..., ge=0.0, le=1.0)
    distance: float = Field(..., ge=0.0)
    content: str = Field(...)
    http_method: Optional[str] = Field(default=None)
    path_template: Optional[str] = Field(default=None)
    operation_id: Optional[str] = Field(default=None)
    is_untrusted_evidence: Literal[True] = Field(default=True, frozen=True)

    @field_validator("is_untrusted_evidence", mode="before")
    @classmethod
    def _force_untrusted(cls, v: Any) -> Literal[True]:
        return True

    @field_validator("source_ref", mode="before")
    @classmethod
    def _clean_sref(cls, v: Optional[str]) -> Optional[str]:
        return ReportDataSanitizer.sanitize_source_reference(v)

    @field_validator("http_method", mode="before")
    @classmethod
    def _clean_method(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        m = str(v).strip().upper()
        if m not in VALID_HTTP_METHODS:
            raise ValueError(f"Invalid HTTP method in evidence: '{v}'")
        return m

    @field_validator("chunk_id", "document_id", "source_type", "content", "path_template", "operation_id", mode="before")
    @classmethod
    def _clean_evidence_strings(cls, v: Optional[str]) -> Optional[str]:
        return ReportDataSanitizer.sanitize_text(v) if v else None

    @field_validator("relevance_score", "distance", mode="before")
    @classmethod
    def _clean_evidence_floats(cls, v: float, info: Any) -> float:
        return ReportDataSanitizer.ensure_finite_non_negative(v, info.field_name or "score")


class DetachedInvestigationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    investigation_degraded: bool = Field(default=False)
    investigation_error_type: Optional[str] = Field(default=None)
    inferences: Tuple[str, ...] = Field(default_factory=tuple)
    source_references: Tuple[str, ...] = Field(default_factory=tuple)
    retrieved_evidence: Tuple[DetachedRetrievedEvidenceItem, ...] = Field(default_factory=tuple)

    @field_validator("investigation_error_type", mode="before")
    @classmethod
    def _clean_iet(cls, v: Optional[str]) -> Optional[str]:
        return ReportDataSanitizer.sanitize_text(v) if v else None

    @field_validator("inferences", mode="before")
    @classmethod
    def _clean_inferences(cls, v: Any) -> Tuple[str, ...]:
        if not isinstance(v, (list, tuple)):
            raise TypeError("inferences must be a sequence")
        return tuple(ReportDataSanitizer.sanitize_text(s) for s in v)

    @field_validator("source_references", mode="before")
    @classmethod
    def _clean_source_refs(cls, v: Any) -> Tuple[str, ...]:
        if not isinstance(v, (list, tuple)):
            raise TypeError("source_references must be a sequence")
        valid = [ReportDataSanitizer.sanitize_source_reference(s) for s in v]
        return tuple(u for u in valid if u)

    @field_validator("retrieved_evidence", mode="before")
    @classmethod
    def _clean_evidence(cls, v: Any) -> Tuple[DetachedRetrievedEvidenceItem, ...]:
        if not isinstance(v, (list, tuple)):
            raise TypeError("retrieved_evidence must be a sequence")
        return tuple(v)


class OperationRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: Optional[str] = Field(default=None)
    http_method: str = Field(...)
    path_template: str = Field(...)
    initial_request_fingerprint: str = Field(...)
    initial_attempt: AttemptOutcomeSummary = Field(...)
    followup_decisions: Tuple[FollowupDecisionAuditRecord, ...] = Field(default_factory=tuple)
    transient_retries: Tuple[TransientRetryRecord, ...] = Field(default_factory=tuple)
    variation_candidates: Tuple[VariationCandidateRecord, ...] = Field(default_factory=tuple)
    variation_tests: Tuple[VariationTestRecord, ...] = Field(default_factory=tuple)
    final_status: OverallRunStatus = Field(...)
    final_outcome: AttemptOutcomeSummary = Field(...)
    investigation: Optional[DetachedInvestigationRecord] = Field(default=None)
    remediation_recommendations: Tuple[RemediationRecommendation, ...] = Field(default_factory=tuple)

    @field_validator("http_method")
    @classmethod
    def _validate_method(cls, v: str) -> str:
        m = str(v).strip().upper()
        if m not in VALID_HTTP_METHODS:
            raise ValueError(f"Unsupported HTTP method: '{v}'")
        return m

    @field_validator("initial_request_fingerprint")
    @classmethod
    def _validate_ifp(cls, v: str) -> str:
        if not HEX_64_PATTERN.match(v):
            raise ValueError(f"initial_request_fingerprint must be 64 hex characters (got '{v}')")
        return v

    @field_validator("operation_id", "path_template", mode="before")
    @classmethod
    def _clean_op_strings(cls, v: Optional[str]) -> Optional[str]:
        return ReportDataSanitizer.sanitize_text(v) if v else None

    @field_validator("followup_decisions", "transient_retries", "variation_candidates", "variation_tests", "remediation_recommendations", mode="before")
    @classmethod
    def _convert_tuples(cls, v: Any, info: Any) -> Tuple[Any, ...]:
        if not isinstance(v, (list, tuple)):
            raise TypeError(f"{info.field_name} must be a sequence")
        return tuple(v)


class VariationMetricsSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates_generated: int = Field(default=0, ge=0)
    candidates_schema_valid: int = Field(default=0, ge=0)
    candidates_rejected: int = Field(default=0, ge=0)
    candidates_approved: int = Field(default=0, ge=0)
    candidates_dispatched: int = Field(default=0, ge=0)
    candidates_dispatch_failed: int = Field(default=0, ge=0)
    rejections_schema_invalid: int = Field(default=0, ge=0)
    rejections_unauthorized: int = Field(default=0, ge=0)
    rejections_budget_exhausted: int = Field(default=0, ge=0)
    variation_tests_passed: int = Field(default=0, ge=0)
    variation_tests_failed: int = Field(default=0, ge=0)
    variation_success_rate_percentage: float = Field(default=0.0, ge=0.0, le=100.0)

    @field_validator("variation_success_rate_percentage", mode="before")
    @classmethod
    def _clean_rate(cls, v: float) -> float:
        return ReportDataSanitizer.ensure_finite_non_negative(v, "variation_success_rate_percentage")


class RunSummaryStatistics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_operations: int = Field(..., ge=0)
    passed_operations: int = Field(..., ge=0)
    resolved_by_transient_retry: int = Field(..., ge=0)
    failed_operations: int = Field(..., ge=0)
    error_operations: int = Field(..., ge=0)
    total_transient_retries_dispatched: int = Field(..., ge=0)
    total_requests_executed: int = Field(..., ge=0)
    pass_rate_percentage: float = Field(..., ge=0.0, le=100.0)
    total_duration_ms: float = Field(..., ge=0.0)
    variation_metrics: VariationMetricsSummary = Field(default_factory=VariationMetricsSummary)

    @field_validator("pass_rate_percentage", mode="before")
    @classmethod
    def _clean_pr(cls, v: float) -> float:
        return ReportDataSanitizer.ensure_finite_non_negative(v, "pass_rate_percentage")

    @field_validator("total_duration_ms", mode="before")
    @classmethod
    def _clean_dur(cls, v: float) -> float:
        return ReportDataSanitizer.ensure_finite_non_negative(v, "total_duration_ms")


class DiagnosticReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: str = Field(...)
    timestamp: str = Field(...)
    api_title: str = Field(...)
    api_version: str = Field(...)
    base_url: Optional[str] = Field(default=None)
    summary: RunSummaryStatistics = Field(...)
    operations: Tuple[OperationRunRecord, ...] = Field(...)
    global_remediation_summary: Tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("report_id", "timestamp", "api_title", "api_version", mode="before")
    @classmethod
    def _clean_rep_strings(cls, v: str) -> str:
        return ReportDataSanitizer.sanitize_text(v)

    @field_validator("base_url", mode="before")
    @classmethod
    def _clean_burl(cls, v: Optional[str]) -> Optional[str]:
        return ReportDataSanitizer.sanitize_url(v)

    @field_validator("operations", "global_remediation_summary", mode="before")
    @classmethod
    def _convert_top_tuples(cls, v: Any, info: Any) -> Tuple[Any, ...]:
        if not isinstance(v, (list, tuple)):
            raise TypeError(f"{info.field_name} must be a sequence")
        return tuple(ReportDataSanitizer.sanitize_text(s) if isinstance(s, str) else s for s in v)

    @model_validator(mode="after")
    def verify_complete_report_consistency(self) -> "DiagnosticReport":
        s = self.summary
        ops = self.operations

        # 1. Total operations matches length of operations tuple
        if s.total_operations != len(ops):
            raise ValueError(f"total_operations ({s.total_operations}) != len(operations) ({len(ops)})")

        # 2. Category counts match actual operation final_status values
        passed_cnt = sum(1 for op in ops if op.final_status == OverallRunStatus.PASSED)
        resolved_retry_cnt = sum(1 for op in ops if op.final_status == OverallRunStatus.RESOLVED_AFTER_TRANSIENT_RETRY)
        failed_cnt = sum(1 for op in ops if op.final_status == OverallRunStatus.FAILED)
        error_cnt = sum(1 for op in ops if op.final_status == OverallRunStatus.ERROR)

        if s.passed_operations != passed_cnt:
            raise ValueError(f"passed_operations mismatch: summary={s.passed_operations}, actual={passed_cnt}")
        if s.resolved_by_transient_retry != resolved_retry_cnt:
            raise ValueError(f"resolved_by_transient_retry mismatch: summary={s.resolved_by_transient_retry}, actual={resolved_retry_cnt}")
        if s.failed_operations != failed_cnt:
            raise ValueError(f"failed_operations mismatch: summary={s.failed_operations}, actual={failed_cnt}")
        if s.error_operations != error_cnt:
            raise ValueError(f"error_operations mismatch: summary={s.error_operations}, actual={error_cnt}")

        if (passed_cnt + resolved_retry_cnt + failed_cnt + error_cnt) != s.total_operations:
            raise ValueError("Sum of operation categories does not equal total_operations.")

        # 3. Dispatched request count reconciles with initial, retries, and variations
        actual_dispatched = (
            sum(1 for op in ops if op.initial_attempt.dispatched)
            + sum(1 for op in ops for r in op.transient_retries if r.outcome.dispatched)
            + sum(1 for op in ops for v in op.variation_tests if v.outcome.dispatched)
        )
        if s.total_requests_executed != actual_dispatched:
            raise ValueError(f"total_requests_executed mismatch: summary={s.total_requests_executed}, actual={actual_dispatched}")

        # 4. Transient retry count reconciles
        actual_retries = sum(len(op.transient_retries) for op in ops)
        if s.total_transient_retries_dispatched != actual_retries:
            raise ValueError(f"total_transient_retries_dispatched mismatch: summary={s.total_transient_retries_dispatched}, actual={actual_retries}")

        # 5. Combined follow-up budget, unified ledger reconciliation, sequential numbering, and state transitions
        for op in ops:
            followups = len(op.transient_retries) + len(op.variation_tests)
            if followups > 2:
                raise ValueError(f"Operation {op.http_method} {op.path_template} exceeded max 2 follow-ups (found {followups})")

            # Unified ledger sequential numbering and mapping
            seen_followup_nums = set()
            dispatched_retries_from_ledger = []
            dispatched_variations_from_ledger = []

            for idx, d in enumerate(op.followup_decisions, start=1):
                if d.followup_number != idx:
                    raise ValueError(f"Ledger followup_number out of sequence: expected {idx}, got {d.followup_number}")
                if d.followup_number in seen_followup_nums:
                    raise ValueError(f"Duplicate followup_number {d.followup_number}")
                seen_followup_nums.add(d.followup_number)

                if d.dispatched:
                    if d.action == RetestAction.RETRY_TRANSIENT:
                        dispatched_retries_from_ledger.append(d.followup_number)
                    elif d.action == RetestAction.RUN_VARIATION_TEST:
                        dispatched_variations_from_ledger.append(d.target_candidate_id)

            # Check that dispatched retry decisions match transient retries
            if len(dispatched_retries_from_ledger) != len(op.transient_retries):
                raise ValueError("Dispatched transient retry count in ledger does not match transient_retries")

            # Check candidate mapping dictionary
            cand_map = {c.candidate_id: c for c in op.variation_candidates}

            # Transient retry checks & fingerprint reconciliation
            for idx, r in enumerate(op.transient_retries, start=1):
                if not r.outcome.dispatched:
                    raise ValueError(f"Recorded transient retry attempt {r.attempt_number} has dispatched=False")
                # Fingerprint check: if retry of initial, matches initial; if retry of variation, matches candidate
                if r.retry_of == "initial":
                    if r.canonical_request_fingerprint != op.initial_request_fingerprint:
                        raise ValueError("Retry of initial attempt has mismatched fingerprint")
                else:
                    if r.retry_of not in cand_map:
                        raise ValueError(f"Retry of unknown candidate '{r.retry_of}'")
                    cand = cand_map[r.retry_of]
                    if cand.canonical_request_fingerprint is None:
                        raise ValueError(f"Candidate '{r.retry_of}' has no canonical fingerprint")
                    if r.canonical_request_fingerprint != cand.canonical_request_fingerprint:
                        raise ValueError(f"Retry of variation '{r.retry_of}' has mismatched fingerprint")

            if len(op.transient_retries) >= 1:
                # If retry is of initial attempt, initial attempt must have failed
                retries_of_initial = [r for r in op.transient_retries if r.retry_of == "initial"]
                if retries_of_initial and op.initial_attempt.passed:
                    raise ValueError("Transient retry executed after an initial_attempt that already passed.")
                if retries_of_initial and not op.initial_attempt.dispatched:
                    raise ValueError("Transient retry executed after an initial_attempt that was not dispatched.")

            if len(op.transient_retries) == 2:
                if op.transient_retries[0].outcome.passed:
                    raise ValueError("Second transient retry executed after first retry already passed.")

            # Variation checks
            seen_test_cids = set()
            for v in op.variation_tests:
                if v.candidate_id in seen_test_cids:
                    raise ValueError(f"Duplicate variation test reference to candidate_id '{v.candidate_id}'")
                seen_test_cids.add(v.candidate_id)

                if v.candidate_id not in cand_map:
                    raise ValueError(f"Variation test references non-existent candidate_id '{v.candidate_id}'")
                cand = cand_map[v.candidate_id]
                if not cand.dispatched:
                    raise ValueError(f"Variation test references candidate '{v.candidate_id}' with dispatched=False")
                if cand.canonical_request_fingerprint is None or v.canonical_request_fingerprint != cand.canonical_request_fingerprint:
                    raise ValueError(f"Variation test '{v.candidate_id}' fingerprint does not match candidate")

            dispatched_cids = {c.candidate_id for c in op.variation_candidates if c.dispatched}
            if dispatched_cids != seen_test_cids:
                raise ValueError(f"Dispatched candidate set {dispatched_cids} does not equal variation test set {seen_test_cids}")

            # State transition invariants & final_outcome reconciliation
            if op.final_status == OverallRunStatus.PASSED:
                if not (op.initial_attempt.passed and op.initial_attempt.dispatched):
                    raise ValueError("Operation marked PASSED but initial_attempt did not pass or was not dispatched")
                if len(op.transient_retries) > 0:
                    raise ValueError("Operation marked PASSED but has transient retries recorded")
                if not op.final_outcome.passed:
                    raise ValueError("Operation marked PASSED but final_outcome.passed is False")
                if op.final_outcome != op.initial_attempt:
                    raise ValueError("Operation marked PASSED but final_outcome does not match initial_attempt")

            elif op.final_status == OverallRunStatus.RESOLVED_AFTER_TRANSIENT_RETRY:
                if op.initial_attempt.passed:
                    raise ValueError("Operation marked RESOLVED_AFTER_TRANSIENT_RETRY but initial_attempt passed")
                if len(op.transient_retries) == 0:
                    raise ValueError("Operation marked RESOLVED_AFTER_TRANSIENT_RETRY but has 0 transient retries")
                retries_of_initial = [r for r in op.transient_retries if r.retry_of == "initial"]
                if not retries_of_initial or not retries_of_initial[-1].outcome.passed:
                    raise ValueError("RESOLVED_AFTER_TRANSIENT_RETRY requires passing retry of initial attempt")
                if not op.final_outcome.passed:
                    raise ValueError("Operation marked RESOLVED_AFTER_TRANSIENT_RETRY but final_outcome.passed is False")
                if op.final_outcome != retries_of_initial[-1].outcome:
                    raise ValueError("Operation marked RESOLVED_AFTER_TRANSIENT_RETRY but final_outcome does not match last retry")

            elif op.final_status == OverallRunStatus.FAILED:
                if op.initial_attempt.passed:
                    raise ValueError("Operation marked FAILED but initial_attempt passed")
                retries_of_initial = [r for r in op.transient_retries if r.retry_of == "initial"]
                if retries_of_initial and retries_of_initial[-1].outcome.passed:
                    raise ValueError("Operation marked FAILED but retry of initial passed")
                if op.final_outcome.passed:
                    raise ValueError("Operation marked FAILED but final_outcome.passed is True")
                expected_last = retries_of_initial[-1].outcome if retries_of_initial else op.initial_attempt
                if op.final_outcome != expected_last:
                    raise ValueError("Operation marked FAILED but final_outcome does not match last determining attempt")

            elif op.final_status == OverallRunStatus.ERROR:
                if op.final_outcome.passed:
                    raise ValueError("Operation marked ERROR but final_outcome.passed is True")
                is_pre_dispatch = not op.initial_attempt.dispatched and op.initial_attempt.failure_category != FailureCategory.SSRF_SECURITY_BLOCK
                is_eval_crash = op.initial_attempt.failure_category == FailureCategory.EVALUATION_INCOMPLETE
                if not (is_pre_dispatch or is_eval_crash):
                    raise ValueError(f"Operation marked ERROR but failure category is {op.initial_attempt.failure_category}")
                if op.final_outcome != op.initial_attempt:
                    raise ValueError("Operation marked ERROR but final_outcome does not match initial_attempt")

        # 6. Candidate metrics reconciliation
        vm = s.variation_metrics
        act_c_gen = sum(len(op.variation_candidates) for op in ops)
        act_c_val = sum(sum(1 for c in op.variation_candidates if c.schema_validation_passed) for op in ops)
        act_c_app = sum(sum(1 for c in op.variation_candidates if c.approved_for_dispatch) for op in ops)
        act_c_rej = sum(sum(1 for c in op.variation_candidates if not c.approved_for_dispatch) for op in ops)
        act_c_disp = sum(sum(1 for c in op.variation_candidates if c.dispatched) for op in ops)
        act_c_fail = sum(sum(1 for c in op.variation_candidates if c.approved_for_dispatch and not c.dispatched) for op in ops)

        act_rej_schema = sum(sum(1 for c in op.variation_candidates if not c.schema_validation_passed) for op in ops)
        act_rej_auth = sum(sum(1 for c in op.variation_candidates if c.schema_validation_passed and not c.authorization_approved) for op in ops)
        act_rej_budget = sum(sum(1 for c in op.variation_candidates if c.schema_validation_passed and c.authorization_approved and not c.budget_approved) for op in ops)

        act_v_pass = sum(sum(1 for v in op.variation_tests if v.outcome.passed) for op in ops)
        act_v_fail = sum(sum(1 for v in op.variation_tests if not v.outcome.passed) for op in ops)

        if vm.candidates_generated != act_c_gen:
            raise ValueError(f"candidates_generated mismatch: summary={vm.candidates_generated}, actual={act_c_gen}")
        if vm.candidates_schema_valid != act_c_val:
            raise ValueError(f"candidates_schema_valid mismatch: summary={vm.candidates_schema_valid}, actual={act_c_val}")
        if vm.candidates_rejected != act_c_rej:
            raise ValueError(f"candidates_rejected mismatch: summary={vm.candidates_rejected}, actual={act_c_rej}")
        if vm.candidates_approved != act_c_app:
            raise ValueError(f"candidates_approved mismatch: summary={vm.candidates_approved}, actual={act_c_app}")
        if vm.candidates_dispatched != act_c_disp:
            raise ValueError(f"candidates_dispatched mismatch: summary={vm.candidates_dispatched}, actual={act_c_disp}")
        if vm.candidates_dispatch_failed != act_c_fail:
            raise ValueError(f"candidates_dispatch_failed mismatch: summary={vm.candidates_dispatch_failed}, actual={act_c_fail}")

        if vm.rejections_schema_invalid != act_rej_schema:
            raise ValueError(f"rejections_schema_invalid mismatch: summary={vm.rejections_schema_invalid}, actual={act_rej_schema}")
        if vm.rejections_unauthorized != act_rej_auth:
            raise ValueError(f"rejections_unauthorized mismatch: summary={vm.rejections_unauthorized}, actual={act_rej_auth}")
        if vm.rejections_budget_exhausted != act_rej_budget:
            raise ValueError(f"rejections_budget_exhausted mismatch: summary={vm.rejections_budget_exhausted}, actual={act_rej_budget}")

        if (act_rej_schema + act_rej_auth + act_rej_budget) != act_c_rej:
            raise ValueError("Sum of rejections does not equal candidates_rejected")
        if (act_c_app + act_c_rej) != act_c_gen:
            raise ValueError("candidates_approved + candidates_rejected != candidates_generated")
        if (act_c_disp + act_c_fail) != act_c_app:
            raise ValueError("candidates_dispatched + candidates_dispatch_failed != candidates_approved")
        if (act_v_pass + act_v_fail) != act_c_disp:
            raise ValueError("variation_tests_passed + variation_tests_failed != candidates_dispatched")

        # 7. Exact pass rate formula checks
        expected_op_rate = (
            round(((s.passed_operations + s.resolved_by_transient_retry) / s.total_operations) * 100.0, 2)
            if s.total_operations > 0 else 0.0
        )
        if s.pass_rate_percentage != expected_op_rate:
            raise ValueError(f"pass_rate_percentage mismatch: expected {expected_op_rate}, got {s.pass_rate_percentage}")

        expected_var_rate = (
            round((act_v_pass / act_c_disp) * 100.0, 2)
            if act_c_disp > 0 else 0.0
        )
        if vm.variation_success_rate_percentage != expected_var_rate:
            raise ValueError(f"variation_success_rate_percentage mismatch: expected {expected_var_rate}, got {vm.variation_success_rate_percentage}")

        # 8. Numeric range checks
        if not (math.isfinite(s.total_duration_ms) and s.total_duration_ms >= 0.0):
            raise ValueError("total_duration_ms must be finite and >= 0.0")

        return self
