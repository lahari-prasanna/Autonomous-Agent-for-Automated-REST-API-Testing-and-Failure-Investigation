"""
Tests for LangGraph Agent State and Data Models (Phase 3A).
Validates typed state invariants, workflow statuses, structured error handling,
isolation of initialization helpers, and guarantees zero live HTTP resources in state.
"""

import socket
import pytest
import httpx

from backend.app.agent.state import (
    AgentError,
    AgentState,
    WORKFLOW_STATUSES,
    WorkflowStatus,
    create_initial_state,
)
from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.spec import EndpointDefinition, ParameterDefinition, ParsedAPISpec
from backend.app.services.assertion_evaluator import AssertionReport, AssertionResult
from backend.app.services.spec_parser import OpenAPIParser


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_parsed_spec() -> ParsedAPISpec:
    raw_spec = {
        "openapi": "3.0.3",
        "info": {"title": "Test Shop API", "version": "1.0.0"},
        "paths": {
            "/api/items/{id}": {
                "get": {
                    "operationId": "getItemById",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Item details",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "integer"},
                                            "name": {"type": "string"},
                                        },
                                        "required": ["id", "name"],
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    return OpenAPIParser().parse_dict(raw_spec)


@pytest.fixture
def sample_execution_request() -> ExecutionRequest:
    return ExecutionRequest(
        method="GET",
        url="http://127.0.0.1:8081/health",
        headers={"Accept": "application/json"},
        params={"query": "active"},
        timeout=5.0,
    )


@pytest.fixture
def sample_execution_result() -> ExecutionResult:
    return ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/health",
        status_code=200,
        headers={"content-type": "application/json"},
        body={"status": "healthy"},
        raw_body_bytes_len=20,
        latency_ms=12.5,
        is_success=True,
    )


@pytest.fixture
def sample_assertion_report() -> AssertionReport:
    return AssertionReport(
        endpoint_path="/health",
        method="GET",
        status_code=200,
        matched_status_key="200",
        is_success=True,
        assertions=[
            AssertionResult(
                name="status_code_match",
                passed=True,
                expected="200",
                actual=200,
                message="HTTP status 200 matches declared 200.",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_initial_state_default_status():
    """Initial state created without arguments must default strictly to 'idle'."""
    state = create_initial_state()

    assert state["status"] == "idle"
    assert state["spec"] is None
    assert state["raw_spec"] is None
    assert state["selected_operation"] is None
    assert state["target_path"] is None
    assert state["target_method"] is None
    assert state["target_operation_id"] is None
    assert state["request_config"] is None
    assert state["execution_result"] is None
    assert state["assertion_report"] is None
    assert state["error"] is None
    assert state["metadata"] == {}
    assert state["logs"] == []


def test_state_represents_each_workflow_status():
    """Verify all explicit workflow statuses can be represented in state."""
    expected_statuses = [
        "idle",
        "spec_loaded",
        "operation_selected",
        "request_ready",
        "executed",
        "evaluated",
        "error",
    ]

    assert list(WORKFLOW_STATUSES) == expected_statuses

    for status in expected_statuses:
        state = create_initial_state(status=status)
        assert state["status"] == status

        # Direct mutation/assignment
        state["status"] = status
        assert state["status"] == status


def test_state_accepts_expected_project_data_structures(
    sample_parsed_spec: ParsedAPISpec,
    sample_execution_request: ExecutionRequest,
    sample_execution_result: ExecutionResult,
    sample_assertion_report: AssertionReport,
):
    """Verify state seamlessly accepts existing Phase 1 & 2 schemas with full fidelity."""
    endpoint = sample_parsed_spec.endpoints[0]

    state = create_initial_state(
        spec=sample_parsed_spec,
        selected_operation=endpoint,
        target_path=endpoint.path,
        target_method=endpoint.method,
        target_operation_id=endpoint.operation_id,
        request_config=sample_execution_request,
        execution_result=sample_execution_result,
        assertion_report=sample_assertion_report,
        status="evaluated",
        metadata={"run_env": "test", "iteration": 1},
        logs=["Loaded spec", "Executed request", "Evaluated assertions"],
    )

    # 1. Spec & endpoint fidelity
    assert state["spec"] is not None
    assert state["spec"].title == "Test Shop API"
    assert len(state["spec"].endpoints) == 1
    assert state["selected_operation"].operation_id == "getItemById"
    assert state["target_path"] == "/api/items/{id}"
    assert state["target_method"] == "GET"
    assert state["target_operation_id"] == "getItemById"

    # 2. Execution request envelope
    assert state["request_config"] is not None
    assert state["request_config"].method == "GET"
    assert state["request_config"].url == "http://127.0.0.1:8081/health"

    # 3. Execution result envelope
    assert state["execution_result"] is not None
    assert state["execution_result"].status_code == 200
    assert state["execution_result"].is_success is True
    assert state["execution_result"].body == {"status": "healthy"}

    # 4. Assertion report
    assert state["assertion_report"] is not None
    assert state["assertion_report"].is_success is True
    assert len(state["assertion_report"].assertions) == 1

    # 5. Metadata and logs
    assert state["status"] == "evaluated"
    assert state["metadata"]["run_env"] == "test"
    assert len(state["logs"]) == 3


def test_error_state_represents_structured_errors():
    """Verify error state carries rich structured diagnostics and exception context."""
    # 1. Direct AgentError instantiation
    custom_error = AgentError(
        message="Endpoint connection rejected by host",
        error_type="ConnectionRefusedError",
        step="executed",
        details={"host": "127.0.0.1", "port": 8081, "errno": 111},
    )

    state = create_initial_state(
        status="error",
        error=custom_error,
    )

    assert state["status"] == "error"
    assert state["error"] is not None
    assert state["error"].message == "Endpoint connection rejected by host"
    assert state["error"].error_type == "ConnectionRefusedError"
    assert state["error"].step == "executed"
    assert state["error"].details["port"] == 8081

    # Verify serialization
    dumped = state["error"].model_dump()
    assert dumped["error_type"] == "ConnectionRefusedError"
    assert dumped["details"]["errno"] == 111

    # 2. Construction via from_exception helper
    try:
        raise ValueError("Missing required path parameter 'id'")
    except ValueError as exc:
        agent_err = AgentError.from_exception(
            exc=exc,
            step="request_ready",
            details={"parameter": "id", "location": "path"},
        )

    state["error"] = agent_err
    assert state["error"].message == "Missing required path parameter 'id'"
    assert state["error"].error_type == "ValueError"
    assert state["error"].step == "request_ready"
    assert state["error"].details["location"] == "path"


def test_no_live_http_resources_in_state(
    sample_parsed_spec: ParsedAPISpec,
    sample_execution_request: ExecutionRequest,
    sample_execution_result: ExecutionResult,
    sample_assertion_report: AssertionReport,
):
    """
    Verify that state and its constituent models hold zero live sockets,
    HTTPX clients, transport instances, or streaming response objects.
    """
    state = create_initial_state(
        spec=sample_parsed_spec,
        selected_operation=sample_parsed_spec.endpoints[0],
        request_config=sample_execution_request,
        execution_result=sample_execution_result,
        assertion_report=sample_assertion_report,
        status="evaluated",
    )

    # Disallowed live resource types
    disallowed_types = (
        httpx.Client,
        httpx.AsyncClient,
        httpx.Response,
        socket.socket,
    )

    # Inspect all top-level values
    for key, val in state.items():
        assert not isinstance(val, disallowed_types), f"Field '{key}' contains live HTTP resource: {type(val)}"

    # Inspect execution result fields specifically
    result = state["execution_result"]
    assert result is not None
    assert isinstance(result.status_code, int)
    assert isinstance(result.headers, dict)
    assert isinstance(result.body, (dict, list, str, type(None)))
    assert not hasattr(result, "_client")
    assert not hasattr(result, "_transport")
    assert not hasattr(result, "_stream")


def test_existing_state_data_not_accidentally_overwritten():
    """
    Verify create_initial_state preserves existing state values when merging,
    does not overwrite explicit fields with defaults, and isolates mutable containers.
    """
    initial_metadata = {"test_run": "run-42", "env": "local"}
    initial_logs = ["Spec validated successfully"]

    existing_state = create_initial_state(
        target_path="/api/products",
        target_method="POST",
        status="operation_selected",
        metadata=initial_metadata,
        logs=initial_logs,
    )

    # Merge/update only target_method and add a log entry
    updated_state = create_initial_state(
        existing_state=existing_state,
        target_method="PUT",
        logs=["Operation changed to PUT"],
    )

    # 1. Existing values preserved
    assert updated_state["target_path"] == "/api/products"
    assert updated_state["status"] == "operation_selected"  # NOT overwritten with 'idle'
    assert updated_state["metadata"]["test_run"] == "run-42"

    # 2. Targeted field updated
    assert updated_state["target_method"] == "PUT"

    # 3. Logs accumulated
    assert updated_state["logs"] == [
        "Spec validated successfully",
        "Operation changed to PUT",
    ]

    # 4. Mutating updated_state metadata does not mutate existing_state
    updated_state["metadata"]["new_field"] = "mutated"
    assert "new_field" not in existing_state["metadata"]


def test_independent_mutable_containers():
    """Verify distinct create_initial_state calls do not share mutable default references."""
    state_a = create_initial_state()
    state_b = create_initial_state()

    state_a["metadata"]["key"] = "value_a"
    state_a["logs"].append("log_a")

    assert "key" not in state_b["metadata"]
    assert len(state_b["logs"]) == 0
