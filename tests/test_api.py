"""
Comprehensive Integration and Unit Tests for FastAPI REST API Layer (Phase 5C).
Verifies:
  1. System health check endpoint (/health).
  2. Project API lifecycle: creation from OpenAPI spec, retrieval, pagination, endpoint listing, and deletion.
  3. Error responses on invalid YAML/JSON syntax, schema validation errors, and empty payloads.
  4. Run API lifecycle: background execution dispatch (202 Accepted), status retrieval, project runs listing, and cancellation.
  5. Diagnostic report API: structured JSON export and safe Markdown export (?format=markdown).
  6. 409 Conflict rejection when attempting to retrieve reports for in-flight/pending runs.
  7. Centralized exception handling, validation errors, and sanitized error responses.
"""

from __future__ import annotations

from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

from fastapi import status
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.api.deps import get_db, get_run_service
from backend.app.core.database import build_engine, close_db, drop_db, init_db
from backend.app.main import app
from backend.app.models.base import utc_now
from backend.app.repositories import create_project, create_report, create_run, update_run_status
from backend.app.schemas.failure import FailureCategory
from backend.app.schemas.report import (
    AttemptOutcomeSummary,
    DiagnosticReport,
    OperationRunRecord,
    OverallRunStatus,
    RunSummaryStatistics,
)
from backend.app.services.run_service import RunService

SAMPLE_OPENAPI_YAML = """
openapi: "3.0.3"
info:
  title: "API Gateway Inventory"
  version: "2.1.0"
servers:
  - url: "https://gateway.example.com/api"
paths:
  /products:
    get:
      summary: "List products"
      operationId: "listProducts"
      responses:
        "200":
          description: "List of products"
    post:
      summary: "Create product"
      operationId: "createProduct"
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: "object"
              properties:
                name:
                  type: "string"
      responses:
        "201":
          description: "Created"
"""


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Provides a fresh, isolated in-memory SQLite async engine per test."""
    eng = build_engine("sqlite+aiosqlite:///:memory:", echo=False)
    await init_db(eng)
    yield eng
    await drop_db(eng)
    await close_db(eng)


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Provides an async sessionmaker bound to the isolated in-memory test database."""
    return async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@pytest.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    """Provides an isolated AsyncSession bound to the in-memory test database."""
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI test client with dependency overrides for isolated test database sessions."""
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    def override_get_run_service() -> RunService:
        return RunService(session_factory=session_factory)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_run_service] = override_get_run_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Health Check Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_health_check(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == "ok"
    assert "environment" in data
    assert "timestamp" in data


# ---------------------------------------------------------------------------
# 2. Project API Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_create_project_success(client: AsyncClient):
    payload = {
        "name": "Gateway Service",
        "description": "API Gateway spec test",
        "spec_content": SAMPLE_OPENAPI_YAML,
        "base_url_override": "https://custom.gateway.io",
    }
    response = await client.post("/api/v1/projects", json=payload)
    assert response.status_code == status.HTTP_201_CREATED
    data = response.json()
    assert data["name"] == "Gateway Service"
    assert data["spec_title"] == "API Gateway Inventory"
    assert data["spec_version"] == "2.1.0"
    assert data["base_url"] == "https://custom.gateway.io"
    assert data["endpoints_count"] == 2
    assert len(data["endpoints"]) == 2

    # Verify extracted endpoints
    paths = {ep["path"] for ep in data["endpoints"]}
    assert "/products" in paths


@pytest.mark.anyio
async def test_create_project_invalid_syntax(client: AsyncClient):
    payload = {
        "name": "Bad YAML",
        "spec_content": "openapi: [unclosed bracket",
    }
    response = await client.post("/api/v1/projects", json=payload)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    data = response.json()
    assert "Invalid specification syntax" in data["detail"] or "SpecParsingError" in data.get("error_type", "")


@pytest.mark.anyio
async def test_create_project_validation_error(client: AsyncClient):
    # Empty project name triggers Pydantic RequestValidationError (422)
    payload = {
        "name": "",
        "spec_content": SAMPLE_OPENAPI_YAML,
    }
    response = await client.post("/api/v1/projects", json=payload)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.anyio
async def test_list_and_get_projects(client: AsyncClient, db_session: AsyncSession):
    # Setup projects directly via repo
    p1 = await create_project(db_session, name="Alpha API", spec_title="Alpha", spec_version="1.0")
    p2 = await create_project(db_session, name="Beta API", spec_title="Beta", spec_version="2.0")
    await db_session.commit()

    # List projects
    response = await client.get("/api/v1/projects?skip=0&limit=10")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert len(data) >= 2
    names = [p["name"] for p in data]
    assert "Alpha API" in names
    assert "Beta API" in names

    # Get specific project
    get_res = await client.get(f"/api/v1/projects/{p1.id}")
    assert get_res.status_code == status.HTTP_200_OK
    assert get_res.json()["id"] == p1.id
    assert get_res.json()["name"] == "Alpha API"

    # Get non-existent project
    not_found_res = await client.get("/api/v1/projects/00000000-0000-0000-0000-000000000000")
    assert not_found_res.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.anyio
async def test_list_project_endpoints(client: AsyncClient):
    # Create project through API
    payload = {
        "name": "Endpoint Query Project",
        "spec_content": SAMPLE_OPENAPI_YAML,
    }
    create_res = await client.post("/api/v1/projects", json=payload)
    project_id = create_res.json()["id"]

    # Query endpoints
    res = await client.get(f"/api/v1/projects/{project_id}/endpoints")
    assert res.status_code == status.HTTP_200_OK
    endpoints = res.json()
    assert len(endpoints) == 2
    methods = {ep["method"] for ep in endpoints}
    assert "GET" in methods
    assert "POST" in methods


@pytest.mark.anyio
async def test_delete_project_cascade(client: AsyncClient, db_session: AsyncSession):
    project = await create_project(db_session, name="To Delete")
    await db_session.commit()

    # Delete project
    del_res = await client.delete(f"/api/v1/projects/{project.id}")
    assert del_res.status_code == status.HTTP_204_NO_CONTENT

    # Verify not found
    get_res = await client.get(f"/api/v1/projects/{project.id}")
    assert get_res.status_code == status.HTTP_404_NOT_FOUND

    # Delete non-existent project returns 404
    del_again = await client.delete(f"/api/v1/projects/{project.id}")
    assert del_again.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# 3. Run API Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_create_and_inspect_run(client: AsyncClient, db_session: AsyncSession):
    project = await create_project(
        db_session,
        name="Run Target Project",
        spec_raw=SAMPLE_OPENAPI_YAML,
    )
    await db_session.commit()

    # Mock execute_run so background task does not invoke real network orchestrator during API test
    with patch.object(RunService, "execute_run", AsyncMock()) as mock_exec:
        payload = {
            "concurrency": 2,
            "target_endpoints": ["/products"],
            "custom_base_url": "https://test.local",
            "safe_dev_mode": True,
        }
        response = await client.post(f"/api/v1/projects/{project.id}/runs", json=payload)
        assert response.status_code == status.HTTP_202_ACCEPTED
        data = response.json()
        assert data["project_id"] == project.id
        assert data["status"] == "pending"
        assert data["concurrency"] == 2
        assert data["target_endpoints"] == ["/products"]

        run_id = data["id"]

    # Verify run details can be retrieved
    get_run_res = await client.get(f"/api/v1/runs/{run_id}")
    assert get_run_res.status_code == status.HTTP_200_OK
    assert get_run_res.json()["id"] == run_id

    # List project runs
    list_runs_res = await client.get(f"/api/v1/projects/{project.id}/runs")
    assert list_runs_res.status_code == status.HTTP_200_OK
    assert len(list_runs_res.json()) >= 1


@pytest.mark.anyio
async def test_cancel_run(client: AsyncClient, db_session: AsyncSession):
    project = await create_project(db_session, name="Cancel Run Project")
    run = await create_run(db_session, project_id=project.id, status="running")
    await db_session.commit()

    response = await client.post(f"/api/v1/runs/{run.id}/cancel")
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == "cancelled"
    assert data["completed_at"] is not None

    # Cancel non-existent run
    res404 = await client.post("/api/v1/runs/00000000-0000-0000-0000-000000000000/cancel")
    assert res404.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# 4. Diagnostic Report API Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_diagnostic_report_json_and_markdown(
    client: AsyncClient,
    db_session: AsyncSession,
):
    project = await create_project(db_session, name="Report Project")
    run = await create_run(db_session, project_id=project.id, status="completed")
    await update_run_status(
        db_session,
        run_id=run.id,
        status="completed",
        completed_at=utc_now(),
        duration_ms=120.0,
    )

    # Persist a DiagnosticReport
    report_schema = DiagnosticReport(
        report_id="rep_api_test_01",
        timestamp="2026-11-06T11:20:00Z",
        api_title="Gateway Inventory",
        api_version="2.1.0",
        base_url="https://gateway.example.com",
        summary=RunSummaryStatistics(
            total_operations=0,
            passed_operations=0,
            resolved_by_transient_retry=0,
            failed_operations=0,
            error_operations=0,
            total_transient_retries_dispatched=0,
            total_requests_executed=0,
            pass_rate_percentage=0.0,
            total_duration_ms=120.0,
        ),
        operations=(),
        global_remediation_summary=("Remediation note 1",),
    )

    await create_report(
        session=db_session,
        run_id=run.id,
        report=report_schema,
        markdown_rendered="# Gateway Inventory Diagnostic Report\n\nAll operations passed.",
    )
    await db_session.commit()

    # 1. Retrieve as JSON (default)
    json_res = await client.get(f"/api/v1/runs/{run.id}/report")
    assert json_res.status_code == status.HTTP_200_OK
    json_data = json_res.json()
    assert json_data["report_id"] == "rep_api_test_01"
    assert json_data["api_title"] == "Gateway Inventory"
    assert json_data["summary"]["total_duration_ms"] == 120.0

    # 2. Retrieve as safe Markdown (?format=markdown)
    md_res = await client.get(f"/api/v1/runs/{run.id}/report?format=markdown")
    assert md_res.status_code == status.HTTP_200_OK
    assert "text/markdown" in md_res.headers.get("content-type", "")
    assert "# Gateway Inventory Diagnostic Report" in md_res.text


@pytest.mark.anyio
async def test_get_diagnostic_report_conflicts_and_not_found(
    client: AsyncClient,
    db_session: AsyncSession,
):
    project = await create_project(db_session, name="Conflict Project")
    running_run = await create_run(db_session, project_id=project.id, status="running")
    await db_session.commit()

    # In-flight run should return 409 Conflict
    res_conflict = await client.get(f"/api/v1/runs/{running_run.id}/report")
    assert res_conflict.status_code == status.HTTP_409_CONFLICT
    assert "not yet available" in res_conflict.json()["detail"]

    # Non-existent run should return 404 Not Found
    res_404 = await client.get("/api/v1/runs/00000000-0000-0000-0000-000000000000/report")
    assert res_404.status_code == status.HTTP_404_NOT_FOUND
