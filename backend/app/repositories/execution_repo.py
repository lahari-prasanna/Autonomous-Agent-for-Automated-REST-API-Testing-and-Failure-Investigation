"""
TestExecution, TestCase, and FailureInvestigation Repository (Phase 5B).
Provides database operations for executed HTTP attempts, generated test cases,
failure investigation facts, and sanitized snapshot persistence.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.base import utc_now
from backend.app.models.execution import TestExecution
from backend.app.models.investigation import FailureInvestigationModel
from backend.app.models.testcase import TestCase
from backend.app.schemas.report import OperationRunRecord
from backend.app.schemas.sanitizer import ReportDataSanitizer

logger = logging.getLogger(__name__)


def sanitize_snapshot_dict(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Recursively redacts sensitive keys and values from request/response snapshot payloads."""
    if not data or not isinstance(data, dict):
        return None

    sanitized: Dict[str, Any] = {}
    for k, v in data.items():
        key_str = str(k)
        if ReportDataSanitizer.SENSITIVE_KEY_PATTERN.search(key_str):
            sanitized[key_str] = "[REDACTED]"
        elif key_str.lower() in ("url", "uri", "endpoint_url") and isinstance(v, str):
            sanitized[key_str] = ReportDataSanitizer.sanitize_url(v) or "[INVALID_URL_REDACTED]"
        elif isinstance(v, str):
            sanitized[key_str] = ReportDataSanitizer.sanitize_text(v)
        elif isinstance(v, dict):
            sanitized[key_str] = sanitize_snapshot_dict(v)
        elif isinstance(v, (list, tuple)):
            sanitized[key_str] = [
                sanitize_snapshot_dict(item)
                if isinstance(item, dict)
                else (ReportDataSanitizer.sanitize_text(item) if isinstance(item, str) else item)
                for item in v
            ]
        else:
            sanitized[key_str] = v

    return sanitized


async def create_test_case(
    session: AsyncSession,
    endpoint_id: str,
    request_spec: Dict[str, Any],
    test_type: str = "standard",
    expected_status: Optional[int] = None,
    assertions: Optional[Any] = None,
    run_id: Optional[str] = None,
) -> TestCase:
    """Creates and persists a TestCase specification."""
    sanitized_req = sanitize_snapshot_dict(request_spec) or request_spec
    tc = TestCase(
        endpoint_id=endpoint_id,
        run_id=run_id,
        test_type=test_type,
        request_spec=sanitized_req,
        expected_status=expected_status,
        assertions=assertions,
    )
    session.add(tc)
    await session.flush()
    await session.refresh(tc)
    return tc


async def get_test_case(session: AsyncSession, test_case_id: str) -> Optional[TestCase]:
    """Retrieves a TestCase by ID, or None if not found."""
    stmt = select(TestCase).where(TestCase.id == test_case_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_test_cases_by_endpoint(
    session: AsyncSession,
    endpoint_id: str,
) -> List[TestCase]:
    """Lists TestCases belonging to an Endpoint."""
    stmt = select(TestCase).where(TestCase.endpoint_id == endpoint_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_test_execution(
    session: AsyncSession,
    run_id: str,
    endpoint_id: str,
    attempt_number: int = 1,
    execution_type: str = "initial",
    dispatched: bool = True,
    status_code: Optional[int] = None,
    latency_ms: Optional[float] = None,
    passed: bool = False,
    request_snapshot: Optional[Any] = None,
    response_snapshot: Optional[Any] = None,
    assertion_results: Optional[Any] = None,
    failure_classification: Optional[Any] = None,
    test_case_id: Optional[str] = None,
    executed_at: Optional[datetime] = None,
) -> TestExecution:
    """Creates and persists a single TestExecution record with sanitized snapshots."""
    safe_req = (
        sanitize_snapshot_dict(request_snapshot)
        if isinstance(request_snapshot, dict)
        else request_snapshot
    )
    safe_resp = (
        sanitize_snapshot_dict(response_snapshot)
        if isinstance(response_snapshot, dict)
        else response_snapshot
    )

    execution = TestExecution(
        run_id=run_id,
        endpoint_id=endpoint_id,
        test_case_id=test_case_id,
        attempt_number=attempt_number,
        execution_type=execution_type,
        dispatched=dispatched,
        status_code=status_code,
        latency_ms=latency_ms,
        passed=passed,
        request_snapshot=safe_req,
        response_snapshot=safe_resp,
        assertion_results=assertion_results,
        failure_classification=failure_classification,
        executed_at=executed_at or utc_now(),
    )
    session.add(execution)
    await session.flush()
    await session.refresh(execution)
    return execution


async def get_execution(session: AsyncSession, execution_id: str) -> Optional[TestExecution]:
    """Retrieves a TestExecution by ID, or None if not found."""
    stmt = select(TestExecution).where(TestExecution.id == execution_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_executions_by_run(session: AsyncSession, run_id: str) -> List[TestExecution]:
    """Lists all TestExecution records for an AgentRun ordered by executed_at ascending."""
    stmt = (
        select(TestExecution)
        .where(TestExecution.run_id == run_id)
        .order_by(TestExecution.executed_at.asc(), TestExecution.attempt_number.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_investigation(
    session: AsyncSession,
    execution_id: str,
    run_id: str,
    failure_category: Optional[str] = None,
    hypotheses: Optional[Any] = None,
    retrieved_evidence: Optional[Any] = None,
    recommendations: Optional[Any] = None,
    investigation_degraded: bool = False,
) -> FailureInvestigationModel:
    """Creates and persists a FailureInvestigationModel record."""
    inv = FailureInvestigationModel(
        execution_id=execution_id,
        run_id=run_id,
        failure_category=failure_category,
        hypotheses=hypotheses,
        retrieved_evidence=retrieved_evidence,
        recommendations=recommendations,
        investigation_degraded=investigation_degraded,
    )
    session.add(inv)
    await session.flush()
    await session.refresh(inv)
    return inv


async def list_investigations_by_run(
    session: AsyncSession,
    run_id: str,
) -> List[FailureInvestigationModel]:
    """Lists FailureInvestigationModel records for an AgentRun."""
    stmt = select(FailureInvestigationModel).where(FailureInvestigationModel.run_id == run_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_investigation_by_execution(
    session: AsyncSession,
    execution_id: str,
) -> Optional[FailureInvestigationModel]:
    """Retrieves the FailureInvestigationModel for a specific TestExecution ID."""
    stmt = select(FailureInvestigationModel).where(
        FailureInvestigationModel.execution_id == execution_id
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def bulk_create_executions_from_report(
    session: AsyncSession,
    run_id: str,
    endpoint_id_map: Dict[Tuple[str, str], str],
    operations: Sequence[OperationRunRecord],
) -> List[TestExecution]:
    """
    Persists all initial attempts, transient retries, variation tests,
    and associated failure investigations from a DiagnosticReport.
    """
    created_executions: List[TestExecution] = []

    for op in operations:
        endpoint_id = endpoint_id_map.get((op.http_method.upper(), op.path_template))
        if not endpoint_id:
            logger.warning(
                "No Endpoint record found for operation %s %s in run %s",
                op.http_method,
                op.path_template,
                run_id,
            )
            continue

        # 1. Initial Attempt
        init_att = op.initial_attempt
        assertion_results = (
            [{"assertion": a, "passed": False} for a in init_att.failed_assertions]
            if init_att.failed_assertions
            else []
        )
        failure_class = None
        if init_att.failure_category or init_att.error_type:
            failure_class = {
                "category": init_att.failure_category.value if init_att.failure_category else None,
                "error_type": init_att.error_type,
            }

        req_snapshot = {
            "fingerprint": op.initial_request_fingerprint,
            "method": op.http_method,
            "path": op.path_template,
        }
        resp_snapshot = {
            "status_code": init_att.status_code,
            "latency_ms": init_att.latency_ms,
            "dispatched": init_att.dispatched,
        }

        initial_execution = await create_test_execution(
            session=session,
            run_id=run_id,
            endpoint_id=endpoint_id,
            attempt_number=1,
            execution_type="initial",
            dispatched=init_att.dispatched,
            status_code=init_att.status_code,
            latency_ms=init_att.latency_ms,
            passed=init_att.passed,
            request_snapshot=req_snapshot,
            response_snapshot=resp_snapshot,
            assertion_results=assertion_results,
            failure_classification=failure_class,
        )
        created_executions.append(initial_execution)

        # 2. Attach Investigation if initial attempt had failure investigation
        if op.investigation:
            inv_rec = op.investigation
            evidence_dump = (
                [ev.model_dump() for ev in inv_rec.retrieved_evidence]
                if inv_rec.retrieved_evidence
                else []
            )
            recs_dump = (
                [rem.model_dump() for rem in op.remediation_recommendations]
                if op.remediation_recommendations
                else []
            )
            await create_investigation(
                session=session,
                execution_id=initial_execution.id,
                run_id=run_id,
                failure_category=(
                    init_att.failure_category.value if init_att.failure_category else None
                ),
                hypotheses=list(inv_rec.inferences) if inv_rec.inferences else [],
                retrieved_evidence=evidence_dump,
                recommendations=recs_dump,
                investigation_degraded=inv_rec.investigation_degraded,
            )

        # 3. Transient Retries
        for tr in op.transient_retries:
            tr_out = tr.outcome
            tr_req = {
                "fingerprint": tr.canonical_request_fingerprint,
                "retry_of": tr.retry_of,
                "backoff_delay_seconds": tr.backoff_delay_seconds,
            }
            tr_resp = {
                "status_code": tr_out.status_code,
                "latency_ms": tr_out.latency_ms,
                "dispatched": tr_out.dispatched,
            }
            tr_exec = await create_test_execution(
                session=session,
                run_id=run_id,
                endpoint_id=endpoint_id,
                attempt_number=tr.attempt_number + 1,
                execution_type="transient_retry",
                dispatched=tr_out.dispatched,
                status_code=tr_out.status_code,
                latency_ms=tr_out.latency_ms,
                passed=tr_out.passed,
                request_snapshot=tr_req,
                response_snapshot=tr_resp,
                assertion_results=[
                    {"assertion": a, "passed": False} for a in tr_out.failed_assertions
                ],
                failure_classification=(
                    {"category": tr_out.failure_category.value, "error_type": tr_out.error_type}
                    if tr_out.failure_category or tr_out.error_type
                    else None
                ),
            )
            created_executions.append(tr_exec)

        # 4. Variation Tests
        for vt in op.variation_tests:
            vt_out = vt.outcome
            vt_req = {
                "fingerprint": vt.canonical_request_fingerprint,
                "candidate_id": vt.candidate_id,
                "perturbations": [p.model_dump() for p in vt.perturbations_applied],
            }
            vt_resp = {
                "status_code": vt_out.status_code,
                "latency_ms": vt_out.latency_ms,
                "dispatched": vt_out.dispatched,
            }
            vt_exec = await create_test_execution(
                session=session,
                run_id=run_id,
                endpoint_id=endpoint_id,
                attempt_number=vt.variation_number + 1,
                execution_type="variation_test",
                dispatched=vt_out.dispatched,
                status_code=vt_out.status_code,
                latency_ms=vt_out.latency_ms,
                passed=vt_out.passed,
                request_snapshot=vt_req,
                response_snapshot=vt_resp,
                assertion_results=[
                    {"assertion": a, "passed": False} for a in vt_out.failed_assertions
                ],
                failure_classification=(
                    {"category": vt_out.failure_category.value, "error_type": vt_out.error_type}
                    if vt_out.failure_category or vt_out.error_type
                    else None
                ),
            )
            created_executions.append(vt_exec)

    return created_executions
