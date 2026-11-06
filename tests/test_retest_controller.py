"""
Unit tests for RetestController, Request Fingerprinting, and Decision Logic (Phase 4C).
"""

from datetime import datetime, timedelta, timezone
import email.utils
import secrets
import pytest

from backend.app.schemas.execution import ExecutionRequest
from backend.app.schemas.failure import (
    FailureCategory,
    FailureClassification,
    FailureSeverity,
    RetryGuidance,
    RetryPolicy,
    SanitizedFailureEvidence,
)
from backend.app.schemas.retest import (
    ParameterPerturbation,
    PerturbationStrategy,
    PolicyAuthorization,
    RetestAction,
)
from backend.app.schemas.spec import EndpointDefinition, ParameterDefinition
from backend.app.services.retest_controller import RetestController


def _mock_classification(
    cat: FailureCategory,
    status_code: int = 0,
    is_idempotent: bool = True,
    method: str = "GET",
) -> FailureClassification:
    policy = RetryPolicy.SAFE_TO_RETRY if is_idempotent else RetryPolicy.UNSAFE_NON_IDEMPOTENT
    return FailureClassification(
        has_failure=True,
        primary_category=cat,
        secondary_categories=[],
        severity=FailureSeverity.MEDIUM,
        retry_guidance=RetryGuidance(
            policy=policy,
            is_idempotent_method=is_idempotent,
            reason=f"Classification for {cat.value}",
        ),
        summary=f"Mock failure summary for {cat.value}",
        evidence=SanitizedFailureEvidence(
            http_method=method,
            path_template="/api/items",
            status_code=status_code,
        ),
    )


def test_retest_cap_enforced():
    """Confirms follow-up budget is strictly capped at max 2."""
    classification = _mock_classification(FailureCategory.REQUEST_TIMEOUT, is_idempotent=True)

    # Attempt 0 -> budget remaining 2
    elig0 = RetestController.evaluate_retry_eligibility("GET", classification, followup_count=0)
    assert elig0.eligible is True
    assert elig0.remaining_followup_budget == 2

    # Attempt 1 -> budget remaining 1
    elig1 = RetestController.evaluate_retry_eligibility("GET", classification, followup_count=1)
    assert elig1.eligible is True
    assert elig1.remaining_followup_budget == 1

    # Attempt 2 -> budget exhausted
    elig2 = RetestController.evaluate_retry_eligibility("GET", classification, followup_count=2)
    assert elig2.eligible is False
    assert elig2.action == RetestAction.NO_ACTION
    assert elig2.remaining_followup_budget == 0
    assert "budget exhausted" in elig2.reason.lower()


def test_idempotent_method_transient_allowed():
    """Confirms idempotent methods (GET, PUT, DELETE) are eligible for transient retry."""
    classification = _mock_classification(FailureCategory.REQUEST_TIMEOUT, is_idempotent=True)

    for method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE"):
        dec = RetestController.decide_transient_retry(method, classification, followup_count=0)
        assert dec.action == RetestAction.RETRY_TRANSIENT
        assert dec.backoff_delay_seconds > 0.0


def test_non_idempotent_method_transient_blocked():
    """Confirms non-idempotent methods (POST, PATCH) are blocked from transient retries."""
    classification = _mock_classification(FailureCategory.REQUEST_TIMEOUT, is_idempotent=False, method="POST")

    for method in ("POST", "PATCH"):
        dec = RetestController.decide_transient_retry(method, classification, followup_count=0)
        assert dec.action == RetestAction.NO_ACTION
        assert "non-idempotent" in dec.rationale.lower()


def test_retry_after_parses_integer_delta_seconds():
    """Confirms integer delta-seconds are parsed correctly."""
    assert RetestController.parse_retry_after("2") == 2.0
    assert RetestController.parse_retry_after("0") == 0.0
    assert RetestController.parse_retry_after("10") == 10.0


def test_retry_after_rejects_decimal_seconds():
    """Confirms decimal seconds like '1.5' are rejected as malformed."""
    assert RetestController.parse_retry_after("1.5") is None
    assert RetestController.parse_retry_after("0.1") is None


def test_retry_after_parses_rfc5322_http_date():
    """Confirms HTTP-date parsing and past/future calculation."""
    # Future date in 3 seconds
    future_dt = datetime.now(timezone.utc) + timedelta(seconds=3)
    future_str = email.utils.format_datetime(future_dt)
    parsed_future = RetestController.parse_retry_after(future_str)
    assert parsed_future is not None
    assert 1.0 <= parsed_future <= 5.0

    # Past date
    past_dt = datetime.now(timezone.utc) - timedelta(seconds=10)
    past_str = email.utils.format_datetime(past_dt)
    parsed_past = RetestController.parse_retry_after(past_str)
    assert parsed_past == 0.0


def test_retry_after_exceeding_max_skips_retry_never_shortens():
    """Confirms delay exceeding 5.0s skips retry entirely rather than clamping."""
    classification = _mock_classification(FailureCategory.RATE_LIMITING, status_code=429, is_idempotent=True)

    dec = RetestController.decide_transient_retry(
        "GET", classification, retry_after_header="30", followup_count=0
    )
    assert dec.action == RetestAction.NO_ACTION
    assert "exceeds maximum allowable threshold" in dec.rationale
    assert dec.backoff_delay_seconds == 0.0


def test_canonical_request_fingerprint_hmac_equality_and_difference():
    """Confirms canonical request fingerprinting using ephemeral HMAC key."""
    key = secrets.token_bytes(32)

    req1 = ExecutionRequest(
        method="GET",
        url="http://api.local/v1/orders/123",
        params={"sort": "desc", "filter": "active"},
        headers={"Accept": "application/json", "Authorization": "Bearer secret"},
        json_body=None,
    )
    # Identical parameters in different dict insertion order
    req2 = ExecutionRequest(
        method="GET",
        url="http://api.local/v1/orders/123",
        params={"filter": "active", "sort": "desc"},
        headers={"Accept": "application/json", "Authorization": "Bearer different_secret"},
        json_body=None,
    )
    # Mutated request
    req3 = ExecutionRequest(
        method="GET",
        url="http://api.local/v1/orders/123",
        params={"filter": "inactive", "sort": "desc"},
        headers={"Accept": "application/json"},
        json_body=None,
    )

    fp1 = RetestController.compute_canonical_request_fingerprint(req1, key)
    fp2 = RetestController.compute_canonical_request_fingerprint(req2, key)
    fp3 = RetestController.compute_canonical_request_fingerprint(req3, key)

    assert len(fp1) == 64
    assert fp1 == fp2  # Excluded auth header and sorted params produce identical fingerprint
    assert fp1 != fp3  # Mutated query value changes fingerprint


def test_unauthorized_post_patch_variations_denied_by_default():
    """Confirms non-idempotent variations are denied by default without authorization."""
    op = EndpointDefinition(
        path="/api/orders",
        method="POST",
        parameters=[
            ParameterDefinition(
                name="count",
                **{"in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 10}},
            )
        ],
    )
    classification = _mock_classification(
        FailureCategory.REQUEST_VALIDATION_FAILURE, status_code=422, is_idempotent=False, method="POST"
    )

    dec, cand_data, _ = RetestController.synthesize_variation_candidate(
        candidate_id="cand_1",
        operation=op,
        classification=classification,
        followup_count=0,
        policy_authorization=None,
    )

    assert dec.action == RetestAction.NO_ACTION
    assert cand_data["approved_for_dispatch"] is False
    assert cand_data["authorization_approved"] is False
    assert "non-idempotent mutations prohibited by default" in cand_data["rejection_reason"].lower()


def test_variation_candidate_schema_boundary_clamp():
    """Confirms candidate synthesis clamps parameters to OpenAPI schema boundaries."""
    op = EndpointDefinition(
        path="/api/items",
        method="GET",
        parameters=[
            ParameterDefinition(
                name="limit",
                **{"in": "query", "schema": {"type": "integer", "minimum": 5, "maximum": 50}},
            )
        ],
    )
    classification = _mock_classification(
        FailureCategory.REQUEST_VALIDATION_FAILURE, status_code=400, is_idempotent=True, method="GET"
    )

    dec, cand_data, meta = RetestController.synthesize_variation_candidate(
        candidate_id="cand_1",
        operation=op,
        classification=classification,
        followup_count=0,
        policy_authorization=None,
    )

    assert dec.action == RetestAction.RUN_VARIATION_TEST
    assert cand_data["approved_for_dispatch"] is True
    assert cand_data["strategy"] == PerturbationStrategy.BOUNDARY_CLAMP
    assert len(dec.planned_perturbations) == 1
    assert dec.planned_perturbations[0].varied_value == 5
    assert dec.planned_perturbations[0].pre_execution_validated is True

