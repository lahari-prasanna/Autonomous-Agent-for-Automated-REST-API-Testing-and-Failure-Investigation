"""
Run Application Service (Phase 5B & 5D).
Orchestrates test run lifecycle, executes WorkflowOrchestrator strictly outside
database transactions, persists test executions, investigations, and diagnostic reports,
handles cancellation and crash recovery safely, and publishes real-time SSE progress events.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.agent.orchestrator import WorkflowOrchestrator
from backend.app.core.database import async_session_factory
from backend.app.core.events import EventManager, event_manager
from backend.app.models.base import utc_now
from backend.app.models.execution import TestExecution
from backend.app.models.investigation import FailureInvestigationModel
from backend.app.models.report import DiagnosticReportModel
from backend.app.models.run import AgentRun
from backend.app.repositories import execution_repo, project_repo, report_repo, run_repo
from backend.app.schemas.report import DiagnosticReport, OperationRunRecord
from backend.app.schemas.sanitizer import ReportDataSanitizer
from backend.app.schemas.spec import EndpointDefinition, ParsedAPISpec
from backend.app.services.report_generator import DiagnosticReportGenerator
from backend.app.services.spec_parser import OpenAPIParser

logger = logging.getLogger(__name__)


async def emit_operation_events(
    event_mgr: EventManager,
    run_id: str,
    record: OperationRunRecord,
) -> None:
    """Publishes fine-grained attempt, investigation, and variation progress events."""
    op_key = record.operation_id or f"{record.http_method} {record.path_template}"

    # 1. Initial Attempt
    init_att = record.initial_attempt
    await event_mgr.publish(
        run_id=run_id,
        event_type="attempt_completed",
        data={
            "operation_id": op_key,
            "attempt_number": 1,
            "type": "initial",
            "status_code": init_att.status_code,
            "latency_ms": init_att.latency_ms,
            "passed": init_att.passed,
            "dispatched": init_att.dispatched,
            "error_type": init_att.error_type,
        },
    )

    # 2. Failure Investigation
    if record.investigation:
        inv = record.investigation
        await event_mgr.publish(
            run_id=run_id,
            event_type="investigation_completed",
            data={
                "operation_id": op_key,
                "failure_category": (
                    init_att.failure_category.value if init_att.failure_category else None
                ),
                "inferences_count": len(inv.inferences) if inv.inferences else 0,
                "investigation_degraded": inv.investigation_degraded,
            },
        )

    # 3. Retests
    for tr in record.transient_retries:
        await event_mgr.publish(
            run_id=run_id,
            event_type="retest_completed",
            data={
                "operation_id": op_key,
                "type": "transient_retry",
                "attempt_number": tr.attempt_number + 1,
                "retry_of": tr.retry_of,
                "status_code": tr.outcome.status_code,
                "passed": tr.outcome.passed,
            },
        )

    # 4. Variations
    for vt in record.variation_tests:
        await event_mgr.publish(
            run_id=run_id,
            event_type="variation_completed",
            data={
                "operation_id": op_key,
                "type": "variation_test",
                "variation_number": vt.variation_number,
                "candidate_id": vt.candidate_id,
                "status_code": vt.outcome.status_code,
                "passed": vt.outcome.passed,
            },
        )

    # 5. Operation Completed
    await event_mgr.publish(
        run_id=run_id,
        event_type="operation_completed",
        data={
            "operation_id": op_key,
            "method": record.http_method,
            "path": record.path_template,
            "final_status": record.final_status.value,
            "passed": record.final_outcome.passed,
        },
    )


class EventEmittingOrchestrator(WorkflowOrchestrator):
    """
    Subclass of WorkflowOrchestrator that publishes live SSE events
    for each bounded operation without modifying Phase 4C core orchestrator code.
    """

    def __init__(
        self,
        event_mgr: EventManager,
        run_id: str,
        published_ops_tracker: Set[Tuple[str, str]],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.event_mgr = event_mgr
        self.run_id = run_id
        self.published_ops_tracker = published_ops_tracker

    async def run_operation(
        self,
        spec: ParsedAPISpec,
        operation: EndpointDefinition,
        raw_spec: Optional[Dict[str, Any]] = None,
        custom_base_url: Optional[str] = None,
    ) -> OperationRunRecord:
        op_key = operation.operation_id or f"{operation.method.upper()} {operation.path}"
        await self.event_mgr.publish(
            run_id=self.run_id,
            event_type="operation_started",
            data={
                "operation_id": op_key,
                "method": operation.method.upper(),
                "path": operation.path,
            },
        )
        record = await super().run_operation(
            spec=spec,
            operation=operation,
            raw_spec=raw_spec,
            custom_base_url=custom_base_url,
        )
        await emit_operation_events(self.event_mgr, self.run_id, record)
        self.published_ops_tracker.add((record.http_method, record.path_template))
        return record


class RunService:
    """
    Application service managing testing execution runs.
    Ensures network and agent graph executions are decoupled from database transactions
    and dispatches live progress events to the event broker.
    """

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        parser: Optional[OpenAPIParser] = None,
        event_mgr: Optional[EventManager] = None,
    ) -> None:
        self.session_factory = session_factory or async_session_factory
        self.parser = parser or OpenAPIParser()
        self.event_manager = event_mgr or event_manager
        # Cooperative cancellation tokens mapped by run_id
        self._cancellation_events: Dict[str, asyncio.Event] = {}

    async def create_run(
        self,
        session: AsyncSession,
        project_id: str,
        concurrency: int = 1,
        target_endpoints: Optional[List[str]] = None,
    ) -> AgentRun:
        """
        Validates Project existence and creates a new pending AgentRun record.
        Publishes a 'run_created' event.
        """
        project = await project_repo.get_project(session, project_id)
        if not project:
            raise ValueError(f"Project with ID '{project_id}' not found.")

        clamped_concurrency = max(1, min(concurrency, 5))
        run = await run_repo.create_run(
            session=session,
            project_id=project_id,
            concurrency=clamped_concurrency,
            target_endpoints=target_endpoints,
            status="pending",
        )

        await self.event_manager.publish(
            run_id=run.id,
            event_type="run_created",
            data={
                "run_id": run.id,
                "project_id": project_id,
                "status": "pending",
                "concurrency": clamped_concurrency,
                "target_endpoints": target_endpoints,
            },
        )
        return run

    async def cancel_run(
        self,
        session: AsyncSession,
        run_id: str,
        reason: Optional[str] = "Run cancelled by user",
    ) -> Optional[AgentRun]:
        """
        Signals cancellation to any running background task and marks the run cancelled in DB.
        Publishes a 'run_cancelled' event.
        """
        event = self._cancellation_events.get(run_id)
        if event is not None:
            event.set()

        run = await run_repo.cancel_run(session, run_id, reason=reason)
        if run:
            await self.event_manager.publish(
                run_id=run_id,
                event_type="run_cancelled",
                data={
                    "run_id": run_id,
                    "status": "cancelled",
                    "reason": reason,
                    "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                },
            )
        return run

    async def recover_interrupted_runs(
        self,
        session: AsyncSession,
        error_message: str = "Interrupted run recovered: process terminated while running",
    ) -> int:
        """Recovers any stale runs left in 'running' state on server boot or failure."""
        return await run_repo.recover_interrupted_runs(session, error_message=error_message)

    async def get_run(
        self,
        session: AsyncSession,
        run_id: str,
    ) -> Optional[AgentRun]:
        """Retrieves an AgentRun by ID."""
        return await run_repo.get_run(session, run_id)

    async def list_runs_by_project(
        self,
        session: AsyncSession,
        project_id: str,
        skip: int = 0,
        limit: int = 100,
    ) -> List[AgentRun]:
        """Lists AgentRuns for a Project ordered by created_at descending."""
        return await run_repo.list_runs_by_project(session, project_id, skip=skip, limit=limit)

    async def get_run_report(
        self,
        session: AsyncSession,
        run_id: str,
    ) -> Optional[DiagnosticReportModel]:
        """Retrieves the DiagnosticReportModel associated with a run."""
        return await report_repo.get_report_by_run(session, run_id)

    async def get_run_executions(
        self,
        session: AsyncSession,
        run_id: str,
    ) -> List[TestExecution]:
        """Lists all TestExecution records recorded for a run."""
        return await execution_repo.list_executions_by_run(session, run_id)

    async def get_run_investigations(
        self,
        session: AsyncSession,
        run_id: str,
    ) -> List[FailureInvestigationModel]:
        """Lists all FailureInvestigationModel records recorded for a run."""
        return await execution_repo.list_investigations_by_run(session, run_id)

    async def execute_run(
        self,
        run_id: str,
        knowledge_store: Optional[Any] = None,
        custom_base_url: Optional[str] = None,
        safe_dev_mode: bool = False,
    ) -> DiagnosticReport:
        """
        Executes a test run end-to-end.

        Crucial architectural invariant:
        Database transactions are NEVER held during the HTTP testing and agent graph phase.
        Phase 1: Short Tx to read run & project metadata, update status to 'running', commit & close.
        Phase 2: Untransacted agent execution (WorkflowOrchestrator.run_suite) with SSE event publishing.
        Phase 3: Short Tx to persist all executions, investigations, reports, and mark run 'completed'.
        On Failure: Short Tx to mark run 'failed' with sanitized error message.
        """
        cancel_event = asyncio.Event()
        self._cancellation_events[run_id] = cancel_event

        try:
            # -------------------------------------------------------------
            # Phase 1: Load metadata and mark run 'running' (Short Tx 1)
            # -------------------------------------------------------------
            async with self.session_factory() as session:
                run = await run_repo.get_run(session, run_id)
                if not run:
                    raise ValueError(f"AgentRun with ID '{run_id}' not found.")
                if run.status == "cancelled":
                    raise ValueError(f"AgentRun '{run_id}' is already cancelled.")
                if run.status == "completed":
                    raise ValueError(f"AgentRun '{run_id}' has already completed.")

                project = await project_repo.get_project(session, run.project_id)
                if not project or not project.spec_raw:
                    raise ValueError(
                        f"Project '{run.project_id}' does not exist or lacks specification data."
                    )

                project_id = project.id
                raw_spec_text = project.spec_raw
                project_base_url = project.base_url
                run_concurrency = run.concurrency
                target_filters = run.target_endpoints

                # Map existing database endpoint entities: (method, path) -> endpoint_id
                db_endpoints = await project_repo.list_endpoints_by_project(session, project_id)
                endpoint_id_map: Dict[Tuple[str, str], str] = {
                    (ep.method.upper(), ep.path): ep.id for ep in db_endpoints
                }
                id_to_operation_map: Dict[str, Tuple[str, str]] = {
                    ep.id: (ep.method.upper(), ep.path) for ep in db_endpoints
                }

                # Transition status to running
                await run_repo.update_run_status(
                    session=session,
                    run_id=run_id,
                    status="running",
                    started_at=utc_now(),
                )
                await session.commit()

            # -------------------------------------------------------------
            # Phase 2: Untransacted Agent Execution with Event Publishing
            # -------------------------------------------------------------
            if cancel_event.is_set():
                await self._handle_cancellation(run_id)
                raise asyncio.CancelledError(f"Run {run_id} cancelled before execution.")

            # Parse specification using OpenAPIParser
            parsed_spec: ParsedAPISpec = self.parser.parse_string(raw_spec_text)

            # Determine effective target base URL
            effective_base_url: Optional[str] = (
                custom_base_url
                or project_base_url
                or (parsed_spec.servers[0] if parsed_spec.servers else None)
            )
            if effective_base_url:
                effective_base_url = (
                    ReportDataSanitizer.sanitize_url(effective_base_url) or effective_base_url
                )

            # Filter operations if target_endpoints was specified
            operations_to_run: List[EndpointDefinition] = self._resolve_target_operations(
                parsed_spec=parsed_spec,
                target_filters=target_filters,
                id_to_operation_map=id_to_operation_map,
            )

            # Publish run_started event
            await self.event_manager.publish(
                run_id=run_id,
                event_type="run_started",
                data={
                    "run_id": run_id,
                    "project_id": project_id,
                    "total_operations": len(operations_to_run),
                    "started_at": utc_now().isoformat(),
                },
            )

            # Instantiate orchestrator with live SSE progress tracking
            published_ops: Set[Tuple[str, str]] = set()
            orchestrator = EventEmittingOrchestrator(
                event_mgr=self.event_manager,
                run_id=run_id,
                published_ops_tracker=published_ops,
                knowledge_store=knowledge_store,
                base_url=effective_base_url,
                max_concurrency=run_concurrency,
                safe_dev_mode=safe_dev_mode,
            )

            report: DiagnosticReport = await orchestrator.run_suite(
                spec=parsed_spec,
                operations=operations_to_run,
                raw_spec=parsed_spec.raw_spec,
                custom_base_url=effective_base_url,
            )

            # Replay any operations that may have been bypassed if run_suite was patched in tests
            for op_rec in report.operations:
                key = (op_rec.http_method, op_rec.path_template)
                if key not in published_ops:
                    await emit_operation_events(self.event_manager, run_id, op_rec)
                    published_ops.add(key)

            if cancel_event.is_set():
                await self._handle_cancellation(run_id)
                raise asyncio.CancelledError(f"Run {run_id} cancelled during execution.")

            # Render safe markdown representation
            safe_markdown = DiagnosticReportGenerator.to_markdown(report)

            # -------------------------------------------------------------
            # Phase 3: Persist Results and Mark 'completed' (Short Tx 2)
            # -------------------------------------------------------------
            async with self.session_factory() as session:
                # 1. Persist DiagnosticReport
                await report_repo.create_report(
                    session=session,
                    run_id=run_id,
                    report=report,
                    markdown_rendered=safe_markdown,
                )

                # 2. Persist TestExecutions and Investigations
                await execution_repo.bulk_create_executions_from_report(
                    session=session,
                    run_id=run_id,
                    endpoint_id_map=endpoint_id_map,
                    operations=report.operations,
                )

                # 3. Mark run completed
                await run_repo.update_run_status(
                    session=session,
                    run_id=run_id,
                    status="completed",
                    completed_at=utc_now(),
                    duration_ms=report.summary.total_duration_ms,
                    summary=report.summary.model_dump(),
                )
                await session.commit()

            # Publish run_completed event
            await self.event_manager.publish(
                run_id=run_id,
                event_type="run_completed",
                data={
                    "run_id": run_id,
                    "status": "completed",
                    "total_operations": report.summary.total_operations,
                    "passed_operations": report.summary.passed_operations,
                    "failed_operations": report.summary.failed_operations,
                    "duration_ms": report.summary.total_duration_ms,
                    "pass_rate_percentage": report.summary.pass_rate_percentage,
                },
            )

            logger.info("Run '%s' completed successfully.", run_id)
            return report

        except asyncio.CancelledError:
            logger.info("Run '%s' was cancelled.", run_id)
            raise

        except Exception as exc:
            logger.error("Run '%s' encountered an unhandled error: %s", run_id, exc, exc_info=True)
            # Fallback error recovery transaction
            await self._handle_failure(run_id, exc)
            raise

        finally:
            self._cancellation_events.pop(run_id, None)

    async def _handle_cancellation(self, run_id: str) -> None:
        """Marks run as cancelled in an isolated short transaction and publishes run_cancelled event."""
        try:
            async with self.session_factory() as session:
                await run_repo.cancel_run(
                    session=session,
                    run_id=run_id,
                    reason="Run cancelled during execution",
                )
                await session.commit()
            await self.event_manager.publish(
                run_id=run_id,
                event_type="run_cancelled",
                data={
                    "run_id": run_id,
                    "status": "cancelled",
                    "reason": "Run cancelled during execution",
                },
            )
        except Exception as err:
            logger.warning("Failed to record cancellation status for run %s: %s", run_id, err)

    async def _handle_failure(self, run_id: str, exc: Exception) -> None:
        """Marks run as failed in an isolated short transaction and publishes run_failed event."""
        try:
            safe_err = ReportDataSanitizer.sanitize_text(str(exc))
            async with self.session_factory() as session:
                run = await run_repo.get_run(session, run_id)
                if run and run.status == "cancelled":
                    return  # Preserve cancelled state
                await run_repo.update_run_status(
                    session=session,
                    run_id=run_id,
                    status="failed",
                    completed_at=utc_now(),
                    error_message=safe_err or "Unknown execution error occurred",
                )
                await session.commit()

            await self.event_manager.publish(
                run_id=run_id,
                event_type="run_failed",
                data={
                    "run_id": run_id,
                    "status": "failed",
                    "error_message": safe_err or "Unknown execution error occurred",
                },
            )
        except Exception as err:
            logger.error("Failed to record failure status for run %s: %s", run_id, err)

    def _resolve_target_operations(
        self,
        parsed_spec: ParsedAPISpec,
        target_filters: Optional[Any],
        id_to_operation_map: Dict[str, Tuple[str, str]],
    ) -> List[EndpointDefinition]:
        """
        Filters parsed_spec.endpoints according to target_filters.
        Supports endpoint UUIDs, route paths (e.g. '/items'), and 'METHOD /path' pairs.
        """
        if not target_filters or not isinstance(target_filters, (list, tuple)):
            return list(parsed_spec.endpoints)

        allowed_pairs: Set[Tuple[str, str]] = set()

        for f in target_filters:
            item_str = str(f).strip()
            # 1. Match by endpoint UUID
            if item_str in id_to_operation_map:
                allowed_pairs.add(id_to_operation_map[item_str])
                continue

            # 2. Match by "METHOD /path" format (e.g. "GET /products")
            parts = item_str.split(" ", 1)
            if len(parts) == 2 and parts[0].upper() in (
                "GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "TRACE"
            ):
                allowed_pairs.add((parts[0].upper(), parts[1]))
                continue

            # 3. Match by path template alone
            for ep in parsed_spec.endpoints:
                if ep.path == item_str:
                    allowed_pairs.add((ep.method.upper(), ep.path))

        if not allowed_pairs:
            logger.warning(
                "None of the target_endpoints filters matched specification operations. Running all endpoints."
            )
            return list(parsed_spec.endpoints)

        filtered = [
            op for op in parsed_spec.endpoints
            if (op.method.upper(), op.path) in allowed_pairs
        ]
        return filtered
