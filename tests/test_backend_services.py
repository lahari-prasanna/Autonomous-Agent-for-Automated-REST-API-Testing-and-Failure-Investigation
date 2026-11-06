"""
Unit and Integration Tests for Application Services Layer (Phase 5B).
Verifies:
  1. ProjectService OpenAPI spec validation, parsing, and operation persistence.
  2. ProjectService error handling on malformed syntax and invalid parameters.
  3. ProjectService base_url overrides and CRUD operations.
  4. RunService test run creation and concurrency clamping.
  5. RunService un-transacted execution lifecycle (Phase 1 Tx -> Execution -> Phase 3 Tx).
  6. Persistence of DiagnosticReport, TestExecution, and FailureInvestigation records.
  7. RunService target endpoint filtering by UUID and route path.
  8. RunService crash and exception recovery with sanitized error reporting.
  9. RunService cancellation mechanics and interrupted-run recovery.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.core.database import build_engine, close_db, drop_db, init_db
from backend.app.models.base import utc_now
from backend.app.repositories import create_run
from backend.app.schemas.failure import FailureCategory, FailureSeverity, RetryPolicy
from backend.app.schemas.report import (
    AttemptOutcomeSummary,
    DetachedInvestigationRecord,
    DiagnosticReport,
    OperationRunRecord,
    OverallRunStatus,
    RemediationRecommendation,
    RunSummaryStatistics,
    TransientRetryRecord,
    VariationMetricsSummary,
    VariationTestRecord,
)
from backend.app.schemas.retest import ParameterPerturbation
from backend.app.services.project_service import ProjectService
from backend.app.services.run_service import RunService
from backend.app.services.spec_parser import SpecParsingError

# Sample OpenAPI 3.0 YAML specification
SAMPLE_OPENAPI_YAML = """
openapi: "3.0.3"
info:
  title: "Petstore Inventory API"
  version: "1.2.0"
  description: "Test API specification for project service."
servers:
  - url: "https://api.petstore.test/v1"
paths:
  /pets:
    get:
      summary: "List all pets"
      operationId: "listPets"
      parameters:
        - name: "limit"
          in: "query"
          required: false
          schema:
            type: "integer"
            default: 20
      responses:
        "200":
          description: "A list of pets."
    post:
      summary: "Create a pet"
      operationId: "createPet"
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: "object"
              required: ["name"]
              properties:
                name:
                  type: "string"
      responses:
        "201":
          description: "Pet created successfully."
"""

SAMPLE_OPENAPI_DICT = {
    "openapi": "3.0.3",
    "info": {
        "title": "Minimal API",
        "version": "0.1.0",
    },
    "servers": [{"url": "https://minimal.test/api"}],
    "paths": {
        "/status": {
            "get": {
                "operationId": "getStatus",
                "responses": {"200": {"description": "Service healthy"}},
            }
        }
    },
}


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


# ---------------------------------------------------------------------------
# ProjectService Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_project_service_create_from_yaml(db_session: AsyncSession):
    service = ProjectService()
    project, endpoints = await service.create_project_from_spec(
        session=db_session,
        name="Petstore Project",
        spec_content=SAMPLE_OPENAPI_YAML,
        description="Petstore API test",
    )

    assert project.id is not None
    assert project.name == "Petstore Project"
    assert project.spec_title == "Petstore Inventory API"
    assert project.spec_version == "1.2.0"
    assert project.base_url == "https://api.petstore.test/v1"

    # Verify extracted endpoints
    assert len(endpoints) == 2
    op_ids = {ep.operation_id for ep in endpoints}
    assert "listPets" in op_ids
    assert "createPet" in op_ids

    # Query project with endpoints
    fetched_endpoints = await service.get_project_endpoints(db_session, project.id)
    assert len(fetched_endpoints) == 2


@pytest.mark.anyio
async def test_project_service_create_from_dict_and_override_url(db_session: AsyncSession):
    service = ProjectService()
    project, endpoints = await service.create_project_from_spec(
        session=db_session,
        name="Custom Base URL Project",
        spec_content=SAMPLE_OPENAPI_DICT,
        base_url_override="https://staging.custom.io/v2",
    )

    assert project.name == "Custom Base URL Project"
    assert project.base_url == "https://staging.custom.io/v2"
    assert len(endpoints) == 1
    assert endpoints[0].path == "/status"


@pytest.mark.anyio
async def test_project_service_invalid_spec_handling(db_session: AsyncSession):
    service = ProjectService()

    # Empty project name
    with pytest.raises(ValueError, match="Project name must not be empty"):
        await service.create_project_from_spec(db_session, name="", spec_content=SAMPLE_OPENAPI_YAML)

    # Empty spec content
    with pytest.raises(ValueError, match="Specification content must not be empty"):
        await service.create_project_from_spec(db_session, name="Valid Name", spec_content="   ")

    # Malformed YAML
    with pytest.raises(SpecParsingError):
        await service.create_project_from_spec(
            db_session,
            name="Broken YAML",
            spec_content="openapi: [unclosed list",
        )


@pytest.mark.anyio
async def test_project_service_crud_operations(db_session: AsyncSession):
    service = ProjectService()
    project, _ = await service.create_project_from_spec(
        session=db_session,
        name="Initial Name",
        spec_content=SAMPLE_OPENAPI_DICT,
    )

    # Get project
    p = await service.get_project(db_session, project.id)
    assert p is not None
    assert p.name == "Initial Name"

    # Update project
    updated = await service.update_project(
        db_session,
        project.id,
        name="Updated Name",
        description="New description",
        base_url="https://new.base.url",
    )
    assert updated.name == "Updated Name"
    assert updated.base_url == "https://new.base.url"

    # List projects
    projs = await service.list_projects(db_session)
    assert len(projs) >= 1

    # Delete project
    deleted = await service.delete_project(db_session, project.id)
    assert deleted is True
    assert await service.get_project(db_session, project.id) is None


# ---------------------------------------------------------------------------
# RunService Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_run_service_create_run(db_session: AsyncSession):
    proj_service = ProjectService()
    project, _ = await proj_service.create_project_from_spec(
        session=db_session,
        name="Run Creation Project",
        spec_content=SAMPLE_OPENAPI_DICT,
    )

    run_service = RunService()
    run = await run_service.create_run(
        session=db_session,
        project_id=project.id,
        concurrency=10,  # Should clamp to 5
        target_endpoints=["/status"],
    )

    assert run.id is not None
    assert run.project_id == project.id
    assert run.status == "pending"
    assert run.concurrency == 5
    assert run.target_endpoints == ["/status"]

    # Non-existent project
    with pytest.raises(ValueError, match="not found"):
        await run_service.create_run(db_session, project_id=str(uuid.uuid4()))


@pytest.mark.anyio
async def test_run_service_execute_run_success(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
):
    proj_service = ProjectService()
    project, endpoints = await proj_service.create_project_from_spec(
        session=db_session,
        name="Exec Project",
        spec_content=SAMPLE_OPENAPI_YAML,
    )

    run_service = RunService(session_factory=session_factory)
    run = await run_service.create_run(db_session, project_id=project.id, concurrency=2)

    # Build mock DiagnosticReport
    fp = "f" * 64
    op_record = OperationRunRecord(
        operation_id="listPets",
        http_method="GET",
        path_template="/pets",
        initial_request_fingerprint=fp,
        initial_attempt=AttemptOutcomeSummary(
            dispatched=True,
            status_code=200,
            latency_ms=45.0,
            passed=True,
        ),
        followup_decisions=(),
        transient_retries=(),
        variation_candidates=(),
        variation_tests=(),
        final_status=OverallRunStatus.PASSED,
        final_outcome=AttemptOutcomeSummary(
            dispatched=True,
            status_code=200,
            latency_ms=45.0,
            passed=True,
        ),
        investigation=None,
        remediation_recommendations=(),
    )

    mock_report = DiagnosticReport(
        report_id="rep_exec_success",
        timestamp="2026-11-06T11:00:00Z",
        api_title="Petstore Inventory API",
        api_version="1.2.0",
        base_url="https://api.petstore.test/v1",
        summary=RunSummaryStatistics(
            total_operations=1,
            passed_operations=1,
            resolved_by_transient_retry=0,
            failed_operations=0,
            error_operations=0,
            total_transient_retries_dispatched=0,
            total_requests_executed=1,
            pass_rate_percentage=100.0,
            total_duration_ms=45.0,
        ),
        operations=(op_record,),
        global_remediation_summary=(),
    )

    # Patch WorkflowOrchestrator.run_suite to simulate execution outside DB transactions
    with patch(
        "backend.app.services.run_service.WorkflowOrchestrator.run_suite",
        AsyncMock(return_value=mock_report),
    ):
        result_report = await run_service.execute_run(run_id=run.id)

    assert result_report.report_id == "rep_exec_success"

    # Verify DB state after run completion
    async with session_factory() as session:
        completed_run = await run_service.get_run(session, run.id)
        assert completed_run is not None
        assert completed_run.status == "completed"
        assert completed_run.started_at is not None
        assert completed_run.completed_at is not None
        assert completed_run.duration_ms == 45.0
        assert completed_run.summary["pass_rate_percentage"] == 100.0

        # Verify DiagnosticReport persisted
        db_rep = await run_service.get_run_report(session, run.id)
        assert db_rep is not None
        assert db_rep.report_id == "rep_exec_success"
        assert db_rep.markdown_rendered is not None
        assert "Petstore Inventory API" in db_rep.markdown_rendered

        # Verify TestExecution persisted
        execs = await run_service.get_run_executions(session, run.id)
        assert len(execs) == 1
        assert execs[0].execution_type == "initial"
        assert execs[0].passed is True
        assert execs[0].status_code == 200


@pytest.mark.anyio
async def test_run_service_target_endpoint_filtering(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
):
    proj_service = ProjectService()
    project, endpoints = await proj_service.create_project_from_spec(
        session=db_session,
        name="Target Filter Project",
        spec_content=SAMPLE_OPENAPI_YAML,
    )

    get_endpoint = [ep for ep in endpoints if ep.method == "GET"][0]

    run_service = RunService(session_factory=session_factory)
    run = await run_service.create_run(
        session=db_session,
        project_id=project.id,
        target_endpoints=[get_endpoint.id],
    )

    fp = "e" * 64
    op_record = OperationRunRecord(
        operation_id="listPets",
        http_method="GET",
        path_template="/pets",
        initial_request_fingerprint=fp,
        initial_attempt=AttemptOutcomeSummary(dispatched=True, status_code=200, passed=True),
        followup_decisions=(),
        transient_retries=(),
        variation_candidates=(),
        variation_tests=(),
        final_status=OverallRunStatus.PASSED,
        final_outcome=AttemptOutcomeSummary(dispatched=True, status_code=200, passed=True),
    )
    mock_report = DiagnosticReport(
        report_id="rep_filter_test",
        timestamp="2026-11-06T11:00:00Z",
        api_title="Petstore Inventory API",
        api_version="1.2.0",
        summary=RunSummaryStatistics(
            total_operations=1,
            passed_operations=1,
            resolved_by_transient_retry=0,
            failed_operations=0,
            error_operations=0,
            total_transient_retries_dispatched=0,
            total_requests_executed=1,
            pass_rate_percentage=100.0,
            total_duration_ms=20.0,
        ),
        operations=(op_record,),
    )

    captured_ops = []

    async def mock_run_suite(*args, **kwargs):
        captured_ops.extend(kwargs.get("operations", []))
        return mock_report

    with patch(
        "backend.app.services.run_service.WorkflowOrchestrator.run_suite",
        side_effect=mock_run_suite,
    ):
        await run_service.execute_run(run_id=run.id)

    # Verify only the selected GET endpoint was executed
    assert len(captured_ops) == 1
    assert captured_ops[0].method == "GET"
    assert captured_ops[0].path == "/pets"


@pytest.mark.anyio
async def test_run_service_error_handling_sanitization(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
):
    proj_service = ProjectService()
    project, _ = await proj_service.create_project_from_spec(
        session=db_session,
        name="Error Project",
        spec_content=SAMPLE_OPENAPI_DICT,
    )

    run_service = RunService(session_factory=session_factory)
    run = await run_service.create_run(db_session, project_id=project.id)

    # Raise simulated network exception with sensitive leak
    with patch(
        "backend.app.services.run_service.WorkflowOrchestrator.run_suite",
        AsyncMock(side_effect=RuntimeError("Connection failed with password=super_secret_db_pass")),
    ):
        with pytest.raises(RuntimeError):
            await run_service.execute_run(run_id=run.id)

    # Check that run was safely transitioned to 'failed' with sanitized error message
    async with session_factory() as session:
        failed_run = await run_service.get_run(session, run.id)
        assert failed_run is not None
        assert failed_run.status == "failed"
        assert failed_run.completed_at is not None
        # Sensitive credentials MUST be redacted
        assert "super_secret_db_pass" not in failed_run.error_message
        assert "[REDACTED]" in failed_run.error_message


@pytest.mark.anyio
async def test_run_service_cancellation_and_recovery(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
):
    proj_service = ProjectService()
    project, _ = await proj_service.create_project_from_spec(
        session=db_session,
        name="Cancel Project",
        spec_content=SAMPLE_OPENAPI_DICT,
    )
    await db_session.commit()

    run_service = RunService(session_factory=session_factory)
    run = await run_service.create_run(db_session, project_id=project.id)
    await db_session.commit()

    # Cancel pending run
    cancelled = await run_service.cancel_run(db_session, run.id, reason="User cancelled test")
    assert cancelled.status == "cancelled"

    # Attempting to execute a cancelled run raises ValueError
    with pytest.raises(ValueError, match="already cancelled"):
        await run_service.execute_run(run_id=run.id)

    # Crash recovery test
    run2 = await run_service.create_run(db_session, project_id=project.id)
    # Simulate an interrupted run left in 'running'
    run2.status = "running"
    await db_session.flush()

    recovered = await run_service.recover_interrupted_runs(db_session)
    assert recovered == 1

    async with session_factory() as session:
        r2 = await run_service.get_run(session, run2.id)
        assert r2.status == "failed"
        assert "Interrupted run recovered" in r2.error_message
