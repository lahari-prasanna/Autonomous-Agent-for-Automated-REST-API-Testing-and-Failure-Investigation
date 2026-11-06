"""
Repositories Package (Phase 5B).
Exports database access functions for Projects, Endpoints, Runs, Executions, and Reports.
"""

from __future__ import annotations

from backend.app.repositories.execution_repo import (
    bulk_create_executions_from_report,
    create_investigation,
    create_test_case,
    create_test_execution,
    get_execution,
    get_investigation_by_execution,
    get_test_case,
    list_executions_by_run,
    list_investigations_by_run,
    list_test_cases_by_endpoint,
    sanitize_snapshot_dict,
)
from backend.app.repositories.project_repo import (
    bulk_create_endpoints,
    create_endpoint,
    create_project,
    delete_project,
    find_endpoint_by_operation,
    get_endpoint,
    get_project,
    list_endpoints_by_project,
    list_projects,
    update_project,
)
from backend.app.repositories.report_repo import (
    create_report,
    get_report_by_id,
    get_report_by_run,
)
from backend.app.repositories.run_repo import (
    cancel_run,
    create_run,
    get_run,
    list_runs_by_project,
    recover_interrupted_runs,
    update_run,
    update_run_status,
)

__all__ = [
    # Project & Endpoints
    "create_project",
    "get_project",
    "list_projects",
    "update_project",
    "delete_project",
    "create_endpoint",
    "bulk_create_endpoints",
    "get_endpoint",
    "list_endpoints_by_project",
    "find_endpoint_by_operation",
    # Runs
    "create_run",
    "get_run",
    "list_runs_by_project",
    "update_run",
    "update_run_status",
    "cancel_run",
    "recover_interrupted_runs",
    # Executions & TestCases
    "create_test_case",
    "get_test_case",
    "list_test_cases_by_endpoint",
    "create_test_execution",
    "get_execution",
    "list_executions_by_run",
    "create_investigation",
    "list_investigations_by_run",
    "get_investigation_by_execution",
    "bulk_create_executions_from_report",
    "sanitize_snapshot_dict",
    # Reports
    "create_report",
    "get_report_by_run",
    "get_report_by_id",
]
