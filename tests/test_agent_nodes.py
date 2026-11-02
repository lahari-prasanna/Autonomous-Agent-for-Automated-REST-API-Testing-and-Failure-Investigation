"""
Tests for LangGraph Agent Nodes (Phase 3B).
Verifies deterministic node functions: load_spec_node, select_operation_node,
prepare_request_node, execute_request_node, and evaluate_response_node.
"""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from backend.app.agent.nodes import (
    evaluate_response_node,
    execute_request_node,
    load_spec_node,
    prepare_request_node,
    select_operation_node,
)
from backend.app.agent.state import AgentError, AgentState, create_initial_state
from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.spec import EndpointDefinition, ParsedAPISpec
from backend.app.services.assertion_evaluator import AssertionReport, AssertionResult
from backend.app.services.http_executor import HTTPExecutor
from backend.app.services.spec_parser import OpenAPIParser


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_spec_dict() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Warehouse API", "version": "1.0.0"},
        "servers": [{"url": "http://127.0.0.1:8081"}],
        "paths": {
            "/api/products": {
                "get": {
                    "operationId": "listProducts",
                    "summary": "List all products",
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
                },
                "post": {
                    "operationId": "createProduct",
                    "summary": "Create new product",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "price": {"type": "number"},
                                    },
                                    "required": ["name", "price"],
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "integer"},
                                            "name": {"type": "string"},
                                            "price": {"type": "number"},
                                        },
                                        "required": ["id", "name", "price"],
                                    }
                                }
                            },
                        }
                    },
                },
            },
            "/api/products/{id}": {
                "get": {
                    "operationId": "getProductById",
                    "summary": "Get product by ID",
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
                        },
                        "404": {"description": "Product not found"},
                    },
                }
            },
        },
    }


@pytest.fixture
def sample_parsed_spec(sample_spec_dict: dict) -> ParsedAPISpec:
    return OpenAPIParser().parse_dict(sample_spec_dict)


# ---------------------------------------------------------------------------
# Tests: load_spec_node
# ---------------------------------------------------------------------------

def test_load_spec_node_success_dict(sample_spec_dict: dict):
    """load_spec_node parses in-memory specification dictionary successfully."""
    state = create_initial_state(raw_spec=sample_spec_dict)
    updates = load_spec_node(state)

    assert updates["status"] == "spec_loaded"
    assert isinstance(updates["spec"], ParsedAPISpec)
    assert updates["spec"].title == "Warehouse API"
    assert len(updates["spec"].endpoints) == 3
    assert len(updates["logs"]) > 0


def test_load_spec_node_success_file():
    """load_spec_node loads specification from a local file path."""
    state = create_initial_state(raw_spec="mock_api/openapi.json")
    updates = load_spec_node(state)

    assert updates["status"] == "spec_loaded"
    assert isinstance(updates["spec"], ParsedAPISpec)
    assert updates["spec"].title == "Controlled Mock E-Commerce REST API"


def test_load_spec_node_passthrough(sample_parsed_spec: ParsedAPISpec):
    """load_spec_node passes through an already parsed ParsedAPISpec."""
    state = create_initial_state(spec=sample_parsed_spec)
    updates = load_spec_node(state)

    assert updates["status"] == "spec_loaded"
    assert updates["spec"] is sample_parsed_spec


def test_load_spec_node_invalid_spec():
    """load_spec_node returns structured error when specification is invalid."""
    invalid_dict = {"invalid": "missing openapi version"}
    state = create_initial_state(raw_spec=invalid_dict)
    updates = load_spec_node(state)

    assert updates["status"] == "error"
    assert isinstance(updates["error"], AgentError)
    assert updates["error"].step == "spec_loaded"
    assert "SpecValidationError" in updates["error"].error_type


def test_load_spec_node_missing_input():
    """load_spec_node returns structured error when no specification data is provided."""
    state = create_initial_state()
    updates = load_spec_node(state)

    assert updates["status"] == "error"
    assert isinstance(updates["error"], AgentError)
    assert updates["error"].error_type == "SpecNotFoundError"


# ---------------------------------------------------------------------------
# Tests: select_operation_node
# ---------------------------------------------------------------------------

def test_select_operation_by_operation_id(sample_parsed_spec: ParsedAPISpec):
    """select_operation_node resolves operation using target_operation_id."""
    state = create_initial_state(
        spec=sample_parsed_spec,
        target_operation_id="getProductById",
    )
    updates = select_operation_node(state)

    assert updates["status"] == "operation_selected"
    assert isinstance(updates["selected_operation"], EndpointDefinition)
    assert updates["selected_operation"].operation_id == "getProductById"
    assert updates["target_method"] == "GET"
    assert updates["target_path"] == "/api/products/{id}"


def test_select_operation_by_method_and_path(sample_parsed_spec: ParsedAPISpec):
    """select_operation_node resolves operation using method and path."""
    state = create_initial_state(
        spec=sample_parsed_spec,
        target_method="POST",
        target_path="/api/products",
    )
    updates = select_operation_node(state)

    assert updates["status"] == "operation_selected"
    assert updates["selected_operation"].operation_id == "createProduct"
    assert updates["target_method"] == "POST"
    assert updates["target_path"] == "/api/products"


def test_select_operation_missing_spec():
    """select_operation_node fails gracefully if spec is missing."""
    state = create_initial_state(target_operation_id="listProducts")
    updates = select_operation_node(state)

    assert updates["status"] == "error"
    assert updates["error"].error_type == "MissingSpecificationError"


def test_select_operation_not_found(sample_parsed_spec: ParsedAPISpec):
    """select_operation_node returns structured error when operation does not exist."""
    state = create_initial_state(
        spec=sample_parsed_spec,
        target_operation_id="nonExistentOp",
    )
    updates = select_operation_node(state)

    assert updates["status"] == "error"
    assert updates["error"].error_type == "OperationNotFoundError"
    assert "nonExistentOp" in updates["error"].message


# ---------------------------------------------------------------------------
# Tests: prepare_request_node
# ---------------------------------------------------------------------------

def test_prepare_request_node_success(sample_parsed_spec: ParsedAPISpec):
    """prepare_request_node creates ExecutionRequest from selected operation."""
    op = sample_parsed_spec.endpoints[0]  # GET /api/products
    state = create_initial_state(
        spec=sample_parsed_spec,
        selected_operation=op,
        base_url="http://127.0.0.1:8081",
    )
    updates = prepare_request_node(state)

    assert updates["status"] == "request_ready"
    assert isinstance(updates["request_config"], ExecutionRequest)
    assert updates["request_config"].method == "GET"
    assert updates["request_config"].url == "http://127.0.0.1:8081/api/products"


def test_prepare_request_node_path_param_substitution(sample_parsed_spec: ParsedAPISpec):
    """prepare_request_node substitutes path parameter template values."""
    op = next(ep for ep in sample_parsed_spec.endpoints if ep.path == "/api/products/{id}")
    state = create_initial_state(
        spec=sample_parsed_spec,
        selected_operation=op,
        base_url="http://127.0.0.1:8081",
        metadata={"path_params": {"id": 101}},
    )
    updates = prepare_request_node(state)

    assert updates["status"] == "request_ready"
    assert updates["request_config"].url == "http://127.0.0.1:8081/api/products/101"


def test_prepare_request_node_headers_and_body(sample_parsed_spec: ParsedAPISpec):
    """prepare_request_node populates headers, auth token, and JSON payload."""
    op = next(ep for ep in sample_parsed_spec.endpoints if ep.method == "POST")
    state = create_initial_state(
        spec=sample_parsed_spec,
        selected_operation=op,
        base_url="http://127.0.0.1:8081",
        metadata={
            "auth_token": "valid_token_123",
            "headers": {"X-Test": "custom_val"},
        },
        json_body={"name": "Gaming Keyboard", "price": 89.99},
    )
    updates = prepare_request_node(state)

    assert updates["status"] == "request_ready"
    req = updates["request_config"]
    assert req.method == "POST"
    assert req.headers["Authorization"] == "Bearer valid_token_123"
    assert req.headers["X-Test"] == "custom_val"
    assert req.json_body == {"name": "Gaming Keyboard", "price": 89.99}


def test_prepare_request_node_missing_operation():
    """prepare_request_node fails with error if selected_operation is missing."""
    state = create_initial_state()
    updates = prepare_request_node(state)

    assert updates["status"] == "error"
    assert updates["error"].error_type == "MissingOperationError"


def test_prepare_request_node_missing_base_url(sample_parsed_spec: ParsedAPISpec):
    """prepare_request_node returns error when URL lacks base scheme."""
    op = sample_parsed_spec.endpoints[0]
    op_no_server = op.model_copy(update={"servers": []})
    spec_no_server = sample_parsed_spec.model_copy(update={"servers": []})

    state = create_initial_state(
        spec=spec_no_server,
        selected_operation=op_no_server,
    )
    updates = prepare_request_node(state)

    assert updates["status"] == "error"
    assert updates["error"].error_type == "MissingBaseUrlError"


# ---------------------------------------------------------------------------
# Tests: execute_request_node
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_execute_request_node_success():
    """execute_request_node invokes executor and stores ExecutionResult."""
    mock_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=200,
        headers={"content-type": "application/json"},
        body=[{"id": 1, "name": "Desk Lamp", "price": 25.0}],
        latency_ms=15.0,
        is_success=True,
    )

    mock_executor = MagicMock(spec=HTTPExecutor)
    mock_executor.execute = AsyncMock(return_value=mock_result)

    req = ExecutionRequest(method="GET", url="http://127.0.0.1:8081/api/products")
    state = create_initial_state(request_config=req)

    updates = await execute_request_node(state, executor=mock_executor)

    assert updates["status"] == "executed"
    assert updates["execution_result"] is mock_result
    assert updates["execution_result"].status_code == 200
    mock_executor.execute.assert_awaited_once_with(req)


@pytest.mark.anyio
async def test_execute_request_node_timeout_handling():
    """execute_request_node handles timeout results consistently."""
    timeout_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=0,
        latency_ms=10005.0,
        is_success=False,
        is_timeout=True,
        error_message="Request timed out: wall-clock deadline exceeded (10.0s limit)",
    )

    mock_executor = MagicMock(spec=HTTPExecutor)
    mock_executor.execute = AsyncMock(return_value=timeout_result)

    req = ExecutionRequest(method="GET", url="http://127.0.0.1:8081/api/products")
    state = create_initial_state(request_config=req)

    updates = await execute_request_node(state, executor=mock_executor)

    assert updates["status"] == "executed"
    assert updates["execution_result"].is_timeout is True
    assert "timed out" in updates["logs"][0].lower()


@pytest.mark.anyio
async def test_execute_request_node_missing_config():
    """execute_request_node returns error when request_config is missing."""
    state = create_initial_state()
    updates = await execute_request_node(state)

    assert updates["status"] == "error"
    assert updates["error"].error_type == "MissingRequestConfigError"


@pytest.mark.anyio
async def test_execute_request_node_exception_handling():
    """execute_request_node catches unexpected exceptions during execution."""
    mock_executor = MagicMock(spec=HTTPExecutor)
    mock_executor.execute = AsyncMock(side_effect=RuntimeError("Async connection engine failure"))

    req = ExecutionRequest(method="GET", url="http://127.0.0.1:8081/api/products")
    state = create_initial_state(request_config=req)

    updates = await execute_request_node(state, executor=mock_executor)

    assert updates["status"] == "error"
    assert updates["error"].error_type == "RuntimeError"
    assert "Async connection engine failure" in updates["error"].message


# ---------------------------------------------------------------------------
# Tests: evaluate_response_node
# ---------------------------------------------------------------------------

def test_evaluate_response_node_success(sample_parsed_spec: ParsedAPISpec):
    """evaluate_response_node passes matching status code and schema."""
    op = sample_parsed_spec.endpoints[0]  # GET /api/products
    exec_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=200,
        headers={"content-type": "application/json"},
        body=[{"id": 1, "name": "Laptop Stand", "price": 49.99}],
        latency_ms=20.0,
        is_success=True,
    )

    state = create_initial_state(
        spec=sample_parsed_spec,
        selected_operation=op,
        execution_result=exec_result,
    )
    updates = evaluate_response_node(state)

    assert updates["status"] == "evaluated"
    assert isinstance(updates["assertion_report"], AssertionReport)
    assert updates["assertion_report"].is_success is True
    assert updates["assertion_report"].status_code == 200


def test_evaluate_response_node_schema_failure(sample_parsed_spec: ParsedAPISpec):
    """evaluate_response_node identifies schema violation and records failed assertion."""
    op = sample_parsed_spec.endpoints[0]
    exec_result = ExecutionResult(
        method="GET",
        url="http://127.0.0.1:8081/api/products",
        status_code=200,
        headers={"content-type": "application/json"},
        body=[{"id": 1, "name": "Missing Price Product"}],  # 'price' missing
        latency_ms=20.0,
        is_success=True,
    )

    state = create_initial_state(
        spec=sample_parsed_spec,
        selected_operation=op,
        execution_result=exec_result,
    )
    updates = evaluate_response_node(state)

    assert updates["status"] == "evaluated"
    assert updates["assertion_report"].is_success is False
    assert len(updates["assertion_report"].failed_assertions) > 0


def test_evaluate_response_node_missing_result(sample_parsed_spec: ParsedAPISpec):
    """evaluate_response_node returns error when execution_result is missing."""
    op = sample_parsed_spec.endpoints[0]
    state = create_initial_state(
        spec=sample_parsed_spec,
        selected_operation=op,
    )
    updates = evaluate_response_node(state)

    assert updates["status"] == "error"
    assert updates["error"].error_type == "MissingExecutionResultError"


# ---------------------------------------------------------------------------
# Tests: Pipeline Transitions & Service Invocation Verification
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_full_pipeline_step_by_step_transitions(sample_spec_dict: dict):
    """Verifies sequential state transitions across all 5 nodes in sequence."""
    state = create_initial_state(
        raw_spec=sample_spec_dict,
        target_operation_id="listProducts",
    )

    # 1. Load Spec -> spec_loaded
    s1 = load_spec_node(state)
    assert s1["status"] == "spec_loaded"
    state.update(s1)

    # 2. Select Operation -> operation_selected
    s2 = select_operation_node(state)
    assert s2["status"] == "operation_selected"
    state.update(s2)

    # 3. Prepare Request -> request_ready
    s3 = prepare_request_node(state)
    assert s3["status"] == "request_ready"
    state.update(s3)

    # 4. Execute Request -> executed
    mock_result = ExecutionResult(
        method="GET",
        url=state["request_config"].url,
        status_code=200,
        headers={"content-type": "application/json"},
        body=[{"id": 1, "name": "Item", "price": 10.0}],
        latency_ms=10.0,
        is_success=True,
    )
    mock_exec = MagicMock(spec=HTTPExecutor)
    mock_exec.execute = AsyncMock(return_value=mock_result)

    s4 = await execute_request_node(state, executor=mock_exec)
    assert s4["status"] == "executed"
    state.update(s4)

    # 5. Evaluate Response -> evaluated
    s5 = evaluate_response_node(state)
    assert s5["status"] == "evaluated"
    state.update(s5)

    assert state["status"] == "evaluated"
    assert state["assertion_report"].is_success is True


def test_existing_services_invoked(sample_spec_dict: dict):
    """
    Verifies existing services (OpenAPIParser, DeterministicAssertionEvaluator)
    are invoked rather than duplicate logic being written.
    """
    with patch.object(OpenAPIParser, "parse_dict", wraps=OpenAPIParser().parse_dict) as mock_parser:
        state = create_initial_state(raw_spec=sample_spec_dict)
        load_spec_node(state)
        mock_parser.assert_called_once()
