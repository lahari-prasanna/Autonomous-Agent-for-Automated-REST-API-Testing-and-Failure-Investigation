"""
Tests for LangGraph StateGraph Assembly and Conditional Routing (Phase 3C).
Verifies graph compilation, explicit conditional edge routing, error termination,
timeout handling, assertion failure reporting, and end-to-end execution.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph

from backend.app.agent.graph import (
    agent_graph,
    build_agent_graph,
    compile_agent_graph,
    route_after_evaluate_response,
    route_after_execute_request,
    route_after_load_spec,
    route_after_prepare_request,
    route_after_select_operation,
)
from backend.app.agent.state import AgentError, AgentState, create_initial_state
from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.spec import EndpointDefinition, ParsedAPISpec
from backend.app.services.assertion_evaluator import (
    AssertionReport,
    AssertionResult,
    DeterministicAssertionEvaluator,
)
from backend.app.services.http_executor import HTTPExecutor
from backend.app.services.spec_parser import OpenAPIParser


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_spec_dict() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Inventory Graph API", "version": "1.0.0"},
        "servers": [{"url": "http://127.0.0.1:8081"}],
        "paths": {
            "/api/products": {
                "get": {
                    "operationId": "getProducts",
                    "summary": "Get all products",
                    "responses": {
                        "200": {
                            "description": "Product list",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "id": {"type": "integer"},
                                                "name": {"type": "string"},
                                                "price": {"type": "number"},
                                            },
                                            "required": ["id", "name", "price"],
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/products/{id}": {
                "get": {
                    "operationId": "getProductById",
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
                            "description": "Product found",
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
            },
        },
    }


# ---------------------------------------------------------------------------
# 1. Graph Compilation & Topology Tests
# ---------------------------------------------------------------------------

def test_graph_compilation():
    """Verify StateGraph builds, compiles, and registers all 5 expected nodes."""
    workflow = build_agent_graph()
    assert workflow is not None

    compiled = compile_agent_graph()
    assert isinstance(compiled, CompiledStateGraph)
    assert isinstance(agent_graph, CompiledStateGraph)

    # Verify registered node names
    expected_nodes = {
        "load_spec",
        "select_operation",
        "prepare_request",
        "execute_request",
        "evaluate_response",
    }
    assert expected_nodes.issubset(set(compiled.nodes.keys()))


# ---------------------------------------------------------------------------
# 2. Isolated Conditional Routing Function Tests
# ---------------------------------------------------------------------------

def test_route_after_load_spec():
    """route_after_load_spec routes to select_operation on spec_loaded, else END."""
    assert route_after_load_spec({"status": "spec_loaded"}) == "select_operation"
    assert route_after_load_spec({"status": "error"}) == END
    assert route_after_load_spec({"status": "idle"}) == END


def test_route_after_select_operation():
    """route_after_select_operation routes to prepare_request on operation_selected, else END."""
    assert route_after_select_operation({"status": "operation_selected"}) == "prepare_request"
    assert route_after_select_operation({"status": "error"}) == END
    assert route_after_select_operation({"status": "spec_loaded"}) == END


def test_route_after_prepare_request():
    """route_after_prepare_request routes to execute_request on request_ready, else END."""
    assert route_after_prepare_request({"status": "request_ready"}) == "execute_request"
    assert route_after_prepare_request({"status": "error"}) == END
    assert route_after_prepare_request({"status": "idle"}) == END


def test_route_after_execute_request():
    """
    route_after_execute_request routes to evaluate_response on completed HTTP exchange,
    and terminates at END on timeout, network error, or status='error'.
    """
    # 1. Success case: completed exchange
    successful_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=200,
        is_success=True,
    )
    assert route_after_execute_request({
        "status": "executed",
        "execution_result": successful_result,
    }) == "evaluate_response"

    # 2. Timeout case: terminates immediately at END without evaluating
    timeout_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=0,
        is_success=False,
        is_timeout=True,
    )
    assert route_after_execute_request({
        "status": "executed",
        "execution_result": timeout_result,
    }) == END

    # 3. Network error: terminates at END without evaluating
    net_err_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=0,
        is_success=False,
        is_network_error=True,
    )
    assert route_after_execute_request({
        "status": "executed",
        "execution_result": net_err_result,
    }) == END

    # 4. Status is error
    assert route_after_execute_request({"status": "error"}) == END

    # 5. Missing execution result
    assert route_after_execute_request({"status": "executed"}) == END


def test_route_after_evaluate_response():
    """route_after_evaluate_response always terminates at END."""
    assert route_after_evaluate_response({"status": "evaluated"}) == END
    assert route_after_evaluate_response({"status": "error"}) == END


# ---------------------------------------------------------------------------
# 3. End-to-End Workflow Execution Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_workflow_successful_execution_reaches_evaluated(sample_spec_dict: dict):
    """Successful execution of valid operation transitions from START to evaluated at END."""
    initial_state = create_initial_state(
        raw_spec=sample_spec_dict,
        target_operation_id="getProducts",
    )

    mock_exec_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=200,
        headers={"content-type": "application/json"},
        body=[{"id": 1, "name": "Mechanical Keyboard", "price": 99.0}],
        latency_ms=18.0,
        is_success=True,
    )

    with patch.object(HTTPExecutor, "execute", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_exec_result

        final_state = await agent_graph.ainvoke(initial_state)

        # 1. State reached evaluated
        assert final_state["status"] == "evaluated"

        # 2. All step envelopes populated
        assert isinstance(final_state["spec"], ParsedAPISpec)
        assert final_state["selected_operation"].operation_id == "getProducts"
        assert final_state["request_config"].url == "http://127.0.0.1:8081/api/products"
        assert final_state["execution_result"] is mock_exec_result
        assert isinstance(final_state["assertion_report"], AssertionReport)
        assert final_state["assertion_report"].is_success is True
        assert final_state.get("error") is None


@pytest.mark.anyio
async def test_workflow_invalid_spec_terminates_at_error():
    """Invalid specification terminates workflow at load_spec without running subsequent nodes."""
    initial_state = create_initial_state(
        raw_spec={"invalid": "no openapi version"},
        target_operation_id="getProducts",
    )

    with patch.object(HTTPExecutor, "execute", new_callable=AsyncMock) as mock_exec:
        final_state = await agent_graph.ainvoke(initial_state)

        # 1. Workflow terminated at error
        assert final_state["status"] == "error"
        assert isinstance(final_state["error"], AgentError)
        assert final_state["error"].step == "spec_loaded"

        # 2. Subsequent nodes were NOT executed
        assert final_state.get("selected_operation") is None
        assert final_state.get("request_config") is None
        assert final_state.get("execution_result") is None
        assert final_state.get("assertion_report") is None
        mock_exec.assert_not_called()


@pytest.mark.anyio
async def test_workflow_missing_operation_terminates_at_error(sample_spec_dict: dict):
    """Unmatched operation terminates workflow at select_operation without preparing or executing."""
    initial_state = create_initial_state(
        raw_spec=sample_spec_dict,
        target_operation_id="nonExistentOperation123",
    )

    with patch.object(HTTPExecutor, "execute", new_callable=AsyncMock) as mock_exec:
        final_state = await agent_graph.ainvoke(initial_state)

        assert final_state["status"] == "error"
        assert final_state["error"].step == "operation_selected"
        assert "OperationNotFoundError" in final_state["error"].error_type

        # Did not proceed to prepare_request or execute_request
        assert final_state.get("request_config") is None
        assert final_state.get("execution_result") is None
        assert final_state.get("assertion_report") is None
        mock_exec.assert_not_called()


@pytest.mark.anyio
async def test_workflow_request_preparation_failure_terminates_at_error(sample_spec_dict: dict):
    """Missing base URL causes prepare_request to fail and terminates before execution."""
    # Remove servers from spec
    spec_no_servers = dict(sample_spec_dict)
    spec_no_servers["servers"] = []

    initial_state = create_initial_state(
        raw_spec=spec_no_servers,
        target_operation_id="getProducts",
        # Intentionally no base_url supplied
    )

    with patch.object(HTTPExecutor, "execute", new_callable=AsyncMock) as mock_exec:
        final_state = await agent_graph.ainvoke(initial_state)

        assert final_state["status"] == "error"
        assert final_state["error"].step == "request_ready"
        assert "MissingBaseUrlError" in final_state["error"].error_type

        # Did not proceed to execute_request
        assert final_state.get("execution_result") is None
        assert final_state.get("assertion_report") is None
        mock_exec.assert_not_called()


@pytest.mark.anyio
async def test_workflow_execution_timeout_terminates_without_evaluating(sample_spec_dict: dict):
    """
    When HTTP execution times out, workflow terminates at execute_request
    and does NOT call evaluate_response.
    """
    initial_state = create_initial_state(
        raw_spec=sample_spec_dict,
        target_operation_id="getProducts",
    )

    timeout_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=0,
        is_success=False,
        is_timeout=True,
        error_message="Request timed out: wall-clock deadline exceeded (10.0s limit)",
    )

    with patch.object(HTTPExecutor, "execute", new_callable=AsyncMock) as mock_exec, \
         patch.object(DeterministicAssertionEvaluator, "evaluate") as mock_eval:

        mock_exec.return_value = timeout_result

        final_state = await agent_graph.ainvoke(initial_state)

        # 1. State recorded execution and timeout
        assert final_state["status"] == "executed"
        assert final_state["execution_result"].is_timeout is True

        # 2. Assertion evaluation was NOT invoked
        assert final_state.get("assertion_report") is None
        mock_eval.assert_not_called()


@pytest.mark.anyio
async def test_workflow_execution_exception_terminates_without_evaluating(sample_spec_dict: dict):
    """
    When HTTP executor raises an unhandled exception, workflow sets status='error'
    and terminates at END without calling evaluate_response.
    """
    initial_state = create_initial_state(
        raw_spec=sample_spec_dict,
        target_operation_id="getProducts",
    )

    with patch.object(HTTPExecutor, "execute", new_callable=AsyncMock) as mock_exec, \
         patch.object(DeterministicAssertionEvaluator, "evaluate") as mock_eval:

        mock_exec.side_effect = RuntimeError("Fatal socket backend crash")

        final_state = await agent_graph.ainvoke(initial_state)

        assert final_state["status"] == "error"
        assert final_state["error"].step == "executed"
        assert "Fatal socket backend crash" in final_state["error"].message

        # Assertion evaluation was NOT invoked
        assert final_state.get("assertion_report") is None
        mock_eval.assert_not_called()


@pytest.mark.anyio
async def test_workflow_assertion_failures_produce_evaluated_report_not_routing_error(
    sample_spec_dict: dict,
):
    """
    When response has schema violations, evaluate_response produces an
    AssertionReport with is_success=False and status='evaluated', NOT a pipeline routing error.
    """
    initial_state = create_initial_state(
        raw_spec=sample_spec_dict,
        target_operation_id="getProducts",
    )

    # Missing required 'price' field
    invalid_body_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=200,
        headers={"content-type": "application/json"},
        body=[{"id": 1, "name": "Desk Lamp"}],  # missing 'price'
        latency_ms=15.0,
        is_success=True,
    )

    with patch.object(HTTPExecutor, "execute", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = invalid_body_result

        final_state = await agent_graph.ainvoke(initial_state)

        # 1. Successfully reached evaluated status
        assert final_state["status"] == "evaluated"

        # 2. Report captures schema failures without triggering routing error
        assert final_state.get("error") is None
        report = final_state["assertion_report"]
        assert isinstance(report, AssertionReport)
        assert report.is_success is False
        assert len(report.failed_assertions) > 0
        assert any("price" in a.message.lower() for a in report.failed_assertions)


@pytest.mark.anyio
async def test_workflow_existing_services_are_invoked(sample_spec_dict: dict):
    """
    Verify that StateGraph directly invokes existing services:
    OpenAPIParser, HTTPExecutor, and DeterministicAssertionEvaluator.
    """
    initial_state = create_initial_state(
        raw_spec=sample_spec_dict,
        target_operation_id="getProducts",
    )

    mock_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=200,
        headers={"content-type": "application/json"},
        body=[{"id": 1, "name": "Item", "price": 10.0}],
        latency_ms=10.0,
        is_success=True,
    )

    with patch.object(OpenAPIParser, "parse_dict", wraps=OpenAPIParser().parse_dict) as mock_parser, \
         patch.object(HTTPExecutor, "execute", new_callable=AsyncMock) as mock_exec, \
         patch.object(DeterministicAssertionEvaluator, "evaluate", wraps=DeterministicAssertionEvaluator(raw_spec=sample_spec_dict).evaluate) as mock_eval:

        mock_exec.return_value = mock_result

        final_state = await agent_graph.ainvoke(initial_state)

        assert final_state["status"] == "evaluated"
        mock_parser.assert_called_once()
        mock_exec.assert_awaited_once()
        mock_eval.assert_called_once()

