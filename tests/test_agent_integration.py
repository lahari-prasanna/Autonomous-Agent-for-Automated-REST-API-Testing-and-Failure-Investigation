"""
End-to-End Integration Test Suite for API Testing Agent (Phase 3D).
Validates full LangGraph workflow execution against the Controlled Mock REST API:
  - Same-process background Uvicorn server on an already-bound ephemeral socket (zero port race).
  - Zero-production-change ASGI delay wrapper for deterministic timeout testing.
  - In-memory database isolation resetting mock_api.main.PRODUCTS_DB between tests.
  - Empirical proof that HTTP 2xx, 401, 404, and 500 responses reach assertion evaluation.
  - Complete verification of transport failures, timeouts, and SSRF blocks terminating before evaluation.
"""

import asyncio
import copy
import socket
import threading
import time
from typing import Generator
import httpx
import pytest
import uvicorn

import mock_api.main
from backend.app.agent.graph import agent_graph
from backend.app.agent.state import AgentError, create_initial_state
from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.spec import ParsedAPISpec
from backend.app.services.assertion_evaluator import AssertionReport
from backend.app.services.spec_parser import OpenAPIParser


# ---------------------------------------------------------------------------
# Test-Only ASGI Middleware for Deterministic Delay
# ---------------------------------------------------------------------------

class DelayASGIWrapper:
    """
    Test-only ASGI wrapper around mock_api.main.app.
    Allows tests to inject a deterministic asynchronous delay via the 'X-Test-Delay'
    header without modifying any production code in mock_api/main.py.
    """
    __test__ = False  # Prevent pytest from collecting this class as a test suite

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            if b"x-test-delay" in headers:
                try:
                    delay_seconds = float(headers[b"x-test-delay"].decode())
                    await asyncio.sleep(delay_seconds)
                except ValueError:
                    pass
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Pytest Fixtures: Server Lifecycle, Isolation, and Constants
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_mock_server() -> Generator[str, None, None]:
    """
    Starts the Controlled Mock REST API in a background thread within the same process.
    Binds an ephemeral socket to 127.0.0.1:0 and passes the already-bound socket
    directly to Uvicorn to eliminate port race conditions.
    """
    # 1. Bind socket to an ephemeral loopback port and keep it open
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    # 2. Configure Uvicorn with delay-capable ASGI wrapper
    wrapped_app = DelayASGIWrapper(mock_api.main.app)
    config = uvicorn.Config(
        app=wrapped_app,
        log_level="error",
        lifespan="off",
    )
    server = uvicorn.Server(config=config)

    # 3. Launch in daemon thread passing the already-bound socket
    server_thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    server_thread.start()

    # 4. Reliable readiness check verifying server safety header
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

    # 5. Clean teardown
    server.should_exit = True
    server_thread.join(timeout=2.0)


@pytest.fixture(autouse=True)
def reset_mock_db() -> Generator[None, None, None]:
    """
    Autouse fixture that snapshots mock_api.main.PRODUCTS_DB and _next_id
    before each test and restores them immediately upon completion.
    Provides complete test isolation in the shared Python process.
    """
    initial_db = copy.deepcopy(mock_api.main.PRODUCTS_DB)
    initial_next_id = mock_api.main._next_id
    yield
    mock_api.main.PRODUCTS_DB.clear()
    mock_api.main.PRODUCTS_DB.update(copy.deepcopy(initial_db))
    mock_api.main._next_id = initial_next_id


@pytest.fixture
def mock_spec_file() -> str:
    """Returns the local path to the real OpenAPI specification."""
    return "mock_api/openapi.json"


@pytest.fixture
def auth_token() -> str:
    """Returns the authorized Bearer token recognized by mock_api."""
    return mock_api.main.VALID_TOKEN


# ---------------------------------------------------------------------------
# Scenario 1: GET Products List (200 OK — Successful Read)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_get_products_success(live_mock_server: str, mock_spec_file: str):
    """Scenario 1: End-to-end listProducts execution succeeds with HTTP 200 and schema validation."""
    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="GET",
        target_path="/api/products",
        metadata={"base_url": live_mock_server, "safe_dev_mode": True},
    )

    final_state = await agent_graph.ainvoke(initial_state)

    # 1. State reached evaluated
    assert final_state["status"] == "evaluated"
    assert final_state.get("error") is None

    # 2. Execution envelope
    res = final_state["execution_result"]
    assert isinstance(res, ExecutionResult)
    assert res.status_code == 200
    assert res.is_success is True
    assert isinstance(res.body, list)
    assert len(res.body) >= 3

    # 3. Assertion report
    report = final_state["assertion_report"]
    assert isinstance(report, AssertionReport)
    assert report.is_success is True
    assert report.status_code == 200
    assert report.matched_status_key == "200"


# ---------------------------------------------------------------------------
# Scenario 2: GET Product by ID with Path Substitution (200 OK)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_get_product_by_id_path_substitution(live_mock_server: str, mock_spec_file: str):
    """Scenario 2: Template /api/products/{product_id} substituted with ID 1 and succeeds."""
    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="GET",
        target_path="/api/products/{product_id}",
        metadata={
            "base_url": live_mock_server,
            "path_params": {"product_id": 1},
            "safe_dev_mode": True,
        },
    )

    final_state = await agent_graph.ainvoke(initial_state)

    assert final_state["status"] == "evaluated"
    assert final_state.get("error") is None

    res = final_state["execution_result"]
    assert res.status_code == 200
    assert res.body["id"] == 1
    assert res.body["name"] == "Wireless Mouse"

    report = final_state["assertion_report"]
    assert report.is_success is True
    assert report.status_code == 200


# ---------------------------------------------------------------------------
# Scenario 3: POST Create Product Authenticated (201 Created — Successful Write)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_create_product_authenticated_success(
    live_mock_server: str, mock_spec_file: str, auth_token: str
):
    """Scenario 3: Authenticated product creation succeeds with HTTP 201 and persists to DB."""
    product_payload = {
        "name": "Noise Cancelling Headphones",
        "price": 199.99,
        "category": "Electronics",
        "description": "Premium over-ear wireless headphones",
    }

    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="POST",
        target_path="/api/products",
        request_config=ExecutionRequest(
            method="POST",
            url=f"{live_mock_server}/api/products",
            headers={"Authorization": f"Bearer {auth_token}"},
            json_body=product_payload,
        ),
        metadata={"safe_dev_mode": True},
    )

    final_state = await agent_graph.ainvoke(initial_state)

    assert final_state["status"] == "evaluated"
    assert final_state.get("error") is None

    res = final_state["execution_result"]
    assert res.status_code == 201
    assert res.body["name"] == "Noise Cancelling Headphones"
    assert res.body["price"] == 199.99
    new_id = res.body["id"]

    # Verify DB update reflected in shared memory
    assert new_id in mock_api.main.PRODUCTS_DB
    assert mock_api.main.PRODUCTS_DB[new_id]["name"] == "Noise Cancelling Headphones"

    report = final_state["assertion_report"]
    assert report.is_success is True
    assert report.status_code == 201


# ---------------------------------------------------------------------------
# Scenario 4: DELETE Product (204 No Content — Successful Delete)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_delete_product_204_no_content(
    live_mock_server: str, mock_spec_file: str, auth_token: str
):
    """Scenario 4: Deleting product 3 returns HTTP 204 with valid empty body schema."""
    assert 3 in mock_api.main.PRODUCTS_DB

    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="DELETE",
        target_path="/api/products/{product_id}",
        metadata={
            "base_url": live_mock_server,
            "auth_token": auth_token,
            "path_params": {"product_id": 3},
            "safe_dev_mode": True,
        },
    )

    final_state = await agent_graph.ainvoke(initial_state)

    assert final_state["status"] == "evaluated"
    assert final_state.get("error") is None

    res = final_state["execution_result"]
    assert res.status_code == 204
    assert res.raw_body_bytes_len == 0

    # Verify item deleted from DB
    assert 3 not in mock_api.main.PRODUCTS_DB

    report = final_state["assertion_report"]
    assert report.is_success is True
    assert report.status_code == 204


# ---------------------------------------------------------------------------
# Scenario 5: 401 Unauthorized Reaches Evaluator
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_unauthorized_401_reaches_evaluator(
    live_mock_server: str, mock_spec_file: str
):
    """
    Scenario 5: Requesting createProduct without Authorization header returns HTTP 401.
    Empirically verifies that HTTP 401 reaches evaluate_response_node and is evaluated.
    """
    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="POST",
        target_path="/api/products",
        request_config=ExecutionRequest(
            method="POST",
            url=f"{live_mock_server}/api/products",
            # Intentionally no auth header
            json_body={"name": "Unauthorized Item", "price": 45.0, "category": "Books"},
        ),
        metadata={"safe_dev_mode": True},
    )

    final_state = await agent_graph.ainvoke(initial_state)

    # 1. Pipeline reached evaluated status (NOT stopped at execute_request)
    assert final_state["status"] == "evaluated"

    # 2. Execution result captured HTTP 401
    res = final_state["execution_result"]
    assert res.status_code == 401
    assert res.is_success is True  # HTTP exchange completed
    assert "detail" in res.body

    # 3. Assertion report evaluates HTTP 401
    report = final_state["assertion_report"]
    assert isinstance(report, AssertionReport)
    assert report.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 6: 404 Not Found Reaches Evaluator
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_not_found_404_reaches_evaluator(
    live_mock_server: str, mock_spec_file: str
):
    """
    Scenario 6: Querying non-existent product_id 99999 returns HTTP 404.
    Empirically verifies that HTTP 404 reaches evaluate_response_node.
    """
    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="GET",
        target_path="/api/products/{product_id}",
        metadata={
            "base_url": live_mock_server,
            "path_params": {"product_id": 99999},
            "safe_dev_mode": True,
        },
    )

    final_state = await agent_graph.ainvoke(initial_state)

    assert final_state["status"] == "evaluated"

    res = final_state["execution_result"]
    assert res.status_code == 404
    assert res.is_success is True

    report = final_state["assertion_report"]
    assert isinstance(report, AssertionReport)
    assert report.status_code == 404
    assert report.matched_status_key is None
    assert report.is_success is False
    assert any("404" in a.message for a in report.failed_assertions)


# ---------------------------------------------------------------------------
# Scenario 7: 400 Bad Request Boundary Reaches Evaluator
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_bad_request_400_validation_boundary(
    live_mock_server: str, mock_spec_file: str, auth_token: str
):
    """
    Scenario 7: Product creation with price=0 returns HTTP 400 (validation boundary).
    Empirically verifies that HTTP 400 reaches evaluate_response_node.
    """
    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="POST",
        target_path="/api/products",
        request_config=ExecutionRequest(
            method="POST",
            url=f"{live_mock_server}/api/products",
            headers={"Authorization": f"Bearer {auth_token}"},
            json_body={"name": "Zero Price Item", "price": 0.0, "category": "General"},
        ),
        metadata={"safe_dev_mode": True},
    )

    final_state = await agent_graph.ainvoke(initial_state)

    assert final_state["status"] == "evaluated"

    res = final_state["execution_result"]
    assert res.status_code == 400
    assert res.is_success is True

    report = final_state["assertion_report"]
    assert isinstance(report, AssertionReport)
    assert report.status_code == 400


# ---------------------------------------------------------------------------
# Scenario 8: 500 Server Defect Reaches Evaluator
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_server_defect_500_reaches_evaluator(
    live_mock_server: str, mock_spec_file: str, auth_token: str
):
    """
    Scenario 8: Product creation with price=-500 triggers intentional server defect (HTTP 500).
    Empirically verifies that HTTP 500 reaches evaluate_response_node and fails assertions.
    """
    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="POST",
        target_path="/api/products",
        request_config=ExecutionRequest(
            method="POST",
            url=f"{live_mock_server}/api/products",
            headers={"Authorization": f"Bearer {auth_token}"},
            json_body={"name": "Defect Negative Item", "price": -500.0, "category": "General"},
        ),
        metadata={"safe_dev_mode": True},
    )

    final_state = await agent_graph.ainvoke(initial_state)

    # 1. Successfully reached evaluated status
    assert final_state["status"] == "evaluated"

    # 2. Captured HTTP 500
    res = final_state["execution_result"]
    assert res.status_code == 500
    assert res.is_success is True

    # 3. Assertion report flags undeclared 500 error as failed assertions
    report = final_state["assertion_report"]
    assert isinstance(report, AssertionReport)
    assert report.status_code == 500
    assert report.is_success is False
    assert any("500" in a.message for a in report.failed_assertions)


# ---------------------------------------------------------------------------
# Scenario 9: Invalid Specification Early Termination
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_invalid_specification_early_termination():
    """Scenario 9: Invalid spec terminates at load_spec with error; zero HTTP requests made."""
    initial_state = create_initial_state(
        raw_spec={"invalid": "syntax", "no_openapi_field": True},
        target_method="GET",
        target_path="/api/products",
    )

    final_state = await agent_graph.ainvoke(initial_state)

    assert final_state["status"] == "error"
    assert isinstance(final_state["error"], AgentError)
    assert final_state["error"].step == "spec_loaded"

    # Subsequent pipeline envelopes are None
    assert final_state.get("selected_operation") is None
    assert final_state.get("request_config") is None
    assert final_state.get("execution_result") is None
    assert final_state.get("assertion_report") is None


# ---------------------------------------------------------------------------
# Scenario 10: Missing Operation Early Termination
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_invalid_operation_early_termination(mock_spec_file: str):
    """Scenario 10: Non-existent operation ID terminates at select_operation with error."""
    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_operation_id="ghost_non_existent_operation_999",
    )

    final_state = await agent_graph.ainvoke(initial_state)

    assert final_state["status"] == "error"
    assert isinstance(final_state["error"], AgentError)
    assert final_state["error"].step == "operation_selected"
    assert "OperationNotFoundError" in final_state["error"].error_type

    assert final_state.get("request_config") is None
    assert final_state.get("execution_result") is None
    assert final_state.get("assertion_report") is None


# ---------------------------------------------------------------------------
# Scenario 11: Missing Base URL Early Termination
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_missing_base_url_early_termination(mock_spec_file: str):
    """Scenario 11: Spec without servers and no base_url terminates at prepare_request."""
    parsed_spec = OpenAPIParser.parse_file(mock_spec_file)
    spec_no_servers = parsed_spec.model_copy(update={"servers": []})
    op = spec_no_servers.endpoints[0].model_copy(update={"servers": []})

    initial_state = create_initial_state(
        spec=spec_no_servers,
        selected_operation=op,
        # Intentionally no base_url
    )

    final_state = await agent_graph.ainvoke(initial_state)

    assert final_state["status"] == "error"
    assert isinstance(final_state["error"], AgentError)
    assert final_state["error"].step == "request_ready"
    assert "MissingBaseUrlError" in final_state["error"].error_type

    assert final_state.get("execution_result") is None
    assert final_state.get("assertion_report") is None


# ---------------------------------------------------------------------------
# Scenario 12: Deterministic Execution Timeout Terminates Without Evaluating
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_execution_timeout_terminates_without_eval(
    live_mock_server: str, mock_spec_file: str
):
    """
    Scenario 12: Server delay (1.5s) exceeds client timeout (0.2s).
    Workflow terminates at execute_request with is_timeout=True without evaluating assertions.
    """
    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="GET",
        target_path="/api/products",
        metadata={
            "base_url": live_mock_server,
            "headers": {"X-Test-Delay": "1.5"},
            "timeout": 0.2,  # 200ms limit vs 1500ms delay
            "safe_dev_mode": True,
        },
    )

    final_state = await agent_graph.ainvoke(initial_state)

    # 1. State recorded executed with timeout
    assert final_state["status"] == "executed"
    res = final_state["execution_result"]
    assert res.is_timeout is True
    assert res.is_success is False
    assert res.status_code == 0

    # 2. Crucial: evaluate_response_node was NEVER invoked
    assert final_state.get("assertion_report") is None


# ---------------------------------------------------------------------------
# Scenario 13: Network Connection Error Terminates Without Evaluating
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_network_connection_error_terminates(mock_spec_file: str):
    """
    Scenario 13: Requesting an unbound loopback port triggers socket connection refusal.
    Workflow terminates at execute_request with is_network_error=True without evaluating.
    """
    # Find an unused, unbound loopback port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    unbound_port = s.getsockname()[1]
    s.close()

    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="GET",
        target_path="/api/products",
        metadata={
            "base_url": f"http://127.0.0.1:{unbound_port}",
            "safe_dev_mode": True,
        },
    )

    final_state = await agent_graph.ainvoke(initial_state)

    assert final_state["status"] == "executed"
    res = final_state["execution_result"]
    assert res.is_network_error is True
    assert res.is_success is False
    assert res.status_code == 0

    # Crucial: evaluate_response_node was NEVER invoked
    assert final_state.get("assertion_report") is None


# ---------------------------------------------------------------------------
# Scenario 14: SSRF Guard Blocked Destination Terminates Without Evaluating
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_e2e_ssrf_blocked_destination_terminates(mock_spec_file: str):
    """
    Scenario 14: Requesting a cloud metadata address (169.254.169.254) is blocked by SSRF guard.
    Workflow terminates at execute_request with is_ssrf_blocked=True without evaluating.
    """
    initial_state = create_initial_state(
        raw_spec=mock_spec_file,
        target_method="GET",
        target_path="/api/products",
        metadata={
            "base_url": "http://169.254.169.254",
            "safe_dev_mode": True,  # Even in safe dev mode, metadata IPs remain strictly blocked
        },
    )

    final_state = await agent_graph.ainvoke(initial_state)

    assert final_state["status"] == "executed"
    res = final_state["execution_result"]
    assert res.is_ssrf_blocked is True
    assert res.is_success is False
    assert res.status_code == 0

    # Crucial: evaluate_response_node was NEVER invoked
    assert final_state.get("assertion_report") is None
