"""
Unit tests for ReportDataSanitizer, Immutability, Report Consistency Invariants,
and Secure Markdown Serialization (Phase 4C).
"""

import math
import pytest
from pydantic import ValidationError

from backend.app.schemas.failure import FailureCategory, FailureSeverity, RetryPolicy
from backend.app.schemas.report import (
    AttemptOutcomeSummary,
    DetachedInvestigationRecord,
    DetachedRetrievedEvidenceItem,
    DiagnosticReport,
    FollowupDecisionAuditRecord,
    OperationRunRecord,
    OverallRunStatus,
    RemediationRecommendation,
    RunSummaryStatistics,
    TransientRetryRecord,
    VariationCandidateRecord,
    VariationMetricsSummary,
    VariationTestRecord,
)
from backend.app.schemas.retest import ParameterPerturbation, PerturbationStrategy, RetestAction
from backend.app.schemas.sanitizer import ReportDataSanitizer
from backend.app.services.report_generator import DiagnosticReportGenerator


# ---------------------------------------------------------------------------
# 1. Sanitizer Unit Tests
# ---------------------------------------------------------------------------

def test_sanitizer_secret_assignments():
    """Confirms secret assignment patterns are redacted without leaving dangling quotes."""
    tests = {
        "password=my_secret_pass": "password=[REDACTED]",
        "password: my_secret_pass": "password:[REDACTED]",
        "api_key: 'my_api_key_123'": "api_key:[REDACTED]",
        'api_key = "my secret key with spaces"': "api_key=[REDACTED]",
        "access_token=xyz123, other=456": "access_token=[REDACTED], other=456",
        "credential: 'super secret'; next=val": "credential:[REDACTED]; next=val",
        "auth=secret&other=safe": "auth=[REDACTED]&other=safe",
    }
    for raw, expected in tests.items():
        assert ReportDataSanitizer.sanitize_text(raw) == expected


def test_sanitizer_bearer_jwt_cookies():
    """Confirms Bearer tokens, JWTs, and full cookie header lines are masked."""
    # Bearer
    assert ReportDataSanitizer.sanitize_text("Authorization: Bearer secret-token-123") == "Authorization: Bearer [REDACTED]"
    assert ReportDataSanitizer.sanitize_text("bearer token_xyz") == "Bearer [REDACTED]"

    # JWT
    raw_jwt = "token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgN_p_n123"
    assert ReportDataSanitizer.sanitize_text(raw_jwt) == "token: [REDACTED_JWT]"

    # Multiple cookies
    raw_cookie = "Cookie: session_id=abc123; tracking=xyz; user=admin"
    assert ReportDataSanitizer.sanitize_text(raw_cookie) == "Cookie: [REDACTED]"


def test_sanitizer_negative_cases():
    """Confirms non-secret words and ordinary keys are not redacted."""
    safe_strings = [
        "username=john_doe",
        "operation_id=get_products",
        "status=200",
        "path=/api/v1/users",
        "tokenization=standard",
        "author=Jane Doe",
        "bearing=180",
        "baking cookies is fun",
    ]
    for s in safe_strings:
        assert ReportDataSanitizer.sanitize_text(s) == s


def test_sanitizer_url_parsing_and_guards():
    """Confirms invalid ports, port 0, missing hosts, and IPv6 reconstruction."""
    # Malformed port
    assert ReportDataSanitizer.sanitize_url("http://api.local:invalid/path") is None
    # Port 0
    assert ReportDataSanitizer.sanitize_url("http://api.local:0") is None
    # Port out of range
    assert ReportDataSanitizer.sanitize_url("http://api.local:65536") is None
    # Missing hostname
    assert ReportDataSanitizer.sanitize_url("http:///path") is None
    # IPv6 reconstruction with brackets
    clean_ipv6 = ReportDataSanitizer.sanitize_url("http://[2001:db8::1]:8080/path?token=secret123&user=john")
    assert clean_ipv6 == "http://[2001:db8::1]:8080/path?token=%5BREDACTED%5D&user=john"
    # Unsafe schemes
    assert ReportDataSanitizer.sanitize_url("javascript:alert(1)") is None
    assert ReportDataSanitizer.sanitize_url("file:///etc/passwd") is None


def test_sanitizer_source_references():
    """Confirms relative references are preserved while traversal and unsafe schemes return None."""
    # Preserved relative references (Phase 4B)
    assert ReportDataSanitizer.sanitize_source_reference("paths./items.get") == "paths./items.get"
    assert ReportDataSanitizer.sanitize_source_reference("components.schemas.Item") == "components.schemas.Item"
    assert ReportDataSanitizer.sanitize_source_reference("docs/auth.md#section") == "docs/auth.md#section"

    # Preserved valid HTTP URL
    assert ReportDataSanitizer.sanitize_source_reference("https://api.example.com/docs") == "https://api.example.com/docs"

    # Suppressed traversal and leading slashes (returns None, zero sentinels)
    assert ReportDataSanitizer.sanitize_source_reference("../../etc/passwd") is None
    assert ReportDataSanitizer.sanitize_source_reference("/etc/passwd") is None
    assert ReportDataSanitizer.sanitize_source_reference("..\\windows\\win.ini") is None
    assert ReportDataSanitizer.sanitize_source_reference("%2e%2e/secret") is None

    # Suppressed dangerous schemes
    assert ReportDataSanitizer.sanitize_source_reference("javascript:evil()") is None
    assert ReportDataSanitizer.sanitize_source_reference("data:text/html,evil") is None


# ---------------------------------------------------------------------------
# 2. Immutability & Validation Tests
# ---------------------------------------------------------------------------

def test_models_are_frozen():
    """Confirms report models reject attribute mutation."""
    summary = AttemptOutcomeSummary(dispatched=True, passed=True, status_code=200)
    with pytest.raises(ValidationError):
        summary.passed = False  # Frozen model raises ValidationError on assignment


def test_tuples_reject_mutation():
    """Confirms tuple collections reject append and item assignment."""
    op = OperationRunRecord(
        http_method="GET",
        path_template="/api/items",
        initial_request_fingerprint="a" * 64,
        initial_attempt=AttemptOutcomeSummary(dispatched=True, passed=True, status_code=200),
        final_status=OverallRunStatus.PASSED,
        final_outcome=AttemptOutcomeSummary(dispatched=True, passed=True, status_code=200),
        transient_retries=tuple(),
    )
    with pytest.raises(AttributeError):
        op.transient_retries.append("fake")  # tuple has no attribute 'append'


def test_evidence_untrusted_flag_permanently_true():
    """Confirms is_untrusted_evidence cannot be overridden to False."""
    ev = DetachedRetrievedEvidenceItem(
        chunk_id="chunk_1",
        document_id="doc_1",
        source_type="openapi_spec",
        source_ref="paths./items.get",
        relevance_score=0.9,
        distance=0.1,
        content="some documentation",
        is_untrusted_evidence=False,  # Attempting to override to False
    )
    assert ev.is_untrusted_evidence is True


# ---------------------------------------------------------------------------
# 3. Report Consistency Invariants Tests
# ---------------------------------------------------------------------------

def test_report_consistency_validates_counts():
    """Confirms DiagnosticReportGenerator.build_report produces a validated report."""
    op1 = OperationRunRecord(
        http_method="GET",
        path_template="/api/items",
        initial_request_fingerprint="a" * 64,
        initial_attempt=AttemptOutcomeSummary(dispatched=True, passed=True, status_code=200),
        final_status=OverallRunStatus.PASSED,
        final_outcome=AttemptOutcomeSummary(dispatched=True, passed=True, status_code=200),
    )
    report = DiagnosticReportGenerator.build_report(
        operations=[op1],
        api_title="Test API",
        api_version="1.0.0",
        base_url="http://api.local",
    )
    assert report.summary.total_operations == 1
    assert report.summary.passed_operations == 1
    assert report.summary.pass_rate_percentage == 100.0


def test_report_consistency_rejects_category_mismatch():
    """Confirms tampering with summary counts raises ValueError in root validator."""
    op1 = OperationRunRecord(
        http_method="GET",
        path_template="/api/items",
        initial_request_fingerprint="a" * 64,
        initial_attempt=AttemptOutcomeSummary(dispatched=True, passed=True, status_code=200),
        final_status=OverallRunStatus.PASSED,
        final_outcome=AttemptOutcomeSummary(dispatched=True, passed=True, status_code=200),
    )
    report = DiagnosticReportGenerator.build_report(
        operations=[op1],
        api_title="Test API",
        api_version="1.0.0",
    )
    # Attempt to construct report with mismatched passed_operations
    bad_summary = report.summary.model_copy(update={"passed_operations": 0})
    with pytest.raises(ValidationError) as exc_info:
        DiagnosticReport(
            report_id="rep_1",
            timestamp="2026-11-06T00:00:00Z",
            api_title="Test API",
            api_version="1.0.0",
            summary=bad_summary,
            operations=(op1,),
        )
    assert "passed_operations mismatch" in str(exc_info.value)


def test_report_consistency_reconciles_candidate_metrics():
    """Confirms mutually exclusive candidate rejection partition reconciles exactly."""
    cand1 = VariationCandidateRecord(
        candidate_id="cand_1",
        strategy=PerturbationStrategy.BOUNDARY_CLAMP,
        target_parameter="limit",
        parameter_location="query",
        applicable_constraint="min=1",
        schema_validation_passed=False,
        authorization_approved=True,
        budget_approved=True,
        approved_for_dispatch=False,
        dispatched=False,
        rejection_reason="SchemaInvalid",
    )
    cand2 = VariationCandidateRecord(
        candidate_id="cand_2",
        strategy=PerturbationStrategy.BOUNDARY_CLAMP,
        target_parameter="limit",
        parameter_location="query",
        applicable_constraint="min=1",
        canonical_request_fingerprint="b" * 64,
        schema_validation_passed=True,
        authorization_approved=True,
        budget_approved=True,
        approved_for_dispatch=True,
        dispatched=True,
        rejection_reason=None,
    )
    vtest2 = VariationTestRecord(
        variation_number=1,
        candidate_id="cand_2",
        canonical_request_fingerprint="b" * 64,
        perturbations_applied=(
            ParameterPerturbation(
                parameter_name="limit",
                parameter_location="query",
                varied_value=1,
                constraint_applied="min=1",
                pre_execution_validated=True,
            ),
        ),
        is_non_idempotent_mutation=False,
        outcome=AttemptOutcomeSummary(dispatched=True, passed=True, status_code=200),
    )
    dec1 = FollowupDecisionAuditRecord(
        followup_number=1,
        action=RetestAction.RUN_VARIATION_TEST,
        target_candidate_id="cand_2",
        eligibility_policy=RetryPolicy.REQUIRES_MODIFICATION,
        reason_category="dispatched_variation_test",
        sanitized_reason="Dispatched candidate 2",
        dispatched=True,
    )

    op = OperationRunRecord(
        http_method="GET",
        path_template="/api/items",
        initial_request_fingerprint="a" * 64,
        initial_attempt=AttemptOutcomeSummary(dispatched=True, passed=False, status_code=400),
        followup_decisions=(dec1,),
        variation_candidates=(cand1, cand2),
        variation_tests=(vtest2,),
        final_status=OverallRunStatus.FAILED,  # Variation success does NOT change operation status
        final_outcome=AttemptOutcomeSummary(dispatched=True, passed=False, status_code=400),
    )

    report = DiagnosticReportGenerator.build_report(
        operations=[op],
        api_title="Test API",
        api_version="1.0.0",
    )
    vm = report.summary.variation_metrics
    assert vm.candidates_generated == 2
    assert vm.candidates_schema_valid == 1
    assert vm.candidates_rejected == 1
    assert vm.candidates_approved == 1
    assert vm.candidates_dispatched == 1
    assert vm.rejections_schema_invalid == 1
    assert vm.variation_tests_passed == 1
    assert report.summary.pass_rate_percentage == 0.0  # Operation pass rate remains 0.0%


def test_empty_suite_consistency():
    """Confirms empty suite (0 operations) validates cleanly without division by zero."""
    report = DiagnosticReportGenerator.build_report(
        operations=[],
        api_title="Empty API",
        api_version="1.0.0",
    )
    assert report.summary.total_operations == 0
    assert report.summary.pass_rate_percentage == 0.0
    assert report.summary.variation_metrics.variation_success_rate_percentage == 0.0


# ---------------------------------------------------------------------------
# 4. Markdown Serialization Tests
# ---------------------------------------------------------------------------

def test_markdown_avoids_tables_and_escapes_inline():
    """Confirms Markdown output avoids dynamic tables and escapes active markdown/HTML."""
    attempt = AttemptOutcomeSummary(
        dispatched=True,
        passed=False,
        status_code=400,
        failed_assertions=("Assertion with **bold** and *italic* and <img src=x>",),
    )
    op = OperationRunRecord(
        http_method="GET",
        path_template="/api/<script>alert(1)</script>/|pipe|",
        initial_request_fingerprint="a" * 64,
        initial_attempt=attempt,
        final_status=OverallRunStatus.FAILED,
        final_outcome=attempt,
    )
    report = DiagnosticReportGenerator.build_report(
        operations=[op],
        api_title="Security API",
        api_version="1.0.0",
    )
    md = DiagnosticReportGenerator.to_markdown(report)

    # Asserts dynamic tables with pipes are not used for user content
    assert "| /api/" not in md
    # Asserts script tags are escaped
    assert "<script>" not in md
    assert "&lt;script&gt;" in md
    # Asserts img tag escaped
    assert "<img" not in md
    assert "&lt;img" in md


def test_markdown_fences_multiline_evidence_and_prefixes_quotes():
    """Confirms multiline evidence expands code fences and prefixes every line with >."""
    evidence_content = "```markdown\n# Injected Heading\n```\nSome untrusted evidence"
    evidence_item = DetachedRetrievedEvidenceItem(
        chunk_id="chunk_1",
        document_id="doc_1",
        source_type="openapi_spec",
        source_ref="paths./items.get",
        relevance_score=0.9,
        distance=0.1,
        content=evidence_content,
    )
    inv = DetachedInvestigationRecord(
        retrieved_evidence=(evidence_item,),
    )
    op = OperationRunRecord(
        http_method="GET",
        path_template="/api/items",
        initial_request_fingerprint="a" * 64,
        initial_attempt=AttemptOutcomeSummary(dispatched=True, passed=False, status_code=500),
        final_status=OverallRunStatus.FAILED,
        final_outcome=AttemptOutcomeSummary(dispatched=True, passed=False, status_code=500),
        investigation=inv,
    )
    report = DiagnosticReportGenerator.build_report(
        operations=[op],
        api_title="Evidence API",
        api_version="1.0.0",
    )
    md = DiagnosticReportGenerator.to_markdown(report)

    # Fence dynamically expands to 4 backticks because content has 3 backticks
    assert "> ````text" in md
    assert "> ```markdown" in md
    assert "> # Injected Heading" in md
    assert "> ````" in md


def test_markdown_renders_urls_as_inert_code_spans():
    """Confirms URLs are rendered as inert code spans, never as clickable links."""
    op = OperationRunRecord(
        http_method="GET",
        path_template="/api/items",
        initial_request_fingerprint="a" * 64,
        initial_attempt=AttemptOutcomeSummary(dispatched=True, passed=False, status_code=500),
        final_status=OverallRunStatus.FAILED,
        final_outcome=AttemptOutcomeSummary(dispatched=True, passed=False, status_code=500),
        remediation_recommendations=(
            RemediationRecommendation(
                priority=FailureSeverity.HIGH,
                target="server_implementation",
                finding="Crash in database",
                action="Fix query",
                reference_urls=("https://api.example.com/docs/db",),
            ),
        ),
    )
    report = DiagnosticReportGenerator.build_report(
        operations=[op],
        api_title="URL API",
        api_version="1.0.0",
        base_url="https://api.example.com",
    )
    md = DiagnosticReportGenerator.to_markdown(report)

    # Inert code spans
    assert "`https://api.example.com`" in md
    assert "`https://api.example.com/docs/db`" in md
    # Clickable link syntax is NOT used
    assert "[https://api.example.com]" not in md
    assert "<https://api.example.com>" not in md
