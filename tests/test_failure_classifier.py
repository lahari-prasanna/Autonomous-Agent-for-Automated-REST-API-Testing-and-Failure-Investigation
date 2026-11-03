"""
Comprehensive unit test suite for Deterministic Failure Classification Engine (Phase 4A).
Validates:
1. Missing assertion report handling across HTTP 200, 404, and 500 (EVALUATION_INCOMPLETE).
2. Lifecycle error precedence over downstream/stale assertion reports.
3. Trusting assertion reports only when workflow reached 'evaluated' without error.
4. Declared vs undeclared 5xx responses (negative contract testing vs server defects).
5. Expected vs unexpected 4xx responses.
6. Schema and Content-Type mismatch classifications.
7. Distinct handling of timeout, network failure, and SSRF blocks.
8. Idempotency-aware conservative retry guidance (GET vs POST).
9. Strict allowlist-based evidence sanitization against secrets and sensitive values.
10. Missing, uninitialized, and malformed state handling.
"""

import json
import pytest

from backend.app.agent.state import AgentError, AgentState, create_initial_state
from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.failure import (
    FailureCategory,
    FailureClassification,
    FailureSeverity,
    RetryPolicy,
    SanitizedFailureEvidence,
)
from backend.app.schemas.spec import EndpointDefinition, ParsedAPISpec, ResponseDefinition
from backend.app.services.assertion_evaluator import AssertionReport, AssertionResult
from backend.app.services.failure_classifier import (
    FailureClassifier,
    create_fresh_run_state,
    run_agent_workflow,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_get_endpoint() -> EndpointDefinition:
    """Standard GET /api/products/{id} endpoint declaring only 200 and 422."""
    return EndpointDefinition(
        path="/api/products/{product_id}",
        method="GET",
        operation_id="get_product",
        summary="Get Product",
        parameters=[],
        responses={
            "200": ResponseDefinition(
                status_code="200",
                description="Success",
                content={"application/json": {"schema": {"type": "object", "required": ["id", "name"]}}},
            ),
            "422": ResponseDefinition(
                status_code="422",
                description="Validation Error",
                content={"application/json": {"schema": {"type": "object"}}},
            ),
        },
    )


@pytest.fixture
def sample_post_endpoint() -> EndpointDefinition:
    """Standard POST /api/products endpoint declaring 201 and 400."""
    return EndpointDefinition(
        path="/api/products",
        method="POST",
        operation_id="create_product",
        summary="Create Product",
        parameters=[],
        responses={
            "201": ResponseDefinition(
                status_code="201",
                description="Created",
                content={"application/json": {"schema": {"type": "object", "required": ["id"]}}},
            ),
            "400": ResponseDefinition(
                status_code="400",
                description="Bad Request",
                content={"application/json": {"schema": {"type": "object", "required": ["detail"]}}},
            ),
        },
    )


@pytest.fixture
def sample_503_endpoint() -> EndpointDefinition:
    """Maintenance endpoint explicitly declaring a 503 response contract."""
    return EndpointDefinition(
        path="/api/maintenance",
        method="GET",
        operation_id="check_maintenance",
        summary="Check Maintenance",
        parameters=[],
        responses={
            "503": ResponseDefinition(
                status_code="503",
                description="Service Maintenance Outage",
                content={
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["retry_after", "maintenance_window"],
                            "properties": {
                                "retry_after": {"type": "integer"},
                                "maintenance_window": {"type": "string"},
                            },
                        }
                    }
                },
            ),
        },
    )


@pytest.fixture
def sample_spec(
    sample_get_endpoint: EndpointDefinition,
    sample_post_endpoint: EndpointDefinition,
    sample_503_endpoint: EndpointDefinition,
) -> ParsedAPISpec:
    """Standard ParsedAPISpec containing the sample endpoints."""
    return ParsedAPISpec(
        title="Sample Test API",
        version="1.0.0",
        openapi_version="3.0.3",
        servers=["http://127.0.0.1:8081"],
        endpoints=[sample_get_endpoint, sample_post_endpoint, sample_503_endpoint],
    )


# ---------------------------------------------------------------------------
# Group 1: Missing Assertion Report Handling (EVALUATION_INCOMPLETE)
# ---------------------------------------------------------------------------

def test_missing_assertion_report_on_200_transport_exchange(sample_get_endpoint: EndpointDefinition):
    """
    Mandatory Behavior: A completed HTTP 200 exchange without assertion report
    must NEVER be treated as a pass. Must flag EVALUATION_INCOMPLETE, has_failure=True,
    HIGH severity, and DO_NOT_RETRY.
    """
    state = create_initial_state(
        selected_operation=sample_get_endpoint,
        status="executed",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/1",
            status_code=200,
            is_success=True,
            headers={"content-type": "application/json"},
            latency_ms=10.5,
        ),
        assertion_report=None,  # Intentionally missing
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.EVALUATION_INCOMPLETE
    assert classification.severity == FailureSeverity.HIGH
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY
    assert "Contract compliance cannot be determined" in classification.summary
    assert classification.evidence.status_code == 200
    assert classification.evidence.lifecycle_step == "executed"


def test_missing_assertion_report_on_404_transport_exchange(sample_get_endpoint: EndpointDefinition):
    """
    Mandatory Behavior: HTTP 404 without assertion report must not guess whether 404
    was expected; must mark EVALUATION_INCOMPLETE with DO_NOT_RETRY.
    """
    state = create_initial_state(
        selected_operation=sample_get_endpoint,
        status="executed",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/99999",
            status_code=404,
            is_success=True,
            headers={"content-type": "application/json"},
            latency_ms=8.0,
        ),
        assertion_report=None,
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.EVALUATION_INCOMPLETE
    assert classification.severity == FailureSeverity.HIGH
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY
    assert "Contract compliance cannot be determined" in classification.summary


def test_missing_assertion_report_on_500_transport_exchange(sample_post_endpoint: EndpointDefinition):
    """
    Mandatory Behavior: HTTP 500 without assertion report must mark EVALUATION_INCOMPLETE
    because contract validation was bypassed.
    """
    state = create_initial_state(
        selected_operation=sample_post_endpoint,
        status="executed",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=500,
            is_success=True,
            headers={"content-type": "text/html"},
            latency_ms=25.0,
        ),
        assertion_report=None,
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.EVALUATION_INCOMPLETE
    assert classification.severity == FailureSeverity.HIGH
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY


# ---------------------------------------------------------------------------
# Group 2: Lifecycle Error Precedence & Stale Assertion Rejection
# ---------------------------------------------------------------------------

def test_lifecycle_error_precedence_over_stale_successful_assertion():
    """
    Mandatory Behavior: An active error in state (e.g. SpecNotFoundError at spec_loaded)
    takes absolute precedence over an old/stale assertion report where is_success=True.
    """
    stale_report = AssertionReport(
        endpoint_path="/api/products",
        method="GET",
        status_code=200,
        matched_status_key="200",
        is_success=True,
        assertions=[],
    )

    state = create_initial_state(
        status="error",
        error=AgentError(
            message="Spec file not found on disk",
            error_type="SpecNotFoundError",
            step="spec_loaded",
        ),
        assertion_report=stale_report,  # Stale report from prior execution
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.SPEC_OR_OPERATION_FAILURE
    assert classification.severity == FailureSeverity.HIGH
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY
    assert "SpecNotFoundError" in classification.summary
    assert classification.evidence.error_type == "SpecNotFoundError"
    assert classification.evidence.lifecycle_step == "spec_loaded"


def test_lifecycle_request_preparation_error_precedence():
    """MissingBaseUrlError at step request_ready classifies as REQUEST_PREPARATION_FAILURE."""
    state = create_initial_state(
        status="error",
        error=AgentError(
            message="Target URL lacks a base host scheme",
            error_type="MissingBaseUrlError",
            step="request_ready",
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.REQUEST_PREPARATION_FAILURE
    assert classification.severity == FailureSeverity.HIGH
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY
    assert "MissingBaseUrlError" in classification.summary


def test_assertion_report_untrusted_if_status_is_not_evaluated(sample_get_endpoint: EndpointDefinition):
    """
    Mandatory Behavior: A successful assertion report is trusted ONLY when
    the workflow status explicitly reached 'evaluated'.
    """
    state = create_initial_state(
        selected_operation=sample_get_endpoint,
        status="executed",  # Not yet evaluated
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/1",
            status_code=200,
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products/{product_id}",
            method="GET",
            status_code=200,
            matched_status_key="200",
            is_success=True,
        ),
    )

    classification = FailureClassifier.classify(state)

    # Must reject trust in report because status != 'evaluated'
    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.EVALUATION_INCOMPLETE


# ---------------------------------------------------------------------------
# Group 3: Valid Evaluated Success
# ---------------------------------------------------------------------------

def test_valid_evaluated_success_happy_path_200(sample_get_endpoint: EndpointDefinition):
    """When workflow reaches 'evaluated' with no error and is_success=True, has_failure=False."""
    state = create_initial_state(
        selected_operation=sample_get_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/1",
            status_code=200,
            is_success=True,
            latency_ms=15.0,
            headers={"content-type": "application/json"},
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products/{product_id}",
            method="GET",
            status_code=200,
            matched_status_key="200",
            is_success=True,
            assertions=[
                AssertionResult(name="status_code_match", passed=True, message="Matched 200"),
                AssertionResult(name="body_schema_validation", passed=True, message="Valid JSON schema"),
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is False
    assert classification.primary_category is None
    assert classification.secondary_categories == []
    assert classification.severity == FailureSeverity.LOW
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY
    assert "completed successfully" in classification.summary
    assert classification.evidence.status_code == 200
    assert classification.evidence.matched_status_key == "200"


# ---------------------------------------------------------------------------
# Group 4: Declared vs Undeclared 5xx Responses
# ---------------------------------------------------------------------------

def test_declared_503_matching_schema_is_successful_test(sample_503_endpoint: EndpointDefinition):
    """
    Mandatory Behavior: Declared 5xx matching OpenAPI schema is an expected
    negative test outcome, NOT a failure (has_failure=False, primary=None).
    """
    state = create_initial_state(
        selected_operation=sample_503_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/maintenance",
            status_code=503,
            is_success=True,
            headers={"content-type": "application/json"},
            latency_ms=12.0,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/maintenance",
            method="GET",
            status_code=503,
            matched_status_key="503",
            is_success=True,  # Successfully matched declared 503 response and schema
            assertions=[
                AssertionResult(name="status_code_match", passed=True, message="Matched 503"),
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is False
    assert classification.primary_category is None
    assert classification.secondary_categories == []
    assert "completed successfully with HTTP 503" in classification.summary


def test_declared_503_failing_schema_check(sample_503_endpoint: EndpointDefinition):
    """
    Declared 503 where response body violates schema contract must classify as
    RESPONSE_SCHEMA_MISMATCH. SERVER_ERROR must NOT appear in secondary categories
    because the 503 status code was explicitly declared in the contract.
    """
    state = create_initial_state(
        selected_operation=sample_503_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/maintenance",
            status_code=503,
            is_success=True,
            headers={"content-type": "application/json"},
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/maintenance",
            method="GET",
            status_code=503,
            matched_status_key="503",  # Status was expected!
            is_success=False,
            assertions=[
                AssertionResult(name="status_code_match", passed=True, message="Matched 503"),
                AssertionResult(
                    name="schema_required",
                    passed=False,
                    message="'retry_after' is a required property",
                    json_path="/",
                ),
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.RESPONSE_SCHEMA_MISMATCH
    assert FailureCategory.SERVER_ERROR not in classification.secondary_categories
    assert classification.secondary_categories == []
    assert classification.retry_guidance.policy == RetryPolicy.REQUIRES_MODIFICATION
    assert classification.evidence.matched_status_key == "503"
    assert "schema_required" in classification.evidence.failed_assertion_types


def test_undeclared_500_server_defect(sample_post_endpoint: EndpointDefinition):
    """
    Mandatory Behavior: Classify SERVER_ERROR ONLY for undeclared 5xx response
    (status 500-599 and matched_status_key is None).
    """
    state = create_initial_state(
        selected_operation=sample_post_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=500,
            is_success=True,
            headers={"content-type": "application/json"},
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products",
            method="POST",
            status_code=500,
            matched_status_key=None,  # Undeclared 500!
            is_success=False,
            assertions=[
                AssertionResult(
                    name="status_code_match",
                    passed=False,
                    message="HTTP status code 500 was unexpected and did not match any declared response: ['201', '400'].",
                )
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.SERVER_ERROR
    assert classification.secondary_categories == [FailureCategory.UNEXPECTED_HTTP_STATUS]
    assert classification.severity == FailureSeverity.CRITICAL
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY
    assert "Undeclared HTTP 500 Server Error" in classification.summary


def test_undeclared_503_server_defect(sample_post_endpoint: EndpointDefinition):
    """
    Undeclared 503 Service Unavailable classifies as SERVER_ERROR with UNEXPECTED_HTTP_STATUS
    secondary, because 503 was not declared in the endpoint contract.
    """
    state = create_initial_state(
        selected_operation=sample_post_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=503,
            is_success=True,
            headers={"content-type": "application/json"},
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products",
            method="POST",
            status_code=503,
            matched_status_key=None,  # Undeclared 503!
            is_success=False,
            assertions=[
                AssertionResult(
                    name="status_code_match",
                    passed=False,
                    message="HTTP status code 503 was unexpected and did not match any declared response: ['201', '400'].",
                )
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.SERVER_ERROR
    assert classification.secondary_categories == [FailureCategory.UNEXPECTED_HTTP_STATUS]
    assert classification.severity == FailureSeverity.CRITICAL
    assert classification.retry_guidance.policy == RetryPolicy.UNSAFE_NON_IDEMPOTENT
    assert "Undeclared HTTP 503 Server Error" in classification.summary


# ---------------------------------------------------------------------------
# Group 5: Expected vs Unexpected 4xx Responses
# ---------------------------------------------------------------------------

def test_expected_400_with_valid_schema_is_success(sample_post_endpoint: EndpointDefinition):
    """
    Mandatory Behavior: An expected 400 Bad Request with valid error schema
    is a contract pass, NOT a failure (has_failure=False, primary=None).
    """
    state = create_initial_state(
        selected_operation=sample_post_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=400,
            is_success=True,
            headers={"content-type": "application/json"},
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products",
            method="POST",
            status_code=400,
            matched_status_key="400",  # Declared in sample_post_endpoint
            is_success=True,
            assertions=[
                AssertionResult(name="status_code_match", passed=True, message="Matched 400"),
                AssertionResult(name="body_schema_validation", passed=True, message="Matched detail schema"),
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is False
    assert classification.primary_category is None
    assert classification.secondary_categories == []
    assert "completed successfully with HTTP 400" in classification.summary


def test_unexpected_401_unauthorized(sample_post_endpoint: EndpointDefinition):
    """Undeclared 401 classifies as UNEXPECTED_HTTP_STATUS with AUTHENTICATION_FAILURE secondary."""
    state = create_initial_state(
        selected_operation=sample_post_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=401,
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products",
            method="POST",
            status_code=401,
            matched_status_key=None,
            is_success=False,
            assertions=[
                AssertionResult(name="status_code_match", passed=False, message="Undeclared 401")
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.UNEXPECTED_HTTP_STATUS
    assert FailureCategory.AUTHENTICATION_FAILURE in classification.secondary_categories
    assert classification.retry_guidance.policy == RetryPolicy.REQUIRES_MODIFICATION


def test_unexpected_404_resource_not_found(sample_get_endpoint: EndpointDefinition):
    """Undeclared 404 classifies as UNEXPECTED_HTTP_STATUS with RESOURCE_NOT_FOUND secondary."""
    state = create_initial_state(
        selected_operation=sample_get_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/9999",
            status_code=404,
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products/{product_id}",
            method="GET",
            status_code=404,
            matched_status_key=None,
            is_success=False,
            assertions=[
                AssertionResult(name="status_code_match", passed=False, message="Undeclared 404")
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.UNEXPECTED_HTTP_STATUS
    assert FailureCategory.RESOURCE_NOT_FOUND in classification.secondary_categories
    assert classification.retry_guidance.policy == RetryPolicy.REQUIRES_MODIFICATION


# ---------------------------------------------------------------------------
# Group 6: Contract Schema & Content-Type Violations
# ---------------------------------------------------------------------------

def test_content_type_mismatch_on_declared_200(sample_get_endpoint: EndpointDefinition):
    """Content-Type mismatch on declared 200 classifies as RESPONSE_CONTENT_TYPE_MISMATCH."""
    state = create_initial_state(
        selected_operation=sample_get_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/1",
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products/{product_id}",
            method="GET",
            status_code=200,
            matched_status_key="200",
            is_success=False,
            assertions=[
                AssertionResult(name="status_code_match", passed=True, message="Matched 200"),
                AssertionResult(
                    name="content_type_match",
                    passed=False,
                    message="Expected application/json, got text/html",
                ),
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.RESPONSE_CONTENT_TYPE_MISMATCH
    assert classification.evidence.observed_content_type == "text/html"
    assert "application/json" in classification.evidence.expected_content_types


def test_schema_property_type_mismatch_on_declared_200(sample_get_endpoint: EndpointDefinition):
    """Schema violation on declared 200 extracts json_path and schema keyword."""
    state = create_initial_state(
        selected_operation=sample_get_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/1",
            status_code=200,
            headers={"content-type": "application/json"},
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products/{product_id}",
            method="GET",
            status_code=200,
            matched_status_key="200",
            is_success=False,
            assertions=[
                AssertionResult(name="status_code_match", passed=True, message="Matched 200"),
                AssertionResult(
                    name="schema_type",
                    passed=False,
                    message="'abc' is not of type 'integer'",
                    json_path="/id",
                ),
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.RESPONSE_SCHEMA_MISMATCH
    assert "/id" in classification.evidence.json_paths
    assert "type" in classification.evidence.schema_keywords


# ---------------------------------------------------------------------------
# Group 7: Distinct Transport, Timeout, and SSRF Failures
# ---------------------------------------------------------------------------

def test_ssrf_security_block(sample_get_endpoint: EndpointDefinition):
    """SSRF security block must be distinct, CRITICAL, and DO_NOT_RETRY."""
    state = create_initial_state(
        selected_operation=sample_get_endpoint,
        status="executed",
        execution_result=ExecutionResult(
            method="GET",
            url="http://169.254.169.254/latest/meta-data/",
            status_code=0,
            is_ssrf_blocked=True,
            is_success=False,
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.SSRF_SECURITY_BLOCK
    assert classification.severity == FailureSeverity.CRITICAL
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY
    assert "SSRFGuard" in classification.summary
    assert classification.evidence.error_type == "SSRFSecurityError"


def test_timeout_get_vs_post_idempotency_guidance():
    """
    Mandatory Behavior: Timeout on idempotent GET produces SAFE_TO_RETRY,
    while timeout on non-idempotent POST produces UNSAFE_NON_IDEMPOTENT.
    """
    # 1. GET Timeout (Idempotent)
    get_state = create_initial_state(
        target_method="GET",
        status="executed",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products",
            status_code=0,
            is_timeout=True,
            is_success=False,
        ),
    )
    get_class = FailureClassifier.classify(get_state)
    assert get_class.primary_category == FailureCategory.REQUEST_TIMEOUT
    assert get_class.retry_guidance.policy == RetryPolicy.SAFE_TO_RETRY
    assert get_class.retry_guidance.is_idempotent_method is True
    assert get_class.retry_guidance.suggested_backoff_seconds == 2.0

    # 2. POST Timeout (Non-idempotent)
    post_state = create_initial_state(
        target_method="POST",
        status="executed",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=0,
            is_timeout=True,
            is_success=False,
        ),
    )
    post_class = FailureClassifier.classify(post_state)
    assert post_class.primary_category == FailureCategory.REQUEST_TIMEOUT
    assert post_class.retry_guidance.policy == RetryPolicy.UNSAFE_NON_IDEMPOTENT
    assert post_class.retry_guidance.is_idempotent_method is False


def test_network_failure_get_vs_post_idempotency_guidance():
    """Network connection failure distinguishes GET (SAFE_TO_RETRY) from POST (UNSAFE_NON_IDEMPOTENT)."""
    # 1. GET Network Failure
    get_state = create_initial_state(
        target_method="GET",
        status="executed",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:54321/api/products",
            status_code=0,
            is_network_error=True,
            is_success=False,
        ),
    )
    get_class = FailureClassifier.classify(get_state)
    assert get_class.primary_category == FailureCategory.NETWORK_FAILURE
    assert get_class.retry_guidance.policy == RetryPolicy.SAFE_TO_RETRY

    # 2. POST Network Failure
    post_state = create_initial_state(
        target_method="POST",
        status="executed",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:54321/api/products",
            status_code=0,
            is_network_error=True,
            is_success=False,
        ),
    )
    post_class = FailureClassifier.classify(post_state)
    assert post_class.primary_category == FailureCategory.NETWORK_FAILURE
    assert post_class.retry_guidance.policy == RetryPolicy.UNSAFE_NON_IDEMPOTENT


def test_transient_gateway_502_retry_guidance():
    """Undeclared 502 Bad Gateway distinguishes GET (SAFE_TO_RETRY) from POST (UNSAFE_NON_IDEMPOTENT)."""
    get_state = create_initial_state(
        target_method="GET",
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products",
            status_code=502,
            is_success=True,
            headers={"retry-after": "10"},
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products",
            method="GET",
            status_code=502,
            matched_status_key=None,
            is_success=False,
        ),
    )
    get_class = FailureClassifier.classify(get_state)
    assert get_class.primary_category == FailureCategory.SERVER_ERROR
    assert get_class.retry_guidance.policy == RetryPolicy.SAFE_TO_RETRY
    assert get_class.retry_guidance.suggested_backoff_seconds == 10.0

    post_state = create_initial_state(
        target_method="POST",
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=502,
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products",
            method="POST",
            status_code=502,
            matched_status_key=None,
            is_success=False,
        ),
    )
    post_class = FailureClassifier.classify(post_state)
    assert post_class.primary_category == FailureCategory.SERVER_ERROR
    assert post_class.retry_guidance.policy == RetryPolicy.UNSAFE_NON_IDEMPOTENT


# ---------------------------------------------------------------------------
# Group 8: Allowlist-Based Evidence Sanitization & Secret Injection Test
# ---------------------------------------------------------------------------

def test_evidence_sanitization_against_deliberately_injected_secrets():
    """
    Security Test: Injects raw secrets, passwords, Bearer tokens, cookies,
    API keys, query values, and raw exception messages. Verifies that
    SanitizedFailureEvidence strictly exposes ZERO sensitive values.
    """
    secret_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.super_secret_payload_token_9999"
    secret_password = "MySuperSecretPassword#2026!"
    secret_api_key = "ak_live_abcdef1234567890_secret"
    customer_ssn = "999-88-7777"

    # URL containing basic auth credentials AND query parameter secret values
    dirty_url = f"http://admin:{secret_password}@api.internal.corp:8443/api/customers/{customer_ssn}?token={secret_token}&api_key={secret_api_key}&filter=active"

    state = create_initial_state(
        status="evaluated",
        request_config=ExecutionRequest(
            method="POST",
            url=dirty_url,
            headers={
                "Authorization": f"Bearer {secret_token}",
                "Cookie": f"session_id={secret_token}",
                "X-Api-Key": secret_api_key,
            },
            json_body={"password": secret_password, "ssn": customer_ssn},
        ),
        execution_result=ExecutionResult(
            method="POST",
            url=dirty_url,
            status_code=400,
            is_success=True,
            headers={
                "content-type": "application/json; charset=utf-8",
                "set-cookie": f"token={secret_token}; Path=/",
            },
            body={"error": f"Invalid password {secret_password} for customer {customer_ssn}"},
            error_message=f"Raw internal exception with password={secret_password}",
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/customers/{customer_id}",
            method="POST",
            status_code=400,
            matched_status_key="400",
            is_success=False,
            assertions=[
                AssertionResult(
                    name="schema_pattern",
                    passed=False,
                    message=f"Field 'password' ({secret_password}) does not match regex",
                    json_path="/password",
                )
            ],
        ),
    )

    classification = FailureClassifier.classify(state)
    evidence: SanitizedFailureEvidence = classification.evidence

    # 1. Serialize entire classification and evidence to dict and JSON string
    serialized_dict = classification.model_dump()
    serialized_text = classification.model_dump_json()

    # 2. Strict Negative Verification: NO secrets exist anywhere in classification
    assert secret_token not in serialized_text
    assert secret_password not in serialized_text
    assert secret_api_key not in serialized_text
    assert customer_ssn not in serialized_text
    assert "admin:" not in serialized_text

    # 3. Positive Verification of allowlisted fields
    assert evidence.http_method == "POST"
    assert evidence.path_template == "/api/customers/{customer_id}"
    assert evidence.status_code == 400
    assert evidence.matched_status_key == "400"
    assert "/password" in evidence.json_paths
    assert "pattern" in evidence.schema_keywords
    assert evidence.observed_content_type == "application/json"

    # 4. Verify sanitized URL has credentials and sensitive path segments removed, and query values stripped
    assert evidence.sanitized_url == "http://api.internal.corp:8443/api/customers/{customer_id}?token&api_key&filter"
    assert secret_token not in evidence.sanitized_url
    assert customer_ssn not in evidence.sanitized_url
    assert secret_password not in evidence.sanitized_url


# ---------------------------------------------------------------------------
# Group 9: Missing or Malformed State Fields
# ---------------------------------------------------------------------------

def test_empty_or_uninitialized_state():
    """Empty state dict must classify safely without raising KeyError or AttributeError."""
    classification = FailureClassifier.classify({})

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.UNKNOWN_FAILURE
    assert classification.severity == FailureSeverity.LOW
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY


def test_state_with_dict_error_instead_of_pydantic_model():
    """Accepts dictionary representation of error in state gracefully."""
    raw_state = {
        "status": "error",
        "error": {
            "message": "Operation ghost_op not found",
            "error_type": "OperationNotFoundError",
            "step": "operation_selected",
        },
    }

    classification = FailureClassifier.classify(raw_state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.SPEC_OR_OPERATION_FAILURE
    assert classification.evidence.error_type == "OperationNotFoundError"
    assert classification.evidence.lifecycle_step == "operation_selected"


def test_unexpected_403_forbidden(sample_post_endpoint: EndpointDefinition):
    """Undeclared 403 classifies as UNEXPECTED_HTTP_STATUS with AUTHORIZATION_FAILURE secondary."""
    state = create_initial_state(
        selected_operation=sample_post_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=403,
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products",
            method="POST",
            status_code=403,
            matched_status_key=None,
            is_success=False,
            assertions=[
                AssertionResult(name="status_code_match", passed=False, message="Undeclared 403")
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.UNEXPECTED_HTTP_STATUS
    assert FailureCategory.AUTHORIZATION_FAILURE in classification.secondary_categories
    assert classification.retry_guidance.policy == RetryPolicy.REQUIRES_MODIFICATION


def test_rate_limiting_429_retry_policy(sample_get_endpoint: EndpointDefinition):
    """429 Rate Limiting on idempotent GET yields SAFE_TO_RETRY with backoff."""
    state = create_initial_state(
        selected_operation=sample_get_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/1",
            status_code=429,
            is_success=True,
            headers={"retry-after": "15"},
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products/{product_id}",
            method="GET",
            status_code=429,
            matched_status_key=None,
            is_success=False,
            assertions=[
                AssertionResult(name="status_code_match", passed=False, message="Undeclared 429")
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.UNEXPECTED_HTTP_STATUS
    assert FailureCategory.RATE_LIMITING in classification.secondary_categories
    assert classification.retry_guidance.policy == RetryPolicy.SAFE_TO_RETRY
    assert classification.retry_guidance.suggested_backoff_seconds == 15.0


def test_unexpected_422_validation_failure(sample_post_endpoint: EndpointDefinition):
    """Undeclared 422 Unprocessable Entity classifies as UNEXPECTED_HTTP_STATUS with REQUEST_VALIDATION_FAILURE."""
    state = create_initial_state(
        selected_operation=sample_post_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=422,
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products",
            method="POST",
            status_code=422,
            matched_status_key=None,
            is_success=False,
            assertions=[
                AssertionResult(name="status_code_match", passed=False, message="Undeclared 422")
            ],
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.UNEXPECTED_HTTP_STATUS
    assert FailureCategory.REQUEST_VALIDATION_FAILURE in classification.secondary_categories
    assert classification.retry_guidance.policy == RetryPolicy.REQUIRES_MODIFICATION


def test_missing_request_config_at_execution_step():
    """MissingRequestConfigError at step 'executed' classifies as REQUEST_PREPARATION_FAILURE."""
    state = create_initial_state(
        status="error",
        error=AgentError(
            message="No ExecutionRequest found in state",
            error_type="MissingRequestConfigError",
            step="executed",
        ),
    )

    classification = FailureClassifier.classify(state)

    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.REQUEST_PREPARATION_FAILURE
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY


# ---------------------------------------------------------------------------
# Group 11: Fresh Execution State Contract (Correction 2)
# ---------------------------------------------------------------------------

def test_fresh_run_prior_successful_state_reused(
    sample_get_endpoint: EndpointDefinition, sample_spec: ParsedAPISpec
):
    """
    Contract Requirement: Prior successful state reused must NOT be accepted as a current
    success. Transient execution state is reset to idle/None, and classifying it before
    execution yields has_failure=True with UNKNOWN_FAILURE.
    """
    prior_state = create_initial_state(
        spec=sample_spec,
        selected_operation=sample_get_endpoint,
        target_path="/api/products/{product_id}",
        target_method="GET",
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/1",
            status_code=200,
            is_success=True,
            headers={"content-type": "application/json"},
            body={"id": 1, "name": "Test Product"},
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products/{product_id}",
            method="GET",
            status_code=200,
            matched_status_key="200",
            is_success=True,
            assertions=[AssertionResult(name="status_code_match", passed=True, message="Matched 200")],
        ),
        metadata={"base_url": "http://127.0.0.1:8081"},
    )

    fresh_state = create_fresh_run_state(prior_state)

    # 1. Active transient execution state is strictly reset
    assert fresh_state["status"] == "idle"
    assert fresh_state["error"] is None
    assert fresh_state["execution_result"] is None
    assert fresh_state["assertion_report"] is None

    # 2. Prior execution data is archived in metadata['history'] as a compact allowlisted record
    assert "history" in fresh_state["metadata"]
    assert len(fresh_state["metadata"]["history"]) == 1
    archived = fresh_state["metadata"]["history"][0]
    assert archived["status"] == "evaluated"
    assert archived["status_code"] == 200
    assert archived["method"] == "GET"
    assert archived["path_template"] == "/api/products/{product_id}"
    assert archived["passed_assertions"] == 1
    assert archived["failed_assertions"] == 0
    assert "execution_result" not in archived
    assert "assertion_report" not in archived

    # 3. Classifying unexecuted fresh state produces UNKNOWN_FAILURE, never prior success
    classification = FailureClassifier.classify(fresh_state)
    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.UNKNOWN_FAILURE
    assert "neither execution outcome nor structured error context" in classification.summary.lower()


def test_fresh_run_prior_failed_state_reused(sample_post_endpoint: EndpointDefinition):
    """
    Contract Requirement: Prior failed state reused must clear prior error and error status
    so that the fresh run starts clean and is not blocked by stale error envelopes.
    """
    prior_state = create_initial_state(
        selected_operation=sample_post_endpoint,
        target_path="/api/products",
        target_method="POST",
        status="error",
        error=AgentError(
            message="Connection refused to target host",
            error_type="NetworkError",
            step="executed",
            details={"errno": 111},
        ),
        metadata={"base_url": "http://127.0.0.1:8081"},
    )

    fresh_state = FailureClassifier.create_fresh_run_state(prior_state)

    # Prior error is completely cleared from active fields
    assert fresh_state["status"] == "idle"
    assert fresh_state["error"] is None
    assert fresh_state["execution_result"] is None
    assert fresh_state["assertion_report"] is None

    # Prior error is archived for audit/history in compact format
    assert len(fresh_state["metadata"]["history"]) == 1
    archived = fresh_state["metadata"]["history"][0]
    assert archived["status"] == "error"
    assert archived["error_type"] == "NetworkError"
    assert archived["error_step"] == "executed"
    assert "error" not in archived


def test_fresh_run_completes_evaluation_successfully(sample_get_endpoint: EndpointDefinition):
    """
    Contract Requirement: Fresh run completes execution and evaluation normally, and is
    classified as has_failure=False based strictly on the current run's successful assertions.
    """
    prior_state = create_initial_state(
        selected_operation=sample_get_endpoint,
        target_path="/api/products/{product_id}",
        target_method="GET",
        status="error",
        error=AgentError(message="Old failure", error_type="OldError", step="executed"),
    )

    fresh_state = create_fresh_run_state(prior_state)

    # Simulate fresh workflow execution and evaluation
    fresh_state["status"] = "evaluated"
    fresh_state["execution_result"] = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products/1",
        status_code=200,
        is_success=True,
        headers={"content-type": "application/json"},
    )
    fresh_state["assertion_report"] = AssertionReport(
        endpoint_path="/api/products/{product_id}",
        method="GET",
        status_code=200,
        matched_status_key="200",
        is_success=True,
        assertions=[
            AssertionResult(name="status_code_match", passed=True, message="Status matched 200"),
            AssertionResult(name="schema_match", passed=True, message="Schema matches"),
        ],
    )

    classification = FailureClassifier.classify(fresh_state)

    assert classification.has_failure is False
    assert classification.primary_category is None
    assert classification.secondary_categories == []
    assert classification.retry_guidance.policy == RetryPolicy.DO_NOT_RETRY
    assert "completed successfully" in classification.summary


def test_fresh_run_reusable_configuration_survives_initialization(sample_post_endpoint: EndpointDefinition):
    """
    Contract Requirement: Reusable configuration (spec, raw_spec, selected_operation,
    target_path, target_method, target_operation_id, request_config, and safe metadata)
    survives create_fresh_run_state, and caller-supplied overrides are cleanly applied.
    """
    from backend.app.schemas.spec import ParsedAPISpec
    dummy_spec = ParsedAPISpec(
        title="Test API",
        version="1.0.0",
        openapi_version="3.0.3",
        servers=["http://127.0.0.1:8081"],
        endpoints=[sample_post_endpoint],
    )
    req_config = ExecutionRequest(
        method="POST",
        url="http://127.0.0.1:8081/api/products",
        headers={"Content-Type": "application/json"},
        json_body={"name": "Widget"},
    )

    prior_state = create_initial_state(
        spec=dummy_spec,
        raw_spec={"openapi": "3.0.3"},
        selected_operation=sample_post_endpoint,
        target_path="/api/products",
        target_method="POST",
        target_operation_id="create_product",
        request_config=req_config,
        metadata={"base_url": "http://127.0.0.1:8081", "safe_dev_mode": True},
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=201,
            is_success=True,
        ),
    )

    fresh_state = create_fresh_run_state(prior_state, target_path="/api/products/v2")

    # Reusable configuration is preserved
    assert fresh_state["spec"] is dummy_spec
    assert fresh_state["raw_spec"] == {"openapi": "3.0.3"}
    assert fresh_state["selected_operation"] is sample_post_endpoint
    assert fresh_state["target_method"] == "POST"
    assert fresh_state["target_operation_id"] == "create_product"
    assert fresh_state["request_config"] is req_config
    assert fresh_state["metadata"]["base_url"] == "http://127.0.0.1:8081"
    assert fresh_state["metadata"]["safe_dev_mode"] is True

    # Override applied
    assert fresh_state["target_path"] == "/api/products/v2"

    # Transient execution fields reset
    assert fresh_state["status"] == "idle"
    assert fresh_state["error"] is None
    assert fresh_state["execution_result"] is None
    assert fresh_state["assertion_report"] is None


def test_fresh_run_no_previous_result_accepted_as_new_result(sample_post_endpoint: EndpointDefinition):
    """
    Contract Requirement: Active execution_result and assertion_report strictly reflect the
    new run and never the prior run. If the new run fails, it classifies strictly as the new failure.
    """
    prior_state = create_initial_state(
        selected_operation=sample_post_endpoint,
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/products",
            status_code=201,
            is_success=True,
            body={"id": 42, "name": "Created"},
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products",
            method="POST",
            status_code=201,
            matched_status_key="201",
            is_success=True,
            assertions=[AssertionResult(name="status_code_match", passed=True, message="Matched 201")],
        ),
    )

    fresh_state = create_fresh_run_state(prior_state)

    # New run produces a 400 Bad Request failure
    fresh_state["status"] = "evaluated"
    fresh_state["execution_result"] = ExecutionResult(
        method="POST",
        url="http://127.0.0.1:8081/api/products",
        status_code=400,
        is_success=True,
        headers={"content-type": "application/json"},
    )
    fresh_state["assertion_report"] = AssertionReport(
        endpoint_path="/api/products",
        method="POST",
        status_code=400,
        matched_status_key="400",
        is_success=False,
        assertions=[
            AssertionResult(name="status_code_match", passed=True, message="Matched 400"),
            AssertionResult(name="schema_match", passed=False, message="Missing error code"),
        ],
    )

    classification = FailureClassifier.classify(fresh_state)

    # Must classify based on current 400 failure, not prior 201 success
    assert classification.has_failure is True
    assert classification.primary_category == FailureCategory.RESPONSE_SCHEMA_MISMATCH
    assert classification.evidence.status_code == 400
    assert classification.evidence.matched_status_key == "400"

    # Prior 201 result is strictly isolated in compact history
    assert len(fresh_state["metadata"]["history"]) == 1
    assert fresh_state["metadata"]["history"][0]["status_code"] == 201
    assert fresh_state["metadata"]["history"][0]["method"] == "POST"
    assert fresh_state["metadata"]["history"][0]["passed_assertions"] == 1
    assert "execution_result" not in fresh_state["metadata"]["history"][0]


def test_fresh_run_rejects_transient_overrides(sample_get_endpoint: EndpointDefinition):
    """
    Contract Requirement: Caller attempts to override transient execution fields
    ('status', 'error', 'execution_result', 'assertion_report') must be rejected with ValueError.
    """
    prior_state = create_initial_state(selected_operation=sample_get_endpoint)

    forbidden_overrides = [
        {"status": "evaluated"},
        {"error": AgentError(message="Fake error", step="executed")},
        {"execution_result": ExecutionResult(method="GET", url="http://127.0.0.1:8081/api/products", is_success=True)},
        {"assertion_report": AssertionReport(endpoint_path="/api/products", method="GET", status_code=200, is_success=True)},
    ]

    for override_kwargs in forbidden_overrides:
        field_name = list(override_kwargs.keys())[0]
        with pytest.raises(ValueError, match=f"Cannot override transient execution field '{field_name}'"):
            create_fresh_run_state(prior_state, **override_kwargs)


def test_fresh_run_history_sanitization_excludes_secrets_and_bodies(
    sample_post_endpoint: EndpointDefinition, sample_spec: ParsedAPISpec
):
    """
    Contract Requirement: Compact history archiving must explicitly purge all secrets,
    raw request/response bodies, raw headers, auth tokens, cookies, assertion actuals, and details.
    """
    secret_pass = "MySuperSecretPass#2026!"
    secret_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.token999"
    leaked_ssn = "000-11-2222"

    prior_state = create_initial_state(
        spec=sample_spec,
        selected_operation=sample_post_endpoint,
        target_path="/api/products",
        target_method="POST",
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url=f"http://admin:{secret_pass}@127.0.0.1:8081/api/products?token={secret_token}",
            status_code=400,
            headers={
                "content-type": "application/json",
                "set-cookie": f"session_token={secret_token}; Path=/",
                "authorization": f"Bearer {secret_token}",
            },
            body={"error": "Password leaked", "secret": secret_pass, "ssn": leaked_ssn},
            latency_ms=25.4,
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products",
            method="POST",
            status_code=400,
            matched_status_key="400",
            is_success=False,
            assertions=[
                AssertionResult(
                    name="schema_match",
                    passed=False,
                    message="Schema constraint failed",
                    actual={"sensitive_payload": secret_pass, "ssn": leaked_ssn},
                )
            ],
        ),
        error=AgentError(
            message=f"Raw error with {secret_token}",
            error_type="ValidationFailed",
            step="executed",
            details={"debug_password": secret_pass},
        ),
    )

    fresh_state = create_fresh_run_state(prior_state)

    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    entry = history[0]

    # Verify safe allowlisted primitives only
    assert entry["status"] == "evaluated"
    assert entry["method"] == "POST"
    assert entry["path_template"] == "/api/products"
    assert entry["status_code"] == 400
    assert entry["latency_ms"] == 25.4
    assert entry["passed_assertions"] == 0
    assert entry["failed_assertions"] == 1
    assert entry["error_type"] == "ValidationFailed"
    assert entry["error_step"] == "executed"

    # Verify raw envelopes are completely absent
    assert "execution_result" not in entry
    assert "assertion_report" not in entry
    assert "error" not in entry
    assert "body" not in entry
    assert "headers" not in entry
    assert "actual" not in entry
    assert "details" not in entry

    # Verify JSON serialization contains zero secrets
    dumped = json.dumps(history)
    assert secret_pass not in dumped
    assert secret_token not in dumped
    assert leaked_ssn not in dumped
    assert "admin:" not in dumped
    assert "set-cookie" not in dumped.lower()
    assert "authorization" not in dumped.lower()


def test_fresh_run_history_json_serialization(sample_get_endpoint: EndpointDefinition):
    """
    Contract Requirement: State metadata['history'] must be directly JSON serializable
    with standard json.dumps without raising TypeError.
    """
    prior_state = create_initial_state(
        selected_operation=sample_get_endpoint,
        target_path="/api/products/{product_id}",
        target_method="GET",
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/1",
            status_code=200,
            latency_ms=10.5,
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/products/{product_id}",
            method="GET",
            status_code=200,
            is_success=True,
            assertions=[AssertionResult(name="status_code_match", passed=True, message="Matched 200")],
        ),
    )

    fresh_state = create_fresh_run_state(prior_state)

    # Standard json.dumps must succeed directly
    serialized = json.dumps(fresh_state["metadata"]["history"])
    deserialized = json.loads(serialized)
    assert isinstance(deserialized, list)
    assert len(deserialized) == 1
    assert deserialized[0]["status_code"] == 200


def test_fresh_run_three_consecutive_runs_history_ordering(sample_get_endpoint: EndpointDefinition):
    """
    Contract Requirement: Three consecutive runs accumulate compact history in correct
    chronological order [Run 1, Run 2, Run 3].
    """
    # Run 1
    state0 = create_initial_state(
        selected_operation=sample_get_endpoint,
        target_path="/api/products/1",
        target_method="GET",
        status="evaluated",
        execution_result=ExecutionResult(method="GET", url="http://127.0.0.1:8081/api/products/1", status_code=200, is_success=True),
        assertion_report=AssertionReport(endpoint_path="/api/products/1", method="GET", status_code=200, is_success=True),
    )
    state1 = create_fresh_run_state(state0)
    assert len(state1["metadata"]["history"]) == 1

    # Run 2
    state1["status"] = "evaluated"
    state1["execution_result"] = ExecutionResult(method="POST", url="http://127.0.0.1:8081/api/products", status_code=201, is_success=True)
    state1["assertion_report"] = AssertionReport(endpoint_path="/api/products", method="POST", status_code=201, is_success=True)
    state2 = create_fresh_run_state(state1)
    assert len(state2["metadata"]["history"]) == 2

    # Run 3
    state2["status"] = "error"
    state2["error"] = AgentError(message="Network drop", error_type="NetworkError", step="executed")
    state3 = create_fresh_run_state(state2)
    assert len(state3["metadata"]["history"]) == 3

    # Check chronological ordering
    h = state3["metadata"]["history"]
    assert h[0]["status_code"] == 200
    assert h[0]["method"] == "GET"
    assert h[1]["status_code"] == 201
    assert h[1]["method"] == "POST"
    assert h[2]["status"] == "error"
    assert h[2]["error_type"] == "NetworkError"


def test_fresh_run_history_capped_at_five_entries(sample_get_endpoint: EndpointDefinition):
    """
    Contract Requirement: History is strictly capped at the 5 most recent entries,
    preserving chronological ordering of the tail window.
    """
    state = create_initial_state(selected_operation=sample_get_endpoint)

    # Perform 7 sequential runs with distinct status codes 201, 202, 203, 204, 205, 206, 207
    for code in range(201, 208):
        state["status"] = "evaluated"
        state["execution_result"] = ExecutionResult(
            method="GET",
            url=f"http://127.0.0.1:8081/api/run/{code}",
            status_code=code,
            is_success=True,
        )
        state["assertion_report"] = AssertionReport(
            endpoint_path=f"/api/run/{code}",
            method="GET",
            status_code=code,
            is_success=True,
        )
        state = create_fresh_run_state(state)

    history = state["metadata"]["history"]
    assert len(history) == 5
    # The 5 most recent status codes must be 203, 204, 205, 206, 207 in chronological order
    expected_codes = [203, 204, 205, 206, 207]
    observed_codes = [entry["status_code"] for entry in history]
    assert observed_codes == expected_codes


def test_fresh_run_sanitizes_existing_unsafe_history(sample_get_endpoint: EndpointDefinition):
    """
    Contract Requirement: Existing history containing legacy raw Pydantic objects or
    unsafe keys (body, headers, tokens) is sanitized before being carried into the new state.
    """
    secret_token = "eyJhbGciOiJIUzI1NiJ9.legacy_leak_token"
    raw_unsafe_entry = {
        "status": "evaluated",
        "execution_result": ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/leak",
            status_code=200,
            headers={"authorization": f"Bearer {secret_token}"},
            body={"token": secret_token},
            is_success=True,
        ),
        "assertion_report": AssertionReport(
            endpoint_path="/api/leak",
            method="GET",
            status_code=200,
            is_success=True,
        ),
        "arbitrary_unsafe_field": "drop_me",
    }

    prior_state = create_initial_state(
        selected_operation=sample_get_endpoint,
        metadata={"history": [raw_unsafe_entry]},
    )

    fresh_state = create_fresh_run_state(prior_state)

    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    sanitized_entry = history[0]

    assert sanitized_entry["status"] == "evaluated"
    assert sanitized_entry["status_code"] == 200
    assert "arbitrary_unsafe_field" not in sanitized_entry
    assert "execution_result" not in sanitized_entry
    assert "body" not in sanitized_entry
    assert "headers" not in sanitized_entry

    dumped = json.dumps(history)
    assert secret_token not in dumped
    assert "drop_me" not in dumped


@pytest.mark.anyio
async def test_run_agent_workflow_invokes_graph_with_fresh_transient_state(sample_post_endpoint: EndpointDefinition):
    """
    Contract Requirement: run_agent_workflow() wrapper resets transient fields, archives
    prior state into compact history, invokes the compiled graph via ainvoke(), and returns
    the completed final workflow state.
    """
    from backend.app.schemas.spec import ParsedAPISpec
    dummy_spec = ParsedAPISpec(
        title="Test API",
        version="1.0.0",
        openapi_version="3.0.3",
        servers=["http://127.0.0.1:8081"],
        endpoints=[sample_post_endpoint],
    )

    # Prior state with dirty transient execution results and invalid operation ID to trigger error routing
    prior_state = create_initial_state(
        spec=dummy_spec,
        raw_spec={"openapi": "3.0.3"},
        status="evaluated",
        execution_result=ExecutionResult(method="POST", url="http://127.0.0.1:8081/api/products", status_code=201, is_success=True),
        assertion_report=AssertionReport(endpoint_path="/api/products", method="POST", status_code=201, is_success=True),
        target_operation_id="non_existent_operation_id",
        metadata={"base_url": "http://127.0.0.1:8081"},
    )

    final_state = await run_agent_workflow(prior_state)

    # Workflow must have started with clean transient state, proceeded to select_operation,
    # failed to find the operation, and routed to error (not inheriting the old 'evaluated' status)
    assert final_state["status"] == "error"
    assert final_state["error"] is not None
    assert final_state["error"].error_type == "OperationNotFoundError"

    # Prior run was cleanly archived into compact history
    assert len(final_state["metadata"]["history"]) == 1
    archived = final_state["metadata"]["history"][0]
    assert archived["status"] == "evaluated"
    assert archived["status_code"] == 201


def test_fresh_run_handles_malformed_legacy_history_safely(sample_get_endpoint: EndpointDefinition):
    """
    Contract Requirement: create_fresh_run_state() must never crash when processing
    malformed legacy history containing primitives, None, partial dicts, or non-model types
    inside execution envelopes. Malformed items must be safely skipped or converted.
    """
    malformed_history = [
        "primitive_string_entry",
        12345,
        True,
        None,
        [1, 2, "array_entry"],
        {"unrecognized_key": "some_value"},
        {},
        # Malformed legacy entry where execution_result and error are primitives instead of objects/dicts
        {
            "status": "evaluated",
            "execution_result": "malformed_string_not_an_exec_result",
            "assertion_report": 9999,
            "error": ["error_as_list"],
        },
        # Valid compact legacy entry
        {
            "status": "evaluated",
            "method": "GET",
            "path_template": "/api/products",
            "status_code": 200,
            "latency_ms": 15.2,
            "passed_assertions": 1,
            "failed_assertions": 0,
        },
    ]

    prior_state = create_initial_state(
        selected_operation=sample_get_endpoint,
        metadata={"history": malformed_history},
    )

    # Must execute cleanly without raising AttributeError, TypeError, or any exception
    fresh_state = create_fresh_run_state(prior_state)

    history = fresh_state["metadata"]["history"]
    # Primitives, None, lists, and empty/unrecognized dicts must be safely skipped.
    # The malformed legacy entry recovers status="evaluated" safely without crashing.
    # The valid compact entry is preserved.
    assert len(history) == 2

    # First preserved entry from malformed legacy entry: safe primitives only
    recovered_entry = history[0]
    assert recovered_entry["status"] == "evaluated"
    assert recovered_entry["method"] is None
    assert recovered_entry["status_code"] is None
    assert recovered_entry["error_type"] is None
    assert recovered_entry["error_step"] is None

    # Second preserved entry from valid compact entry
    valid_entry = history[1]
    assert valid_entry["status"] == "evaluated"
    assert valid_entry["method"] == "GET"
    assert valid_entry["path_template"] == "/api/products"
    assert valid_entry["status_code"] == 200
    assert valid_entry["latency_ms"] == 15.2


def test_fresh_run_enforces_string_length_limits(sample_get_endpoint: EndpointDefinition):
    """
    Contract Requirement: Stored strings in history are strictly clamped to MAX_STR_LEN (64)
    and MAX_PATH_LEN (256). Non-string objects passed into string fields are never stringified
    into representations (e.g. str(dict)), but safely set to None.
    """
    # 1. Test string length clamping on prior state archiving
    overly_long_error_type = "E" * 200
    overly_long_step = "S" * 200
    overly_long_status = "status_" + "x" * 100

    # Test with dictionary error containing arbitrary step length
    prior_state = create_initial_state(
        selected_operation=sample_get_endpoint,
        status=overly_long_status,
        error={
            "message": "Internal error",
            "error_type": overly_long_error_type,
            "step": overly_long_step,
        },
    )

    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    archived = history[0]

    assert len(archived["status"]) == 64
    assert archived["status"] == overly_long_status[:64]
    assert len(archived["error_type"]) == 64
    assert archived["error_type"] == overly_long_error_type[:64]
    assert len(archived["error_step"]) == 64
    assert archived["error_step"] == overly_long_step[:64]

    # Test with valid AgentError where error_type is clamped
    prior_state_agent_err = create_initial_state(
        selected_operation=sample_get_endpoint,
        error=AgentError(
            message="Internal error",
            error_type=overly_long_error_type,
            step="executed",
        ),
    )
    fresh_state_agent_err = create_fresh_run_state(prior_state_agent_err)
    archived_agent_err = fresh_state_agent_err["metadata"]["history"][0]
    assert len(archived_agent_err["error_type"]) == 64
    assert archived_agent_err["error_type"] == overly_long_error_type[:64]
    assert archived_agent_err["error_step"] == "executed"

    # 2. Test clamping and type validation via existing history sanitization
    long_path = "/api/items/" + "p" * 500
    raw_history_with_long_strings = [
        {
            "status": "evaluated",
            "method": "CUSTOMMETHODNAME_EXTRALONG",
            "path_template": long_path,
            "status_code": 200,
            # Non-string types must NOT be stringified to arbitrary representations
            "error_type": {"nested_key": "secret_dump"},
            "error_step": ["executed", "step"],
        }
    ]

    prior_state2 = create_initial_state(
        selected_operation=sample_get_endpoint,
        metadata={"history": raw_history_with_long_strings},
    )

    fresh_state2 = create_fresh_run_state(prior_state2)
    history2 = fresh_state2["metadata"]["history"]
    assert len(history2) == 1
    sanitized = history2[0]

    assert len(sanitized["path_template"]) == 256
    assert sanitized["path_template"] == long_path[:256]
    assert len(sanitized["method"]) == 16
    assert sanitized["method"] == "CUSTOMMETHODNAME"
    # Non-string types safely rejected, not converted to "{'nested_key': ...}"
    assert sanitized["error_type"] is None
    assert sanitized["error_step"] is None

    dumped = json.dumps(history2)
    assert "secret_dump" not in dumped


def test_fresh_run_prevents_concrete_path_leakage_when_openapi_template_missing():
    """
    Contract Requirement: In history entries, path_template must come ONLY from verified
    OpenAPI endpoint templates or assertion reports. When an OpenAPI template is unavailable,
    it must NOT fall back to target_path or raw URLs that could contain concrete IDs, query
    parameters, tokens, or sensitive runtime values. If unavailable, path_template must be None.
    """
    secret_token = "eyJhbGciOiJIUzI1NiJ9.leak_token_in_query"
    concrete_id = "999-88-7777"
    raw_sensitive_url = f"http://127.0.0.1:8081/api/customers/{concrete_id}?token={secret_token}&role=admin"
    raw_sensitive_target = f"/api/customers/{concrete_id}?token={secret_token}"

    prior_state = create_initial_state(
        selected_operation=None,  # No verified OpenAPI endpoint definition
        target_path=raw_sensitive_target,
        target_method="GET",
        status="executed",
        execution_result=ExecutionResult(
            method="GET",
            url=raw_sensitive_url,
            status_code=200,
            latency_ms=18.5,
            is_success=True,
        ),
        assertion_report=None,  # No assertion report with verified endpoint_path
    )

    fresh_state = create_fresh_run_state(prior_state)

    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    entry = history[0]

    # Crucial security guarantee: path_template is strictly None, NOT raw URL or target_path
    assert entry["path_template"] is None
    assert entry["status"] == "executed"
    assert entry["method"] == "GET"
    assert entry["status_code"] == 200

    # Ensure sensitive concrete values never leak into serialized history
    serialized = json.dumps(history)
    assert secret_token not in serialized
    assert concrete_id not in serialized
    assert "role=admin" not in serialized
    assert "/api/customers" not in serialized


def test_fresh_run_path_template_from_verified_sources_only(
    sample_get_endpoint: EndpointDefinition,
    sample_spec: ParsedAPISpec,
):
    """
    Contract Requirement: path_template is safely populated when verified OpenAPI
    sources exist in spec.endpoints (from selected_operation or assertion_report with passed lookup).
    """
    # Source A: from selected_operation verified against spec
    state_a = create_initial_state(
        spec=sample_spec,
        selected_operation=sample_get_endpoint,
        status="executed",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/products/42",
            status_code=200,
            is_success=True,
        ),
    )
    fresh_a = create_fresh_run_state(state_a)
    assert fresh_a["metadata"]["history"][0]["path_template"] == "/api/products/{product_id}"

    # Source B: from assertion_report when selected_operation is None, with passed lookup in spec
    orders_ep = EndpointDefinition(
        path="/api/orders/{order_id}",
        method="POST",
        operation_id="create_order",
        summary="Create Order",
        responses={"201": ResponseDefinition(status_code="201", description="Created")},
    )
    orders_spec = ParsedAPISpec(
        title="Orders API",
        version="1.0.0",
        openapi_version="3.0.3",
        servers=["http://127.0.0.1:8081"],
        endpoints=[orders_ep],
    )
    state_b = create_initial_state(
        spec=orders_spec,
        selected_operation=None,
        status="evaluated",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/orders/99",
            status_code=201,
            is_success=True,
        ),
        assertion_report=AssertionReport(
            endpoint_path="/api/orders/{order_id}",
            method="POST",
            status_code=201,
            is_success=True,
            assertions=[AssertionResult(name="endpoint_lookup", passed=True, message="Found in spec")],
        ),
    )
    fresh_b = create_fresh_run_state(state_b)
    assert fresh_b["metadata"]["history"][0]["path_template"] == "/api/orders/{order_id}"


def test_clean_path_template_syntax_boundaries():
    """
    Contract Requirement: _clean_path_template() accepts only valid OpenAPI route
    templates beginning with '/', and strictly rejects URLs with '://', query strings '?',
    fragments '#', missing leading slashes, and non-strings.
    """
    clean = FailureClassifier._clean_path_template

    # 1. Valid OpenAPI templates
    assert clean("/api/products") == "/api/products"
    assert clean("/api/products/{product_id}") == "/api/products/{product_id}"
    assert clean("/users/{user_id}/orders/{order_id}") == "/users/{user_id}/orders/{order_id}"

    # 2. Full URLs with schemes/credentials
    assert clean("http://127.0.0.1:8081/api/products") is None
    assert clean("https://api.example.com/api/products/{id}") is None
    assert clean("http://admin:secret@127.0.0.1:8081/api/leak") is None
    assert clean("ftp://files.example.com/path") is None

    # 3. Query strings and tokens
    assert clean("/api/products?token=secret123") is None
    assert clean("/api/products?page=1&size=20") is None
    assert clean("/api/customers/42?auth=bearer") is None

    # 4. URI fragments
    assert clean("/api/products#section") is None
    assert clean("/api/products/{id}#details") is None

    # 5. Missing leading slash
    assert clean("api/products") is None
    assert clean("products/{id}") is None

    # 6. Non-string types, None, and empty/whitespace
    assert clean(None) is None
    assert clean(12345) is None
    assert clean({"path": "/api/products"}) is None
    assert clean(["/api/products"]) is None
    assert clean("   ") is None
    assert clean("") is None


def test_fresh_run_rejects_failed_endpoint_lookup():
    """
    Contract Requirement: When an assertion report contains a failed endpoint_lookup
    assertion (signifying the operation was not declared in the OpenAPI specification),
    path_template must strictly be None, even if assertion_report.endpoint_path contains a string.
    """
    failed_lookup_report = AssertionReport(
        endpoint_path="/api/customers/999-88-7777",
        method="GET",
        status_code=404,
        matched_status_key=None,
        is_success=False,
        assertions=[
            AssertionResult(
                name="endpoint_lookup",
                passed=False,
                expected="GET /api/customers/999-88-7777",
                actual="Not found in specification",
                message="Operation GET /api/customers/999-88-7777 is not defined in the OpenAPI specification.",
            )
        ],
    )

    prior_state = create_initial_state(
        selected_operation=None,
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url="http://127.0.0.1:8081/api/customers/999-88-7777",
            status_code=404,
            is_success=True,
        ),
        assertion_report=failed_lookup_report,
    )

    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    entry = history[0]

    # Crucial security guarantee: failed endpoint_lookup must result in path_template=None
    assert entry["path_template"] is None
    assert entry["status"] == "evaluated"
    assert entry["status_code"] == 404
    assert entry["method"] == "GET"


def test_fresh_run_rejects_malicious_selected_operation_dictionary():
    """
    Contract Requirement: Arbitrary dictionaries supplied as selected_operation must
    not be blindly trusted. path_template must come only from verified OpenAPI
    EndpointDefinition or spec-validated endpoints.
    """
    # 1. Malicious dictionary with injected path
    malicious_dict_state = create_initial_state(
        selected_operation={"path": "/api/injected/admin", "method": "POST"},
        status="executed",
        execution_result=ExecutionResult(
            method="POST",
            url="http://127.0.0.1:8081/api/injected/admin",
            status_code=200,
            is_success=True,
        ),
    )
    fresh_state_1 = create_fresh_run_state(malicious_dict_state)
    assert fresh_state_1["metadata"]["history"][0]["path_template"] is None

    # 2. Malicious dictionary with full URL and query secret
    malicious_url_dict_state = create_initial_state(
        selected_operation={"path": "http://evil.com/leak?token=stolen", "method": "GET"},
        status="executed",
        execution_result=ExecutionResult(
            method="GET",
            url="http://evil.com/leak?token=stolen",
            status_code=200,
            is_success=True,
        ),
    )
    fresh_state_2 = create_fresh_run_state(malicious_url_dict_state)
    assert fresh_state_2["metadata"]["history"][0]["path_template"] is None

    dumped = json.dumps(fresh_state_2["metadata"]["history"])
    assert "evil.com" not in dumped
    assert "stolen" not in dumped


def test_fresh_run_rejects_urls_with_credentials_query_tokens_and_fragments():
    """
    Contract Requirement: assertion_report.endpoint_path containing full URLs with
    credentials, query tokens, or fragments must be rejected by _clean_path_template,
    resulting in path_template=None and zero secret leakage in history.
    """
    secret_pass = "super_secret_pwd_99"
    token_val = "eyJhbGciOiJIUzI1NiJ9.leak_token"
    raw_leak_url = f"http://admin:{secret_pass}@127.0.0.1:8081/api/customers/42?token={token_val}#top_frag"

    leak_report = AssertionReport(
        endpoint_path=raw_leak_url,
        method="GET",
        status_code=200,
        is_success=True,
    )

    prior_state = create_initial_state(
        selected_operation=None,
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url=raw_leak_url,
            status_code=200,
            is_success=True,
        ),
        assertion_report=leak_report,
    )

    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    entry = history[0]

    assert entry["path_template"] is None
    assert entry["status"] == "evaluated"
    assert entry["status_code"] == 200

    # Ensure zero sensitive strings leak into serialized history
    serialized = json.dumps(history)
    assert secret_pass not in serialized
    assert token_val not in serialized
    assert "top_frag" not in serialized
    assert "http" not in serialized
    assert "admin" not in serialized


def test_fresh_run_sanitizes_legacy_history_malicious_path_templates(sample_get_endpoint: EndpointDefinition):
    """
    Contract Requirement: _sanitize_history_entry() must clean legacy compact history
    entries and strip invalid/malicious path_template values (URLs, query tokens, fragments).
    Valid OpenAPI templates must be preserved.
    """
    raw_history = [
        {"status": "evaluated", "path_template": "http://evil.com/leak?secret=1", "status_code": 200},
        {"status": "evaluated", "path_template": "/api/users/999?token=secret", "status_code": 200},
        {"status": "evaluated", "path_template": "/api/products#section", "status_code": 200},
        {"status": "evaluated", "path_template": "api/no_leading_slash", "status_code": 200},
        {"status": "evaluated", "path_template": "/api/products/{product_id}", "status_code": 200},
    ]

    prior_state = create_initial_state(
        selected_operation=sample_get_endpoint,
        metadata={"history": raw_history},
    )

    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 5

    # Entries 0-3 must have path_template sanitized to None
    assert history[0]["path_template"] is None
    assert history[1]["path_template"] is None
    assert history[2]["path_template"] is None
    assert history[3]["path_template"] is None

    # Entry 4 (valid OpenAPI template) must be preserved
    assert history[4]["path_template"] == "/api/products/{product_id}"

    serialized = json.dumps(history)
    assert "evil.com" not in serialized
    assert "token=secret" not in serialized
    assert "no_leading_slash" not in serialized


def test_provenance_rejects_concrete_id_in_assertion_report_even_when_lookup_passes(sample_spec: ParsedAPISpec):
    """
    Contract Requirement: A concrete customer ID in assertion_report.endpoint_path
    must be rejected even when endpoint_lookup passed, because the concrete path
    does not match any route template in spec.endpoints.
    """
    concrete_id = "999-88-7777"
    concrete_path = f"/api/customers/{concrete_id}"
    report = AssertionReport(
        endpoint_path=concrete_path,
        method="GET",
        status_code=200,
        is_success=True,
        assertions=[AssertionResult(name="endpoint_lookup", passed=True, message="Lookup OK")],
    )
    prior_state = create_initial_state(
        spec=sample_spec,
        selected_operation=None,
        status="evaluated",
        execution_result=ExecutionResult(method="GET", url=f"http://test{concrete_path}", status_code=200, is_success=True),
        assertion_report=report,
    )
    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    assert history[0]["path_template"] is None
    serialized = json.dumps(history)
    assert concrete_id not in serialized


def test_provenance_rejects_unlisted_path_even_when_lookup_passes(sample_spec: ParsedAPISpec):
    """
    Contract Requirement: An unlisted path in assertion_report.endpoint_path
    must be rejected even when endpoint_lookup passed, because it does not exist in spec.endpoints.
    """
    unlisted_path = "/api/fake/unlisted/route"
    report = AssertionReport(
        endpoint_path=unlisted_path,
        method="GET",
        status_code=200,
        is_success=True,
        assertions=[AssertionResult(name="endpoint_lookup", passed=True, message="Lookup OK")],
    )
    prior_state = create_initial_state(
        spec=sample_spec,
        selected_operation=None,
        status="evaluated",
        execution_result=ExecutionResult(method="GET", url=f"http://test{unlisted_path}", status_code=200, is_success=True),
        assertion_report=report,
    )
    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    assert history[0]["path_template"] is None


def test_provenance_rejects_missing_endpoint_lookup_in_assertion_report(sample_spec: ParsedAPISpec):
    """
    Contract Requirement: When assertion_report lacks an explicit endpoint_lookup assertion
    (missing lookup), assertion_report.endpoint_path must be rejected (path_template=None).
    """
    report = AssertionReport(
        endpoint_path="/api/products/{product_id}",
        method="GET",
        status_code=200,
        is_success=True,
        # No endpoint_lookup assertion present in assertions list
        assertions=[AssertionResult(name="status_code_match", passed=True, message="Matched 200")],
    )
    prior_state = create_initial_state(
        spec=sample_spec,
        selected_operation=None,
        status="evaluated",
        execution_result=ExecutionResult(method="GET", url="http://test/api/products/1", status_code=200, is_success=True),
        assertion_report=report,
    )
    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    assert history[0]["path_template"] is None


def test_provenance_rejects_failed_endpoint_lookup_in_assertion_report(sample_spec: ParsedAPISpec):
    """
    Contract Requirement: When assertion_report contains a failed endpoint_lookup assertion,
    assertion_report.endpoint_path must strictly result in path_template=None.
    """
    report = AssertionReport(
        endpoint_path="/api/products/{product_id}",
        method="GET",
        status_code=404,
        is_success=False,
        assertions=[AssertionResult(name="endpoint_lookup", passed=False, message="Not found")],
    )
    prior_state = create_initial_state(
        spec=sample_spec,
        selected_operation=None,
        status="evaluated",
        execution_result=ExecutionResult(method="GET", url="http://test/api/products/999", status_code=404, is_success=True),
        assertion_report=report,
    )
    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    assert history[0]["path_template"] is None


def test_provenance_rejects_arbitrary_endpoint_definition_with_malicious_path_not_in_spec(sample_spec: ParsedAPISpec):
    """
    Contract Requirement: Do not trust an EndpointDefinition solely because of its Python type.
    An arbitrary EndpointDefinition constructed with a path not matching spec.endpoints must be rejected.
    """
    malicious_id = "000-99-8888"
    injected_ep = EndpointDefinition(
        path=f"/api/admin/secrets/{malicious_id}",
        method="GET",
        operation_id="leak_secrets",
        summary="Injected",
        responses={},
    )
    prior_state = create_initial_state(
        spec=sample_spec,
        selected_operation=injected_ep,
        status="executed",
        execution_result=ExecutionResult(method="GET", url=f"http://test/api/admin/secrets/{malicious_id}", status_code=200, is_success=True),
    )
    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    assert history[0]["path_template"] is None
    serialized = json.dumps(history)
    assert malicious_id not in serialized


def test_provenance_preserves_genuine_endpoint_from_parsed_spec(
    sample_get_endpoint: EndpointDefinition, sample_spec: ParsedAPISpec
):
    """
    Contract Requirement: A genuine EndpointDefinition whose path and method match
    an entry in spec.endpoints is safely verified and preserved in path_template.
    """
    prior_state = create_initial_state(
        spec=sample_spec,
        selected_operation=sample_get_endpoint,
        status="executed",
        execution_result=ExecutionResult(method="GET", url="http://test/api/products/42", status_code=200, is_success=True),
    )
    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    assert history[0]["path_template"] == "/api/products/{product_id}"


def test_provenance_rejects_method_mismatches(sample_spec: ParsedAPISpec):
    """
    Contract Requirement: When an endpoint path exists in spec.endpoints but the method
    does not match, path_template must be rejected (path_template=None).
    """
    # /api/products/{product_id} is defined ONLY for GET in sample_spec, not DELETE
    mismatched_ep = EndpointDefinition(
        path="/api/products/{product_id}",
        method="DELETE",
        operation_id="delete_product",
        summary="Delete Product",
        responses={},
    )
    prior_state = create_initial_state(
        spec=sample_spec,
        selected_operation=mismatched_ep,
        status="executed",
        execution_result=ExecutionResult(method="DELETE", url="http://test/api/products/1", status_code=204, is_success=True),
    )
    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    assert history[0]["path_template"] is None


def test_provenance_ensures_zero_sensitive_values_enter_serialized_history(sample_spec: ParsedAPISpec):
    """
    Contract Requirement: Multi-vector adversarial payload with credentials, query tokens,
    and sensitive parameters across selected_operation, assertion_report, and execution_result
    must result in path_template=None and zero secret leakage in serialized JSON.
    """
    secret_pass = "UltimateMasterSecret#2026"
    secret_jwt = "eyJhbGciOiJIUzI1NiJ9.leak_jwt_secret"
    customer_ssn = "123-45-6789"
    adversarial_url = f"http://admin:{secret_pass}@127.0.0.1:8081/api/customers/{customer_ssn}?token={secret_jwt}#admin_frag"

    report = AssertionReport(
        endpoint_path=adversarial_url,
        method="GET",
        status_code=200,
        is_success=True,
        assertions=[AssertionResult(name="endpoint_lookup", passed=True, message="OK")],
    )

    prior_state = create_initial_state(
        spec=sample_spec,
        selected_operation={"path": adversarial_url, "method": "GET"},
        target_path=f"/api/customers/{customer_ssn}",
        target_method="GET",
        status="evaluated",
        execution_result=ExecutionResult(
            method="GET",
            url=adversarial_url,
            status_code=200,
            headers={"authorization": f"Bearer {secret_jwt}"},
            body={"ssn": customer_ssn, "pass": secret_pass},
            is_success=True,
        ),
        assertion_report=report,
        error=AgentError(message=f"Leak {secret_jwt}", error_type="AuthError", step="executed", details={"ssn": customer_ssn}),
    )

    fresh_state = create_fresh_run_state(prior_state)
    history = fresh_state["metadata"]["history"]
    assert len(history) == 1
    assert history[0]["path_template"] is None

    serialized = json.dumps(history)
    assert secret_pass not in serialized
    assert secret_jwt not in serialized
    assert customer_ssn not in serialized
    assert "admin_frag" not in serialized
    assert "authorization" not in serialized.lower()



