import type { LiveOperationProgress } from '../types/events';
import type { RunResponse } from '../types/api';
import type { DiagnosticReportData, OperationRunRecord } from '../types/report';
import type { LiveMetrics } from '../hooks/useRunEvents';

/**
 * Hydrates operations dictionary from completed report operations when live operations are empty.
 * If live operations are present (from SSE or active run), preserves live operations without overwriting.
 */
export function hydrateOperations(
  liveOperations: Record<string, LiveOperationProgress>,
  reportOperations?: OperationRunRecord[]
): Record<string, LiveOperationProgress> {
  // If live state has operations (from active SSE or replay), preserve it
  if (Object.keys(liveOperations).length > 0) {
    return liveOperations;
  }

  // If live state is empty but report operations exist, hydrate from report
  if (reportOperations && reportOperations.length > 0) {
    const opMap: Record<string, LiveOperationProgress> = {};
    for (const op of reportOperations) {
      const opKey = op.operation_id || `${op.http_method} ${op.path_template}`;
      const isPassed =
        op.final_outcome?.passed ??
        (op.final_status === 'passed' || op.final_status === 'resolved_after_transient_retry');

      opMap[opKey] = {
        operationId: opKey,
        method: op.http_method,
        path: op.path_template,
        status: isPassed ? 'completed' : 'failed',
        initialAttempt: op.initial_attempt
          ? {
              statusCode: op.initial_attempt.status_code,
              latencyMs: op.initial_attempt.latency_ms,
              passed: op.initial_attempt.passed,
              errorType: op.initial_attempt.error_type,
            }
          : undefined,
        investigation: op.investigation
          ? {
              failureCategory: op.initial_attempt?.failure_category,
              inferencesCount: op.investigation.inferences?.length ?? 0,
              degraded: op.investigation.investigation_degraded ?? false,
            }
          : undefined,
        retests: (op.transient_retries || []).map((tr) => ({
          attemptNumber: tr.attempt_number,
          statusCode: tr.outcome?.status_code,
          passed: tr.outcome?.passed ?? false,
        })),
        variations: (op.variation_tests || []).map((vt) => ({
          variationNumber: vt.variation_number,
          candidateId: vt.candidate_id,
          statusCode: vt.outcome?.status_code,
          passed: vt.outcome?.passed ?? false,
        })),
        finalStatus: String(op.final_status),
        passed: isPassed,
      };
    }
    return opMap;
  }

  return liveOperations;
}

export interface RunDisplayMetrics {
  displayDuration: string;
  totalOpsDisplay: number | string;
  completedOpsCount: number | string;
  passedOpsDisplay: number;
  failedOpsDisplay: number;
  investigationsDisplay: number;
  retestsDisplay: number;
  variationsDisplay: number;
}

/**
 * Computes user-facing metrics with nullish coalescing to safely preserve 0 values.
 * When run is completed, persisted summary and report data take precedence.
 * When run is executing, live SSE metrics take precedence.
 */
export function calculateRunDisplayMetrics(
  run: RunResponse | undefined,
  report: DiagnosticReportData | undefined,
  metrics: LiveMetrics,
  isRunCompleted: boolean,
  elapsedSeconds: number | null = null,
  hydratedOpsCount: number = 0
): RunDisplayMetrics {
  const summaryData = run?.summary ?? report?.summary;

  // Duration
  const displayDuration =
    run?.duration_ms !== null && run?.duration_ms !== undefined
      ? `${(run.duration_ms / 1000).toFixed(2)}s`
      : report?.summary?.total_duration_ms !== null && report?.summary?.total_duration_ms !== undefined
      ? `${(report.summary.total_duration_ms / 1000).toFixed(2)}s`
      : metrics.durationMs !== null
      ? `${(metrics.durationMs / 1000).toFixed(2)}s`
      : elapsedSeconds !== null
      ? `${elapsedSeconds}s`
      : '-';

  // Total operations
  const totalOpsDisplay =
    (isRunCompleted ? summaryData?.total_operations : null) ??
    metrics.totalOperations ??
    summaryData?.total_operations ??
    (report?.operations ? report.operations.length : null) ??
    (run?.target_endpoints ? run.target_endpoints.length : null) ??
    '-';

  // Completed operations count
  const completedOpsCount =
    isRunCompleted
      ? (summaryData?.total_operations ??
         (report?.operations ? report.operations.length : null) ??
         (hydratedOpsCount > 0 ? hydratedOpsCount : null) ??
         metrics.completedOperations)
      : metrics.completedOperations;

  // Passed operations
  const passedOpsDisplay =
    (isRunCompleted
      ? (summaryData?.passed_operations ?? report?.summary?.passed_operations)
      : metrics.passedOperations) ??
    summaryData?.passed_operations ??
    report?.summary?.passed_operations ??
    (report?.operations ? report.operations.filter((op: OperationRunRecord) => op.final_outcome?.passed).length : null) ??
    0;

  // Failed operations
  const failedOpsDisplay =
    (isRunCompleted
      ? (summaryData?.failed_operations ?? report?.summary?.failed_operations)
      : metrics.failedOperations) ??
    summaryData?.failed_operations ??
    report?.summary?.failed_operations ??
    (report?.operations ? report.operations.filter((op: OperationRunRecord) => !op.final_outcome?.passed).length : null) ??
    0;

  // Investigations: In report, only operations with an investigation record actually conducted one
  const investigationsCountFromReport = report?.operations
    ? report.operations.filter((op: OperationRunRecord) => Boolean(op.investigation)).length
    : null;

  const investigationsDisplay =
    (isRunCompleted
      ? investigationsCountFromReport
      : metrics.investigationsCount) ??
    investigationsCountFromReport ??
    0;

  // Retests
  const retestsCountFromReport = report?.operations
    ? report.operations.reduce((sum: number, op: OperationRunRecord) => sum + (op.transient_retries?.length || 0), 0)
    : null;

  const retestsDisplay =
    (isRunCompleted
      ? (summaryData?.total_transient_retries_dispatched ?? retestsCountFromReport)
      : metrics.retestsCount) ??
    summaryData?.total_transient_retries_dispatched ??
    retestsCountFromReport ??
    0;

  // Variations
  const variationsCountFromReport = report?.operations
    ? report.operations.reduce((sum: number, op: OperationRunRecord) => sum + (op.variation_tests?.length || 0), 0)
    : null;

  const variationMetrics = summaryData?.variation_metrics;
  const variationsCountFromMetrics =
    variationMetrics?.candidates_dispatched ??
    (variationMetrics?.variation_tests_passed !== undefined && variationMetrics?.variation_tests_failed !== undefined
      ? (Number(variationMetrics.variation_tests_passed) + Number(variationMetrics.variation_tests_failed))
      : null);

  const variationsDisplay =
    (isRunCompleted
      ? (variationsCountFromMetrics ?? variationsCountFromReport)
      : metrics.variationsCount) ??
    variationsCountFromMetrics ??
    variationsCountFromReport ??
    0;

  return {
    displayDuration,
    totalOpsDisplay,
    completedOpsCount,
    passedOpsDisplay,
    failedOpsDisplay,
    investigationsDisplay,
    retestsDisplay,
    variationsDisplay,
  };
}

/**
 * Returns timeline empty-state messaging depending on run lifecycle and filter.
 */
export function getTimelineEmptyMessage(isCompleted: boolean, filterType: string = 'all'): {
  title?: string;
  description: string;
} {
  if (filterType !== 'all') {
    return {
      description: `No events match the selected "${filterType}" filter.`,
    };
  }

  if (isCompleted) {
    return {
      title: 'Real-time event stream history is no longer active.',
      description:
        'The test run has finished and event streaming has ended. Full operation results, evidence, and remediations are preserved in the Tracked Operations tab and Diagnostic Report.',
    };
  }

  return {
    description: 'No events recorded yet. Events will appear here in real-time as execution progresses.',
  };
}
