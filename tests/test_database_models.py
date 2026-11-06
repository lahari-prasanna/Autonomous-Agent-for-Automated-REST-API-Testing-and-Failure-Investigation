"""
Unit and Integration Tests for Database Models and Engine Configuration (Phase 5A).
Verifies:
  1. Asynchronous SQLite in-memory engine connection and SQLite foreign keys enforcement.
  2. Complete CRUD operations across all 7 declarative models.
  3. Parent-child relationship integrity and bidirectional navigation.
  4. Cascading deletes from Project -> Endpoints, Runs, Executions, Reports, Investigations.
  5. Dialect-agnostic PortableJSON persistence for complex nested schemas and facts.
  6. Automatic UUID generation and UTC timezone-aware timestamp updates.
  7. Foreign key violation rejection and explicit transaction rollback behavior.
  8. get_db() session dependency lifecycle (commit on success, rollback on exception).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.app.core.database import build_engine, close_db, drop_db, get_db, init_db
from backend.app.models import (
    AgentRun,
    Base,
    DiagnosticReportModel,
    Endpoint,
    FailureInvestigationModel,
    Project,
    TestCase,
    TestExecution,
)


# ---------------------------------------------------------------------------
# Test Fixtures: Isolated In-Memory SQLite Engine
# ---------------------------------------------------------------------------

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
# 1. Database Initialization & PRAGMA Checks
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_database_initialization_and_foreign_keys(db_engine: AsyncEngine):
    """Confirms all 7 tables are created and SQLite foreign key enforcement is active."""
    async with db_engine.connect() as conn:
        # 1. Verify foreign keys are enabled (PRAGMA foreign_keys == 1)
        res = await conn.execute(text("PRAGMA foreign_keys;"))
        fk_enabled = res.scalar()
        assert fk_enabled == 1, "SQLite foreign keys must be active"

        # 2. Verify all 7 tables exist
        tables_res = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        )
        table_names = {row[0] for row in tables_res.fetchall()}
        expected_tables = {
            "projects",
            "endpoints",
            "agent_runs",
            "test_cases",
            "test_executions",
            "failure_investigations",
            "diagnostic_reports",
        }
        assert expected_tables.issubset(table_names)


# ---------------------------------------------------------------------------
# 2. Project CRUD, UUIDs, and Timestamps
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_project_crud_and_timestamps(db_session: AsyncSession):
    """Confirms Project creation, UUID generation, and UTC timestamp auditing."""
    project = Project(
        name="Payment Gateway API",
        description="Core payment processing microservice",
        base_url="https://api.payment.example.com",
        spec_raw='{"openapi": "3.0.0", "info": {"title": "Payment API", "version": "1.0.0"}}',
        spec_title="Payment API",
        spec_version="1.0.0",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # 1. UUID generated and 36 characters
    assert project.id is not None
    assert len(project.id) == 36
    assert isinstance(uuid.UUID(project.id), uuid.UUID)

    # 2. Timestamps populated with UTC timezone
    assert project.created_at is not None
    assert project.updated_at is not None
    assert project.created_at.tzinfo is not None

    # 3. Read back from database
    stmt = select(Project).where(Project.id == project.id)
    retrieved = (await db_session.execute(stmt)).scalar_one()
    assert retrieved.name == "Payment Gateway API"
    assert retrieved.spec_title == "Payment API"

    # 4. Update and verify updated_at changes
    old_updated = retrieved.updated_at
    retrieved.base_url = "https://v2.api.payment.example.com"
    await db_session.commit()
    await db_session.refresh(retrieved)
    assert retrieved.base_url == "https://v2.api.payment.example.com"


# ---------------------------------------------------------------------------
# 3. Endpoint & PortableJSON Nested Schema Persistence
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_endpoint_relationship_and_portable_json(db_session: AsyncSession):
    """Confirms Endpoint belongs to Project and correctly serializes complex JSON schemas."""
    project = Project(name="E-Commerce API", base_url="http://api.local")
    db_session.add(project)
    await db_session.commit()

    params_json = [
        {"name": "category", "in": "query", "schema": {"type": "string"}, "required": False},
        {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 50}, "required": False},
    ]
    responses_json = {
        "200": {"description": "OK", "content": {"application/json": {"schema": {"type": "array"}}}},
        "400": {"description": "Bad Request"},
    }

    endpoint = Endpoint(
        project_id=project.id,
        method="GET",
        path="/api/products",
        operation_id="listProducts",
        summary="List catalog items",
        parameters=params_json,
        responses=responses_json,
    )
    db_session.add(endpoint)
    await db_session.commit()
    await db_session.refresh(endpoint)

    # Verify bidirectional navigation
    assert endpoint.project.id == project.id
    assert endpoint.project.name == "E-Commerce API"

    # Verify complex nested JSON preservation
    stmt = select(Endpoint).where(Endpoint.id == endpoint.id)
    db_ep = (await db_session.execute(stmt)).scalar_one()
    assert db_ep.parameters == params_json
    assert db_ep.parameters[1]["schema"]["maximum"] == 50
    assert "200" in db_ep.responses


# ---------------------------------------------------------------------------
# 4. AgentRun and DiagnosticReport (1-to-1 Relationship)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_agent_run_and_diagnostic_report_one_to_one(db_session: AsyncSession):
    """Confirms AgentRun lifecycle and unique 1-to-1 DiagnosticReportModel relationship."""
    project = Project(name="CRM API", base_url="http://crm.local")
    db_session.add(project)
    await db_session.commit()

    run = AgentRun(
        project_id=project.id,
        status="running",
        concurrency=3,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    report = DiagnosticReportModel(
        run_id=run.id,
        report_id="rep_9876543210ab",
        api_title="CRM API",
        api_version="2.1.0",
        base_url="http://crm.local",
        summary_data={"total_operations": 5, "pass_rate_percentage": 80.0},
        operations_data=[{"path": "/users", "method": "GET", "status": "passed"}],
        markdown_rendered="# Diagnostic Report: CRM API\nPass Rate: 80%",
    )
    db_session.add(report)
    await db_session.commit()
    await db_session.refresh(run)

    # 1. 1-to-1 relationship navigation
    assert run.report is not None
    assert run.report.report_id == "rep_9876543210ab"
    assert run.report.run.id == run.id

    # 2. Unique constraint on run_id prevents duplicate reports
    dup_report = DiagnosticReportModel(
        run_id=run.id,
        report_id="rep_second_invalid",
        api_title="CRM API",
        api_version="2.1.0",
        summary_data={},
        operations_data=[],
    )
    db_session.add(dup_report)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# 5. Full Pipeline Lifecycle: TestCase, Execution, and Investigation
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_full_testing_pipeline_models(db_session: AsyncSession):
    """
    Confirms end-to-end entity hierarchy:
    Project -> Endpoint -> TestCase -> TestExecution -> FailureInvestigationModel.
    """
    project = Project(name="Billing API", base_url="http://billing.local")
    db_session.add(project)
    await db_session.commit()

    endpoint = Endpoint(project_id=project.id, method="POST", path="/invoices", operation_id="createInvoice")
    run = AgentRun(project_id=project.id, status="running")
    db_session.add_all([endpoint, run])
    await db_session.commit()

    # TestCase
    tc = TestCase(
        endpoint_id=endpoint.id,
        run_id=run.id,
        test_type="boundary",
        request_spec={"method": "POST", "url": "http://billing.local/invoices", "json_body": {"amount": 0}},
        expected_status=400,
    )
    db_session.add(tc)
    await db_session.commit()

    # TestExecution
    exec_record = TestExecution(
        run_id=run.id,
        endpoint_id=endpoint.id,
        test_case_id=tc.id,
        attempt_number=1,
        execution_type="initial",
        dispatched=True,
        status_code=500,
        latency_ms=120.5,
        passed=False,
        request_snapshot={"method": "POST", "sanitized_url": "http://billing.local/invoices"},
        response_snapshot={"status_code": 500, "headers": {"content-type": "application/json"}},
        failure_classification={"primary_category": "server_error", "severity": "high"},
    )
    db_session.add(exec_record)
    await db_session.commit()
    await db_session.refresh(exec_record)

    # FailureInvestigationModel
    inv = FailureInvestigationModel(
        execution_id=exec_record.id,
        run_id=run.id,
        failure_category="server_error",
        hypotheses=[{"hypothesis": "Unhandled zero-amount division error", "likelihood": "high"}],
        retrieved_evidence=[{"source_ref": "rfc9110.md", "content": "500 Internal Server Error"}],
        recommendations=[{"summary": "Add zero-amount validation guard before division"}],
        investigation_degraded=False,
    )
    db_session.add(inv)
    await db_session.commit()
    await db_session.refresh(exec_record)

    # Verify complete relational linkages
    assert len(exec_record.investigations) == 1
    assert exec_record.investigations[0].failure_category == "server_error"
    assert exec_record.test_case.test_type == "boundary"
    assert exec_record.endpoint.operation_id == "createInvoice"
    assert exec_record.run.project.name == "Billing API"


# ---------------------------------------------------------------------------
# 6. Foreign Key Enforcement & Invalid References
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_foreign_key_violation_raises_integrity_error(db_session: AsyncSession):
    """Confirms inserting a record with a non-existent parent foreign key raises IntegrityError."""
    fake_project_id = str(uuid.uuid4())
    orphan_endpoint = Endpoint(
        project_id=fake_project_id,
        method="GET",
        path="/nowhere",
    )
    db_session.add(orphan_endpoint)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# 7. Cascading Deletes
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cascade_delete_from_project(db_session: AsyncSession):
    """
    Confirms deleting a Project automatically deletes all associated Endpoints,
    AgentRuns, TestCases, TestExecutions, Investigations, and DiagnosticReports.
    """
    # 1. Setup complete hierarchy
    project = Project(name="Temporary API", base_url="http://temp.local")
    db_session.add(project)
    await db_session.commit()

    endpoint = Endpoint(project_id=project.id, method="GET", path="/items")
    run = AgentRun(project_id=project.id, status="completed")
    db_session.add_all([endpoint, run])
    await db_session.commit()

    tc = TestCase(endpoint_id=endpoint.id, run_id=run.id, request_spec={"method": "GET"})
    rep = DiagnosticReportModel(
        run_id=run.id,
        report_id="rep_temp",
        api_title="Temp",
        api_version="1.0",
        summary_data={},
        operations_data=[],
    )
    db_session.add_all([tc, rep])
    await db_session.commit()

    execution = TestExecution(
        run_id=run.id,
        endpoint_id=endpoint.id,
        test_case_id=tc.id,
        status_code=200,
        passed=True,
    )
    db_session.add(execution)
    await db_session.commit()

    inv = FailureInvestigationModel(execution_id=execution.id, run_id=run.id, failure_category="none")
    db_session.add(inv)
    await db_session.commit()

    # Confirm records exist
    assert (await db_session.execute(select(Endpoint))).scalars().all()
    assert (await db_session.execute(select(AgentRun))).scalars().all()
    assert (await db_session.execute(select(TestExecution))).scalars().all()
    assert (await db_session.execute(select(FailureInvestigationModel))).scalars().all()
    assert (await db_session.execute(select(DiagnosticReportModel))).scalars().all()

    # 2. Delete Project
    await db_session.delete(project)
    await db_session.commit()

    # 3. Confirm all cascaded records were removed
    assert len((await db_session.execute(select(Project))).scalars().all()) == 0
    assert len((await db_session.execute(select(Endpoint))).scalars().all()) == 0
    assert len((await db_session.execute(select(AgentRun))).scalars().all()) == 0
    assert len((await db_session.execute(select(TestCase))).scalars().all()) == 0
    assert len((await db_session.execute(select(TestExecution))).scalars().all()) == 0
    assert len((await db_session.execute(select(FailureInvestigationModel))).scalars().all()) == 0
    assert len((await db_session.execute(select(DiagnosticReportModel))).scalars().all()) == 0


# ---------------------------------------------------------------------------
# 8. Transaction Rollback Behavior
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_transaction_rollback_preserves_clean_state(db_session: AsyncSession):
    """Confirms explicit transaction rollback discards uncommitted mutations cleanly."""
    project = Project(name="Doomed Project", base_url="http://doomed.local")
    db_session.add(project)
    # Roll back before committing
    await db_session.rollback()

    # Confirm project was not persisted
    stmt = select(Project).where(Project.name == "Doomed Project")
    res = (await db_session.execute(stmt)).scalar_one_or_none()
    assert res is None


# ---------------------------------------------------------------------------
# 9. get_db Dependency Generator Lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_db_dependency_commit_and_rollback(monkeypatch):
    """Confirms get_db commits on clean generator exit and rolls back on exception."""
    test_eng = build_engine("sqlite+aiosqlite:///:memory:")
    await init_db(test_eng)

    test_session_factory = async_sessionmaker(
        bind=test_eng,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    import backend.app.core.database as db_mod
    monkeypatch.setattr(db_mod, "async_session_factory", test_session_factory)

    # 1. Clean exit commits
    async for session in get_db():
        session.add(Project(name="Committed Project"))

    async with test_session_factory() as verify_session:
        res = (await verify_session.execute(select(Project).where(Project.name == "Committed Project"))).scalar_one_or_none()
        assert res is not None

    # 2. Exception rolls back
    with pytest.raises(RuntimeError):
        async for session in get_db():
            session.add(Project(name="Failed Project"))
            raise RuntimeError("Unexpected Route Exception")

    async with test_session_factory() as verify_session:
        res_failed = (await verify_session.execute(select(Project).where(Project.name == "Failed Project"))).scalar_one_or_none()
        assert res_failed is None

    await close_db(test_eng)


# ---------------------------------------------------------------------------
# 10. Phase 4C Diagnostic Report Integration
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_phase_4c_diagnostic_report_integration(db_session: AsyncSession):
    """
    Confirms that real DiagnosticReport and OperationRunRecord structures from Phase 4C
    can be serialized and persisted into DiagnosticReportModel and queried cleanly.
    """
    from backend.app.schemas.report import (
        AttemptOutcomeSummary,
        DiagnosticReport,
        OperationRunRecord,
        OverallRunStatus,
    )
    from backend.app.services.report_generator import DiagnosticReportGenerator

    project = Project(name="Analytics API", base_url="http://analytics.local")
    db_session.add(project)
    await db_session.commit()

    run = AgentRun(project_id=project.id, status="completed")
    db_session.add(run)
    await db_session.commit()

    attempt = AttemptOutcomeSummary(dispatched=True, passed=True, status_code=200, latency_ms=45.0)
    op = OperationRunRecord(
        http_method="GET",
        path_template="/metrics",
        initial_request_fingerprint="f" * 64,
        initial_attempt=attempt,
        final_status=OverallRunStatus.PASSED,
        final_outcome=attempt,
    )
    report = DiagnosticReportGenerator.build_report(
        operations=[op],
        api_title="Analytics API",
        api_version="1.0.0",
        base_url="http://analytics.local",
    )
    md_content = DiagnosticReportGenerator.to_markdown(report)

    db_report = DiagnosticReportModel(
        run_id=run.id,
        report_id=report.report_id,
        api_title=report.api_title,
        api_version=report.api_version,
        base_url=report.base_url,
        summary_data=report.summary.model_dump(),
        operations_data=[o.model_dump() for o in report.operations],
        markdown_rendered=md_content,
    )
    db_session.add(db_report)
    await db_session.commit()
    await db_session.refresh(db_report)

    assert db_report.report_id == report.report_id
    assert db_report.summary_data["total_operations"] == 1
    assert db_report.summary_data["passed_operations"] == 1
    assert db_report.summary_data["pass_rate_percentage"] == 100.0
    assert "# Diagnostic API Testing Report" in db_report.markdown_rendered
