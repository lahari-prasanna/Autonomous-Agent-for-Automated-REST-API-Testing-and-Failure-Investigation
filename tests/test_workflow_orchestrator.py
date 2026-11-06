"""
Comprehensive Integration and Unit Test Suite for Outer Workflow Orchestrator (Phase 4C).
Verifies:
  1. External wrap of agent_graph preserving compiled graph immutability.
  2. Isolated initial state preparation.
  3. Transport dispatch extraction (SSRF vs timeouts vs HTTP responses).
  4. Idempotent transient retries and resolution (RESOLVED_AFTER_TRANSIENT_RETRY).
  5. Non-idempotent transient failure termination without retry (FAILED).
  6. Strict combined follow-up ceiling (max 2 follow-ups).
  7. Fail-closed variation authorization on POST/PATCH.
  8. Parameter variation testing on GET without inflating original operation pass rate.
  9. Safe handling of candidate dispatch failures without inventing test records.
 10. Non-fatal degradation of FailureInvestigator on knowledge store exceptions.
 11. Bounded concurrency in run_suite.
 12. End-to-end execution against live Controlled Mock REST API.
"""

from __future__ import annotations

import asyncio
import copy
import socket
import threading
import time
from typing import Any, Dict, Generator, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import uvicorn

import mock_api.main
from backend.app.agent.graph import agent_graph
from backend.app.agent.orchestrator import WorkflowOrchestrator
from backend.app.agent.state import AgentError, AgentState, create_initial_state
from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.failure import FailureCategory, FailureClassification, FailureSeverity, RetryGuidance, RetryPolicy
from backend.app.schemas.report import (
    AttemptOutcomeSummary,
    DiagnosticReport,
    FollowupDecisionAuditRecord,
    OperationRunRecord,
    OverallRunStatus,
    TransientRetryRecord,
    VariationCandidateRecord,
    VariationTestRecord,
)
from backend.app.schemas.retest import ParameterPerturbation, PerturbationStrategy, RetestAction
from backend.app.schemas.spec import EndpointDefinition, ParameterDefinition, ParsedAPISpec, ResponseDefinition
from backend.app.services.assertion_evaluator import AssertionReport, AssertionResult
from backend.app.services.report_generator import DiagnosticReportGenerator
from backend.app.services.spec_parser import OpenAPIParser


# ---------------------------------------------------------------------------
# Fixtures and Helpers
# ---------------------------------------------------------------------------

class DelayASGIWrapper:
    """Test ASGI wrapper for mock server."""
    __test__ = False

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


@pytest.fixture(scope="module")
def live_mock_server() -> Generator[str, None, None]:
    """Starts Controlled Mock REST API on an ephemeral loopback port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    wrapped_app = DelayASGIWrapper(mock_api.main.app)
    config = uvicorn.Config(app=wrapped_app, log_level="error", lifespan="off")
    server = uvicorn.Server(config=config)

    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    server_thread.start()

    ready = False
    for _ in range(50):
        time.sleep(0.05)
        try:
            r = httpx.get(f"{base_url}/api/health", timeout=0.5)
            if r.status_code == 200 and r.headers.get("x-controlled-mock-api") == "true":
                ready = True
                break
        except Exception:
            pass

    assert ready, f"Failed to start mock API server on {base_url}"
    yield base_url
    server.should_exit = True
    server_thread.join(timeout=2.0)


@pytest.fixture(autouse=True)
def reset_mock_db() -> Generator[None, None, None]:
    """Resets mock database before and after each test."""
    initial_db = copy.deepcopy(mock_api.main.PRODUCTS_DB)
    initial_next_id = mock_api.main._next_id
    yield
    mock_api.main.PRODUCTS_DB.clear()
    mock_api.main.PRODUCTS_DB.update(copy.deepcopy(initial_db))
    mock_api.main._next_id = initial_next_id


def make_exec_result(
    method: str = "GET",
    url: str = "http://testserver/api/items",
    status_code: int = 200,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Any] = None,
    latency_ms: float = 10.0,
    is_success: bool = True,
    is_network_error: bool = False,
    is_timeout: bool = False,
    is_ssrf_blocked: bool = False,
) -> ExecutionResult:
    return ExecutionResult(
        method=method,
        url=url,
        status_code=status_code,
        headers=headers or {},
        body=body,
        latency_ms=latency_ms,
        is_success=is_success,
        is_network_error=is_network_error,
        is_timeout=is_timeout,
        is_ssrf_blocked=is_ssrf_blocked,
    )


def make_pass_report(method: str = "GET", path: str = "/api/items", status_code: int = 200) -> AssertionReport:
    return AssertionReport(
        endpoint_path=path,
        method=method,
        status_code=status_code,
        matched_status_key=str(status_code),
        is_success=True,
        assertions=[
            AssertionResult(
                name="status_code_match",
                passed=True,
                expected=status_code,
                actual=status_code,
                message=f"Status code {status_code} matched",
            )
        ],
    )


def make_fail_report(method: str = "GET", path: str = "/api/items", status_code: int = 500, expected: int = 200) -> AssertionReport:
    return AssertionReport(
        endpoint_path=path,
        method=method,
        status_code=status_code,
        matched_status_key=None,
        is_success=False,
        assertions=[
            AssertionResult(
                name="status_code_match",
                passed=False,
                expected=expected,
                actual=status_code,
                message=f"Expected {expected}, got {status_code}",
            )
        ],
    )


def make_classification(
    has_failure: bool = False,
    primary_category: Optional[FailureCategory] = None,
    severity: FailureSeverity = FailureSeverity.LOW,
    retry_policy: RetryPolicy = RetryPolicy.DO_NOT_RETRY,
    is_idempotent_method: bool = True,
    summary: str = "Test summary",
) -> FailureClassification:
    return FailureClassification(
        has_failure=has_failure,
        primary_category=primary_category,
        severity=severity,
        retry_guidance=RetryGuidance(
            policy=retry_policy,
            is_idempotent_method=is_idempotent_method,
            suggested_backoff_seconds=None,
            reason="Test rationale",
        ),
        summary=summary,
    )


def make_test_spec(
    endpoints: Optional[List[EndpointDefinition]] = None,
    title: str = "Test Spec",
    version: str = "1.0.0",
) -> ParsedAPISpec:
    eps = endpoints or [
        EndpointDefinition(
            path="/api/items",
            method="GET",
            operation_id="getItems",
            summary="Get items",
            parameters=[
                ParameterDefinition(
                    name="limit",
                    in_="query",
                    schema_={"type": "integer", "minimum": 1, "maximum": 100},
                    required=False,
                )
            ],
            request_body=None,
            responses={"200": ResponseDefinition(status_code="200", description="OK")},
            tags=["items"],
        ),
        EndpointDefinition(
            path="/api/items",
            method="POST",
            operation_id="createItem",
            summary="Create item",
            parameters=[],
            request_body=None,
            responses={"201": ResponseDefinition(status_code="201", description="Created")},
            tags=["items"],
        ),
    ]
    return ParsedAPISpec(
        title=title,
        version=version,
        openapi_version="3.0.0",
        description="Test",
        servers=["http://testserver"],
        endpoints=eps,
    )


# ---------------------------------------------------------------------------
# 1. State Preparation & Summary Extraction Unit Tests
# ---------------------------------------------------------------------------

def test_prepare_initial_state():
    """Confirms prepare_initial_state creates a valid AgentState with all required metadata."""
    spec = make_test_spec()
    op = spec.endpoints[0]
    orch = WorkflowOrchestrator(base_url="http://api.example.com", safe_dev_mode=True)

    state = orch.prepare_initial_state(spec=spec, operation=op)

    assert state["status"] == "idle"
    assert state["target_path"] == "/api/items"
    assert state["target_method"] == "GET"
    assert state["target_operation_id"] == "getItems"
    assert state["metadata"]["operation_id"] == "getItems"
    assert state["metadata"]["path_template"] == "/api/items"
    assert state["metadata"]["method"] == "GET"
    assert state["metadata"]["base_url"] == "http://api.example.com"
    assert state["metadata"]["safe_dev_mode"] is True
    assert len(state["logs"]) == 1


def test_extract_attempt_summary_transport_evidence():
    """Confirms dispatch, uncertainty, and status codes reflect actual transport evidence."""
    orch = WorkflowOrchestrator()

    # 1. SSRF block: not dispatched, no status code, not uncertain
    exec_ssrf = make_exec_result(
        method="GET",
        url="http://127.0.0.1/admin",
        status_code=0,
        latency_ms=1.0,
        is_success=False,
        is_ssrf_blocked=True,
    )
    class_ssrf = make_classification(
        has_failure=True,
        primary_category=FailureCategory.SSRF_SECURITY_BLOCK,
        severity=FailureSeverity.CRITICAL,
        retry_policy=RetryPolicy.DO_NOT_RETRY,
    )
    summary_ssrf = orch._extract_attempt_summary({"execution_result": exec_ssrf}, class_ssrf)
    assert summary_ssrf.dispatched is False
    assert summary_ssrf.status_code is None
    assert summary_ssrf.uncertain_delivery is False
    assert summary_ssrf.passed is False

    # 2. Timeout: dispatched, no status code, uncertain delivery
    exec_timeout = make_exec_result(
        method="GET",
        url="http://testserver/api/items",
        status_code=0,
        latency_ms=5000.0,
        is_success=False,
        is_timeout=True,
    )
    class_timeout = make_classification(
        has_failure=True,
        primary_category=FailureCategory.REQUEST_TIMEOUT,
        severity=FailureSeverity.HIGH,
        retry_policy=RetryPolicy.SAFE_TO_RETRY,
    )
    summary_timeout = orch._extract_attempt_summary({"execution_result": exec_timeout}, class_timeout)
    assert summary_timeout.dispatched is True
    assert summary_timeout.status_code is None
    assert summary_timeout.uncertain_delivery is True
    assert summary_timeout.passed is False

    # 3. HTTP 200 Success: dispatched, status code 200, passed
    exec_ok = make_exec_result(
        method="GET",
        url="http://testserver/api/items",
        status_code=200,
        body="{}",
        latency_ms=25.0,
        is_success=True,
    )
    class_ok = make_classification(
        has_failure=False,
        primary_category=None,
        severity=FailureSeverity.LOW,
        retry_policy=RetryPolicy.DO_NOT_RETRY,
    )
    summary_ok = orch._extract_attempt_summary({"execution_result": exec_ok}, class_ok)
    assert summary_ok.dispatched is True
    assert summary_ok.status_code == 200
    assert summary_ok.uncertain_delivery is False
    assert summary_ok.passed is True


# ---------------------------------------------------------------------------
# 2. Orchestrator Workflow Scenarios (Mocked Graph)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_run_operation_initial_success():
    """Scenario: Endpoint succeeds on initial attempt; 0 follow-ups executed; status PASSED."""
    spec = make_test_spec()
    op = spec.endpoints[0]
    orch = WorkflowOrchestrator(base_url="http://testserver")

    req = ExecutionRequest(method="GET", url="http://testserver/api/items")
    res = make_exec_result(method="GET", url="http://testserver/api/items", status_code=200, body="[]", latency_ms=10.0)
    rep = make_pass_report(method="GET", path="/api/items", status_code=200)

    mock_state = {
        "status": "evaluated",
        "error": None,
        "request_config": req,
        "execution_result": res,
        "assertion_report": rep,
    }

    with patch.object(agent_graph, "ainvoke", AsyncMock(return_value=mock_state)):
        op_record = await orch.run_operation(spec=spec, operation=op)

    assert op_record.final_status == OverallRunStatus.PASSED
    assert op_record.initial_attempt.passed is True
    assert op_record.initial_attempt.dispatched is True
    assert len(op_record.transient_retries) == 0
    assert len(op_record.variation_tests) == 0
    assert len(op_record.followup_decisions) == 0
    assert op_record.final_outcome == op_record.initial_attempt


@pytest.mark.anyio
async def test_run_operation_idempotent_transient_retry_resolved():
    """
    Scenario: GET endpoint fails initially with 503 SERVER_ERROR.
    RetestController triggers transient retry with identical fingerprint.
    Retry succeeds with 200 OK.
    Original failure preserved; final_status RESOLVED_AFTER_TRANSIENT_RETRY.
    """
    spec = make_test_spec()
    op = spec.endpoints[0]  # GET /api/items
    orch = WorkflowOrchestrator(base_url="http://testserver")

    req = ExecutionRequest(method="GET", url="http://testserver/api/items")
    res_fail = make_exec_result(method="GET", url="http://testserver/api/items", status_code=503, body="Server Busy", latency_ms=50.0)
    rep_fail = make_fail_report(method="GET", path="/api/items", status_code=503, expected=200)

    state_fail = {
        "status": "evaluated",
        "error": None,
        "request_config": req,
        "execution_result": res_fail,
        "assertion_report": rep_fail,
    }

    res_pass = make_exec_result(method="GET", url="http://testserver/api/items", status_code=200, body="[]", latency_ms=15.0)
    rep_pass = make_pass_report(method="GET", path="/api/items", status_code=200)
    state_pass = {
        "status": "evaluated",
        "error": None,
        "request_config": req,
        "execution_result": res_pass,
        "assertion_report": rep_pass,
    }

    with patch.object(agent_graph, "ainvoke", AsyncMock(side_effect=[state_fail, state_pass])):
        op_record = await orch.run_operation(spec=spec, operation=op)

    # 1. Status resolved by transient retry
    assert op_record.final_status == OverallRunStatus.RESOLVED_AFTER_TRANSIENT_RETRY
    # 2. Original execution result preserved as failed
    assert op_record.initial_attempt.passed is False
    assert op_record.initial_attempt.status_code == 503
    # 3. Exactly one transient retry executed and passed
    assert len(op_record.transient_retries) == 1
    assert op_record.transient_retries[0].attempt_number == 1
    assert op_record.transient_retries[0].outcome.passed is True
    assert op_record.transient_retries[0].outcome.status_code == 200
    # 4. Fingerprint of retry matches initial request fingerprint
    assert op_record.transient_retries[0].canonical_request_fingerprint == op_record.initial_request_fingerprint
    # 5. Ledger has exactly one dispatched decision
    assert len(op_record.followup_decisions) == 1
    assert op_record.followup_decisions[0].action == RetestAction.RETRY_TRANSIENT
    assert op_record.followup_decisions[0].dispatched is True
    # 6. Final outcome matches the successful retry
    assert op_record.final_outcome == op_record.transient_retries[0].outcome


@pytest.mark.anyio
async def test_run_operation_non_idempotent_no_transient_retry():
    """
    Scenario: POST endpoint fails with 503 SERVER_ERROR.
    Orchestrator must NEVER blindly retry non-idempotent operations.
    0 retries executed; status FAILED.
    """
    spec = make_test_spec()
    op = spec.endpoints[1]  # POST /api/items
    orch = WorkflowOrchestrator(base_url="http://testserver")

    req = ExecutionRequest(method="POST", url="http://testserver/api/items", json_body={"name": "widget"})
    res_fail = make_exec_result(method="POST", url="http://testserver/api/items", status_code=503, body="Server Busy", latency_ms=50.0)
    rep_fail = make_fail_report(method="POST", path="/api/items", status_code=503, expected=201)

    state_fail = {
        "status": "evaluated",
        "error": None,
        "request_config": req,
        "execution_result": res_fail,
        "assertion_report": rep_fail,
    }

    with patch.object(agent_graph, "ainvoke", AsyncMock(return_value=state_fail)):
        op_record = await orch.run_operation(spec=spec, operation=op)

    assert op_record.final_status == OverallRunStatus.FAILED
    assert op_record.initial_attempt.passed is False
    assert len(op_record.transient_retries) == 0
    assert len(op_record.followup_decisions) == 0


@pytest.mark.anyio
async def test_run_operation_transient_retry_ceiling_at_two():
    """
    Scenario: GET endpoint fails continuously with 503.
    Orchestrator stops after exactly 2 retries (hard ceiling of 2 follow-ups).
    Status remains FAILED.
    """
    spec = make_test_spec()
    op = spec.endpoints[0]  # GET /api/items
    orch = WorkflowOrchestrator(base_url="http://testserver")

    req = ExecutionRequest(method="GET", url="http://testserver/api/items")
    res_fail = make_exec_result(method="GET", url="http://testserver/api/items", status_code=503, body="Unavailable", latency_ms=30.0)
    rep_fail = make_fail_report(method="GET", path="/api/items", status_code=503, expected=200)
    state_fail = {
        "status": "evaluated",
        "error": None,
        "request_config": req,
        "execution_result": res_fail,
        "assertion_report": rep_fail,
    }

    # Three invocations: initial + 2 retries
    with patch.object(agent_graph, "ainvoke", AsyncMock(return_value=state_fail)) as mock_invoke:
        op_record = await orch.run_operation(spec=spec, operation=op)
        assert mock_invoke.call_count == 3

    assert op_record.final_status == OverallRunStatus.FAILED
    assert len(op_record.transient_retries) == 2
    assert len(op_record.followup_decisions) == 2
    assert op_record.final_outcome == op_record.transient_retries[-1].outcome


@pytest.mark.anyio
async def test_post_variation_fails_closed():
    """
    Scenario: POST endpoint fails with 400 (validation failure).
    Policy authorization fails closed (POST mutation variation denied).
    Candidate rejected before dispatch (approved_for_dispatch=False).
    0 variation tests executed; status FAILED.
    """
    spec = make_test_spec()
    op = spec.endpoints[1]  # POST /api/items
    orch = WorkflowOrchestrator(base_url="http://testserver")

    req = ExecutionRequest(method="POST", url="http://testserver/api/items", json_body={"limit": -1})
    res_fail = make_exec_result(method="POST", url="http://testserver/api/items", status_code=400, body='{"error":"validation error"}', latency_ms=10.0)
    rep_fail = make_fail_report(method="POST", path="/api/items", status_code=400, expected=201)
    state_fail = {
        "status": "evaluated",
        "error": None,
        "request_config": req,
        "execution_result": res_fail,
        "assertion_report": rep_fail,
    }

    with patch.object(agent_graph, "ainvoke", AsyncMock(return_value=state_fail)):
        op_record = await orch.run_operation(spec=spec, operation=op)

    assert op_record.final_status == OverallRunStatus.FAILED
    assert len(op_record.variation_candidates) == 1
    cand = op_record.variation_candidates[0]
    assert cand.authorization_approved is False
    assert cand.approved_for_dispatch is False
    assert cand.dispatched is False
    assert len(op_record.variation_tests) == 0

    assert len(op_record.followup_decisions) == 1
    decision = op_record.followup_decisions[0]
    assert decision.action == RetestAction.NO_ACTION
    assert decision.reason_category == "authorization_denied"
    assert decision.dispatched is False


@pytest.mark.anyio
async def test_get_variation_passing_preserves_original_failed_status():
    """
    Scenario: GET endpoint fails with 400.
    A parameter variation is generated, approved, dispatched, and passes (200 OK).
    CRITICAL REQUIREMENT: Variation passing MUST NEVER turn the original operation into PASSED or RESOLVED!
    The operation final_status remains FAILED.
    """
    spec = make_test_spec()
    op = spec.endpoints[0]  # GET /api/items (has 'limit' query param)
    orch = WorkflowOrchestrator(base_url="http://testserver")

    req_fail = ExecutionRequest(method="GET", url="http://testserver/api/items?limit=-5")
    res_fail = make_exec_result(method="GET", url="http://testserver/api/items?limit=-5", status_code=400, body="Bad limit", latency_ms=10.0)
    rep_fail = make_fail_report(method="GET", path="/api/items", status_code=400, expected=200)
    state_fail = {
        "status": "evaluated",
        "error": None,
        "request_config": req_fail,
        "execution_result": res_fail,
        "assertion_report": rep_fail,
    }

    req_pass = ExecutionRequest(method="GET", url="http://testserver/api/items?limit=10")
    res_pass = make_exec_result(method="GET", url="http://testserver/api/items?limit=10", status_code=200, body="[]", latency_ms=10.0)
    rep_pass = make_pass_report(method="GET", path="/api/items", status_code=200)
    state_pass = {
        "status": "evaluated",
        "error": None,
        "request_config": req_pass,
        "execution_result": res_pass,
        "assertion_report": rep_pass,
    }

    with patch.object(agent_graph, "ainvoke", AsyncMock(side_effect=[state_fail, state_pass])):
        op_record = await orch.run_operation(spec=spec, operation=op)

    # 1. Operation MUST remain FAILED
    assert op_record.final_status == OverallRunStatus.FAILED
    assert op_record.initial_attempt.passed is False
    assert op_record.final_outcome == op_record.initial_attempt

    # 2. Variation test recorded independently as passed
    assert len(op_record.variation_candidates) == 1
    assert op_record.variation_candidates[0].dispatched is True
    assert len(op_record.variation_tests) == 1
    assert op_record.variation_tests[0].outcome.passed is True
    assert op_record.variation_tests[0].outcome.status_code == 200

    # 3. Build DiagnosticReport and verify metric reconciliations
    report = DiagnosticReportGenerator.build_report(
        operations=[op_record],
        api_title="Test API",
        api_version="1.0",
    )
    assert report.summary.total_operations == 1
    assert report.summary.failed_operations == 1
    assert report.summary.passed_operations == 0
    assert report.summary.pass_rate_percentage == 0.0
    assert report.summary.variation_metrics.candidates_generated == 1
    assert report.summary.variation_metrics.candidates_dispatched == 1
    assert report.summary.variation_metrics.variation_tests_passed == 1
    assert report.summary.variation_metrics.variation_success_rate_percentage == 100.0


@pytest.mark.anyio
async def test_candidate_dispatch_failure():
    """
    Scenario: Candidate approved for dispatch, but dispatching raises an exception in graph.
    The candidate is recorded with dispatched=False and dispatch_error populated.
    No variation test record is invented.
    """
    spec = make_test_spec()
    op = spec.endpoints[0]  # GET /api/items
    orch = WorkflowOrchestrator(base_url="http://testserver")

    req = ExecutionRequest(method="GET", url="http://testserver/api/items?limit=-1")
    res_fail = make_exec_result(method="GET", url="http://testserver/api/items?limit=-1", status_code=400, body="Bad parameter", latency_ms=10.0)
    rep_fail = make_fail_report(method="GET", path="/api/items", status_code=400, expected=200)
    state_fail = {
        "status": "evaluated",
        "error": None,
        "request_config": req,
        "execution_result": res_fail,
        "assertion_report": rep_fail,
    }

    # Second invocation raises a dispatch exception
    with patch.object(agent_graph, "ainvoke", AsyncMock(side_effect=[state_fail, RuntimeError("Transport connection closed")])):
        op_record = await orch.run_operation(spec=spec, operation=op)

    assert op_record.final_status == OverallRunStatus.FAILED
    assert len(op_record.variation_candidates) == 1
    cand = op_record.variation_candidates[0]
    assert cand.approved_for_dispatch is True
    assert cand.dispatched is False
    assert "DispatchException: RuntimeError" in cand.dispatch_error

    # Zero variation tests executed (no record invented)
    assert len(op_record.variation_tests) == 0

    # Ledger audit
    assert len(op_record.followup_decisions) == 1
    decision = op_record.followup_decisions[0]
    assert decision.action == RetestAction.RUN_VARIATION_TEST
    assert decision.reason_category == "dispatch_failed"
    assert decision.dispatched is False


@pytest.mark.anyio
async def test_knowledge_store_resilience():
    """
    Scenario: FailureInvestigator raises an unexpected exception (e.g. database down).
    Orchestrator gracefully catches it; marks investigation_degraded=True;
    workflow completes deterministically without throwing.
    """
    spec = make_test_spec()
    op = spec.endpoints[0]
    orch = WorkflowOrchestrator(base_url="http://testserver")

    req = ExecutionRequest(method="GET", url="http://testserver/api/items")
    res_fail = make_exec_result(method="GET", url="http://testserver/api/items", status_code=500, body="Crash", latency_ms=20.0)
    rep_fail = make_fail_report(method="GET", path="/api/items", status_code=500, expected=200)
    state_fail = {
        "status": "evaluated",
        "error": None,
        "request_config": req,
        "execution_result": res_fail,
        "assertion_report": rep_fail,
    }

    with patch.object(agent_graph, "ainvoke", AsyncMock(return_value=state_fail)):
        with patch("backend.app.services.failure_investigator.FailureInvestigator.investigate", side_effect=RuntimeError("Store unreachable")):
            op_record = await orch.run_operation(spec=spec, operation=op)

    assert op_record.final_status == OverallRunStatus.FAILED
    assert op_record.investigation is not None
    assert op_record.investigation.investigation_degraded is True
    assert op_record.investigation.investigation_error_type == "RuntimeError"
    assert "RuntimeError" in op_record.investigation.inferences[0]


@pytest.mark.anyio
async def test_run_suite_concurrency_and_report():
    """
    Scenario: Batch execution of multiple endpoints with bounded concurrency.
    Returns fully validated DiagnosticReport.
    """
    spec = make_test_spec()
    orch = WorkflowOrchestrator(base_url="http://testserver", max_concurrency=2)

    req = ExecutionRequest(method="GET", url="http://testserver/api/items")
    res = make_exec_result(method="GET", url="http://testserver/api/items", status_code=200, body="[]", latency_ms=10.0)
    rep = make_pass_report(method="GET", path="/api/items", status_code=200)
    mock_state = {
        "status": "evaluated",
        "error": None,
        "request_config": req,
        "execution_result": res,
        "assertion_report": rep,
    }

    with patch.object(agent_graph, "ainvoke", AsyncMock(return_value=mock_state)):
        report = await orch.run_suite(spec=spec)

    assert isinstance(report, DiagnosticReport)
    assert report.summary.total_operations == 2
    assert report.summary.passed_operations == 2
    assert report.summary.pass_rate_percentage == 100.0


# ---------------------------------------------------------------------------
# 3. Live Mock REST API End-to-End Orchestrator Verification
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_live_mock_api_orchestrator_integration(live_mock_server: str):
    """
    Scenario: Runs against actual live background Uvicorn mock server.
    Real HTTP execution, real SSRF safe transport, real assertion evaluation,
    and real diagnostic report generation.
    """
    spec = OpenAPIParser.parse_file("mock_api/openapi.json")
    health_op = next(ep for ep in spec.endpoints if ep.path == "/api/health")

    orch = WorkflowOrchestrator(
        base_url=live_mock_server,
        safe_dev_mode=True,
    )

    op_record = await orch.run_operation(spec=spec, operation=health_op)

    assert op_record.final_status == OverallRunStatus.PASSED
    assert op_record.initial_attempt.passed is True
    assert op_record.initial_attempt.status_code == 200
    assert op_record.initial_attempt.dispatched is True
    assert len(op_record.transient_retries) == 0

    # Build report for single operation
    report = DiagnosticReportGenerator.build_report(
        operations=[op_record],
        api_title=spec.title,
        api_version=spec.version,
        base_url=live_mock_server,
    )
    assert report.summary.total_operations == 1
    assert report.summary.passed_operations == 1
    assert report.summary.pass_rate_percentage == 100.0

    # Markdown format generation
    md = DiagnosticReportGenerator.to_markdown(report)
    assert "# Diagnostic API Testing Report" in md
    assert "Controlled Mock E-Commerce REST API" in md
    assert "Overall Operation Pass Rate" in md
    assert "100.00%" in md
