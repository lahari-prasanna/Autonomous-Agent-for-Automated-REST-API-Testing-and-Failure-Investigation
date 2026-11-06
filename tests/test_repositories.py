"""
Unit and Integration Tests for Repository Layer (Phase 5B).
Verifies:
  1. Project repository CRUD, pagination, and attribute updates.
  2. Endpoint repository bulk persistence and operation lookup.
  3. Parent-child cascade deletion across Projects, Endpoints, Runs, and Reports.
  4. AgentRun lifecycle transitions, cancellation, and crash recovery.
  5. TestCase and TestExecution CRUD with sanitized snapshot verification.
  6. FailureInvestigationModel persistence and lookup.
  7. DiagnosticReportModel persistence and markdown rendering.
  8. bulk_create_executions_from_report mapping initial, retry, and variation attempts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import AsyncGenerator
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.core.database import build_engine, close_db, drop_db, init_db
from backend.app.models.base import utc_now
from backend.app.models.project import Project
from backend.app.repositories import (
    bulk_create_endpoints,
    bulk_create_executions_from_report,
    cancel_run,
    create_endpoint,
    create_investigation,
    create_project,
    create_report,
    create_run,
    create_test_case,
    create_test_execution,
    delete_project,
    find_endpoint_by_operation,
    get_endpoint,
    get_execution,
    get_investigation_by_execution,
    get_project,
    get_report_by_id,
    get_report_by_run,
    get_run,
    get_test_case,
    list_endpoints_by_project,
    list_executions_by_run,
    list_investigations_by_run,
    list_projects,
    list_runs_by_project,
    list_test_cases_by_endpoint,
    recover_interrupted_runs,
    sanitize_snapshot_dict,
    update_project,
    update_run_status,
)
from backend.app.schemas.failure import FailureCategory, FailureSeverity, RetryPolicy
from backend.app.schemas.report import (
    AttemptOutcomeSummary,
    DetachedInvestigationRecord,
    DetachedRetrievedEvidenceItem,
    DiagnosticReport,
    OperationRunRecord,
    OverallRunStatus,
    RemediationRecommendation,
    RunSummaryStatistics,
    TransientRetryRecord,
    VariationMetricsSummary,
    VariationTestRecord,
)
from backend.app.schemas.retest import ParameterPerturbation, PerturbationStrategy
from backend.app.schemas.spec import EndpointDefinition, ParameterDefinition


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
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provides an isolated AsyncSession bound to the in-memory test database."""
    session_factory = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Project & Endpoint Repository Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_project_repo_crud_and_pagination(db_session: AsyncSession):
    # Create projects
    p1 = await create_project(
        session=db_session,
        name="API Project 1",
        description="First test project",
        base_url="https://api.example.com",
        spec_title="Spec 1",
        spec_version="1.0.0",
    )
    p2 = await create_project(
        session=db_session,
        name="API Project 2",
        description="Second test project",
        base_url="https://api2.example.com",
    )

    # Get project
    fetched = await get_project(db_session, p1.id)
    assert fetched is not None
    assert fetched.id == p1.id
    assert fetched.name == "API Project 1"
    assert fetched.spec_title == "Spec 1"

    # List projects with pagination
    all_projects = await list_projects(db_session, skip=0, limit=10)
    assert len(all_projects) == 2

    first_page = await list_projects(db_session, skip=0, limit=1)
    assert len(first_page) == 1

    # Update project
    updated = await update_project(
        db_session,
        p1.id,
        name="Renamed Project 1",
        description="Updated description",
        base_url="https://updated.example.com",
    )
    assert updated is not None
    assert updated.name == "Renamed Project 1"
    assert updated.description == "Updated description"
    assert updated.base_url == "https://updated.example.com"

    # Delete project
    deleted = await delete_project(db_session, p2.id)
    assert deleted is True
    assert await get_project(db_session, p2.id) is None


@pytest.mark.anyio
async def test_endpoint_repo_bulk_creation_and_lookup(db_session: AsyncSession):
    project = await create_project(db_session, name="Test Project")

    # Single endpoint creation
    ep1 = await create_endpoint(
        session=db_session,
        project_id=project.id,
        method="GET",
        path="/items",
        operation_id="listItems",
        summary="List all items",
    )
    assert ep1.id is not None
    assert ep1.method == "GET"
    assert ep1.path == "/items"

    # Bulk creation using EndpointDefinition Pydantic models
    def1 = EndpointDefinition(
        path="/items/{id}",
        method="GET",
        operation_id="getItem",
        summary="Get item by ID",
        parameters=[
            ParameterDefinition(name="id", in_="path", required=True, schema={"type": "integer"})
        ],
    )
    def2 = EndpointDefinition(
        path="/items",
        method="POST",
        operation_id="createItem",
        summary="Create an item",
    )

    bulk_eps = await bulk_create_endpoints(
        session=db_session,
        project_id=project.id,
        endpoints=[def1, def2],
    )
    assert len(bulk_eps) == 2

    # Verify lookup by operation
    found_post = await find_endpoint_by_operation(db_session, project.id, "POST", "/items")
    assert found_post is not None
    assert found_post.operation_id == "createItem"

    # Verify list by project
    all_eps = await list_endpoints_by_project(db_session, project.id)
    assert len(all_eps) == 3


@pytest.mark.anyio
async def test_project_cascade_delete(db_session: AsyncSession):
    project = await create_project(db_session, name="Cascade Project")
    ep = await create_endpoint(db_session, project_id=project.id, method="GET", path="/test")
    run = await create_run(db_session, project_id=project.id)

    # Delete project
    await delete_project(db_session, project.id)

    # Verify children were deleted
    assert await get_endpoint(db_session, ep.id) is None
    assert await get_run(db_session, run.id) is None


# ---------------------------------------------------------------------------
# AgentRun Repository Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_run_repo_lifecycle_and_cancellation(db_session: AsyncSession):
    project = await create_project(db_session, name="Run Test Project")
    run = await create_run(db_session, project_id=project.id, concurrency=3)

    assert run.status == "pending"
    assert run.concurrency == 3

    # Update status to running
    now = utc_now()
    updated = await update_run_status(
        session=db_session,
        run_id=run.id,
        status="running",
        started_at=now,
    )
    assert updated is not None
    assert updated.status == "running"
    assert updated.started_at == now

    # Cancel run
    cancelled = await cancel_run(db_session, run.id, reason="User cancelled test")
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.error_message == "User cancelled test"
    assert cancelled.completed_at is not None


@pytest.mark.anyio
async def test_run_repo_recover_interrupted_runs(db_session: AsyncSession):
    project = await create_project(db_session, name="Recovery Project")
    r1 = await create_run(db_session, project_id=project.id, status="running")
    r2 = await create_run(db_session, project_id=project.id, status="running")
    r3 = await create_run(db_session, project_id=project.id, status="completed")

    # Recover interrupted runs
    recovered_count = await recover_interrupted_runs(
        db_session, error_message="Crash recovery triggered"
    )
    assert recovered_count == 2

    # Verify status changed to failed
    r1_db = await get_run(db_session, r1.id)
    r2_db = await get_run(db_session, r2.id)
    r3_db = await get_run(db_session, r3.id)

    assert r1_db.status == "failed"
    assert r1_db.error_message == "Crash recovery triggered"
    assert r2_db.status == "failed"
    assert r3_db.status == "completed"


# ---------------------------------------------------------------------------
# Snapshot Sanitization Tests
# ---------------------------------------------------------------------------

def test_sanitize_snapshot_dict():
    raw_payload = {
        "url": "https://user:pass@api.example.com/login?token=secret123&name=Alice",
        "headers": {
            "Authorization": "Bearer secret_jwt_token_12345",
            "Cookie": "session_id=abcdef123456",
            "Content-Type": "application/json",
        },
        "password": "super_secret_pw",
        "api_key": "key-xyz-789",
        "nested": {
            "token": "nested_secret",
            "safe_field": "visible_data",
        },
    }

    sanitized = sanitize_snapshot_dict(raw_payload)
    assert sanitized is not None
    # Authorization header is masked
    assert sanitized["headers"]["Authorization"] == "[REDACTED]"
    assert sanitized["headers"]["Cookie"] == "[REDACTED]"
    assert sanitized["headers"]["Content-Type"] == "application/json"
    # Secrets directly masked
    assert sanitized["password"] == "[REDACTED]"
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["nested"]["token"] == "[REDACTED]"
    assert sanitized["nested"]["safe_field"] == "visible_data"
    # URL userinfo and sensitive query params masked
    assert "pass" not in sanitized["url"]
    assert "token=%5BREDACTED%5D" in sanitized["url"] or "token=[REDACTED]" in sanitized["url"]


# ---------------------------------------------------------------------------
# TestExecution and Investigation Repository Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_execution_repo_crud(db_session: AsyncSession):
    project = await create_project(db_session, name="Exec Project")
    endpoint = await create_endpoint(db_session, project_id=project.id, method="GET", path="/status")
    run = await create_run(db_session, project_id=project.id)

    # Test Case
    tc = await create_test_case(
        session=db_session,
        endpoint_id=endpoint.id,
        run_id=run.id,
        request_spec={"method": "GET", "url": "https://api.example.com/status"},
        expected_status=200,
    )
    assert tc.id is not None
    assert tc.expected_status == 200

    # Test Execution
    exec_record = await create_test_execution(
        session=db_session,
        run_id=run.id,
        endpoint_id=endpoint.id,
        test_case_id=tc.id,
        attempt_number=1,
        execution_type="initial",
        status_code=200,
        latency_ms=45.2,
        passed=True,
    )
    assert exec_record.id is not None
    assert exec_record.passed is True
    assert exec_record.status_code == 200

    # Investigation
    inv = await create_investigation(
        session=db_session,
        execution_id=exec_record.id,
        run_id=run.id,
        failure_category="server_error",
        hypotheses=["DB connection timeout"],
    )
    assert inv.id is not None
    assert inv.failure_category == "server_error"

    fetched_inv = await get_investigation_by_execution(db_session, exec_record.id)
    assert fetched_inv is not None
    assert fetched_inv.id == inv.id


@pytest.mark.anyio
async def test_bulk_create_executions_from_report(db_session: AsyncSession):
    project = await create_project(db_session, name="Bulk Exec Project")
    endpoint = await create_endpoint(
        db_session, project_id=project.id, method="GET", path="/users/{id}"
    )
    run = await create_run(db_session, project_id=project.id)

    endpoint_id_map = {("GET", "/users/{id}"): endpoint.id}

    # Construct mock OperationRunRecord with initial attempt, retry, and variation
    fingerprint = "a" * 64
    op_record = OperationRunRecord(
        operation_id="getUser",
        http_method="GET",
        path_template="/users/{id}",
        initial_request_fingerprint=fingerprint,
        initial_attempt=AttemptOutcomeSummary(
            dispatched=True,
            status_code=500,
            latency_ms=120.0,
            passed=False,
            failure_category=FailureCategory.SERVER_ERROR,
            failed_assertions=("Status code is 200",),
            error_type="InternalServerError",
        ),
        followup_decisions=(),
        transient_retries=(
            TransientRetryRecord(
                attempt_number=1,
                retry_of="initial",
                backoff_delay_seconds=1.0,
                canonical_request_fingerprint=fingerprint,
                outcome=AttemptOutcomeSummary(
                    dispatched=True,
                    status_code=200,
                    latency_ms=90.0,
                    passed=True,
                ),
            ),
        ),
        variation_candidates=(),
        variation_tests=(
            VariationTestRecord(
                variation_number=1,
                candidate_id="cand_1",
                canonical_request_fingerprint=fingerprint,
                perturbations_applied=(
                    ParameterPerturbation(
                        parameter_name="id",
                        parameter_location="path",
                        original_value=10,
                        varied_value=1,
                        constraint_applied="minimum",
                        pre_execution_validated=True,
                    ),
                ),
                outcome=AttemptOutcomeSummary(
                    dispatched=True,
                    status_code=200,
                    latency_ms=80.0,
                    passed=True,
                ),
            ),
        ),
        final_status=OverallRunStatus.RESOLVED_AFTER_TRANSIENT_RETRY,
        final_outcome=AttemptOutcomeSummary(
            dispatched=True,
            status_code=200,
            latency_ms=90.0,
            passed=True,
        ),
        investigation=DetachedInvestigationRecord(
            investigation_degraded=False,
            inferences=("Transient DB lock resolved",),
            source_references=(),
            retrieved_evidence=(),
        ),
        remediation_recommendations=(
            RemediationRecommendation(
                priority=FailureSeverity.MEDIUM,
                target="server_implementation",
                finding="Transient 500 error",
                action="Increase DB pool timeout",
            ),
        ),
    )

    created_execs = await bulk_create_executions_from_report(
        session=db_session,
        run_id=run.id,
        endpoint_id_map=endpoint_id_map,
        operations=[op_record],
    )

    # 1 initial + 1 transient retry + 1 variation test = 3 executions
    assert len(created_execs) == 3

    # Verify executions queryable in DB
    db_execs = await list_executions_by_run(db_session, run.id)
    assert len(db_execs) == 3

    types = [e.execution_type for e in db_execs]
    assert "initial" in types
    assert "transient_retry" in types
    assert "variation_test" in types

    # Verify investigation persisted for initial failed attempt
    invs = await list_investigations_by_run(db_session, run.id)
    assert len(invs) == 1
    assert invs[0].failure_category == "server_error"
    assert "Transient DB lock resolved" in invs[0].hypotheses


# ---------------------------------------------------------------------------
# DiagnosticReport Repository Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_report_repo_crud(db_session: AsyncSession):
    project = await create_project(db_session, name="Report Project")
    run = await create_run(db_session, project_id=project.id)

    # Build a minimal valid DiagnosticReport
    report = DiagnosticReport(
        report_id="rep_test_001",
        timestamp="2026-11-06T10:00:00Z",
        api_title="Test API",
        api_version="1.0.0",
        base_url="https://api.example.com",
        summary=RunSummaryStatistics(
            total_operations=0,
            passed_operations=0,
            resolved_by_transient_retry=0,
            failed_operations=0,
            error_operations=0,
            total_transient_retries_dispatched=0,
            total_requests_executed=0,
            pass_rate_percentage=0.0,
            total_duration_ms=150.0,
        ),
        operations=(),
        global_remediation_summary=("All checks satisfied",),
    )

    saved_report = await create_report(
        session=db_session,
        run_id=run.id,
        report=report,
    )
    assert saved_report.id is not None
    assert saved_report.run_id == run.id
    assert saved_report.api_title == "Test API"
    assert saved_report.markdown_rendered is not None
    assert "Test API" in saved_report.markdown_rendered

    # Lookup by run_id
    by_run = await get_report_by_run(db_session, run.id)
    assert by_run is not None
    assert by_run.id == saved_report.id

    # Lookup by report_id
    by_rep_id = await get_report_by_id(db_session, "rep_test_001")
    assert by_rep_id is not None
    assert by_rep_id.id == saved_report.id
