import test from 'node:test';
import assert from 'node:assert/strict';

import {
  hydrateOperations,
  calculateRunDisplayMetrics,
  getTimelineEmptyMessage,
} from '../src/utils/runHelpers.ts';
import type { RunResponse } from '../src/types/api.ts';
import type { DiagnosticReportData, OperationRunRecord } from '../src/types/report.ts';
import type { LiveMetrics } from '../src/hooks/useRunEvents.ts';
import type { LiveOperationProgress } from '../src/types/events.ts';

// Sample mock data matching backend schemas
const mockReportOperations: OperationRunRecord[] = [
  {
    operation_id: 'get_users',
    http_method: 'GET',
    path_template: '/users',
    initial_request_fingerprint: 'fp_1',
    initial_attempt: {
      dispatched: true,
      status_code: 200,
      latency_ms: 45,
      passed: true,
    },
    final_status: 'passed',
    final_outcome: {
      dispatched: true,
      status_code: 200,
      latency_ms: 45,
      passed: true,
    },
  },
  {
    operation_id: 'get_orders',
    http_method: 'GET',
    path_template: '/orders',
    initial_request_fingerprint: 'fp_2',
    initial_attempt: {
      dispatched: true,
      status_code: 200,
      latency_ms: 50,
      passed: true,
    },
    final_status: 'passed',
    final_outcome: {
      dispatched: true,
      status_code: 200,
      latency_ms: 50,
      passed: true,
    },
  },
  {
    operation_id: 'get_items',
    http_method: 'GET',
    path_template: '/items',
    initial_request_fingerprint: 'fp_3',
    initial_attempt: {
      dispatched: true,
      status_code: 200,
      latency_ms: 30,
      passed: true,
    },
    final_status: 'passed',
    final_outcome: {
      dispatched: true,
      status_code: 200,
      latency_ms: 30,
      passed: true,
    },
  },
  {
    operation_id: 'get_inventory',
    http_method: 'GET',
    path_template: '/inventory',
    initial_request_fingerprint: 'fp_4',
    initial_attempt: {
      dispatched: true,
      status_code: 200,
      latency_ms: 40,
      passed: true,
    },
    final_status: 'passed',
    final_outcome: {
      dispatched: true,
      status_code: 200,
      latency_ms: 40,
      passed: true,
    },
  },
  {
    operation_id: 'get_settings',
    http_method: 'GET',
    path_template: '/settings',
    initial_request_fingerprint: 'fp_5',
    initial_attempt: {
      dispatched: true,
      status_code: 200,
      latency_ms: 25,
      passed: true,
    },
    final_status: 'passed',
    final_outcome: {
      dispatched: true,
      status_code: 200,
      latency_ms: 25,
      passed: true,
    },
  },
  {
    operation_id: 'post_order',
    http_method: 'POST',
    path_template: '/orders',
    initial_request_fingerprint: 'fp_6',
    initial_attempt: {
      dispatched: true,
      status_code: 500,
      latency_ms: 120,
      passed: false,
      failure_category: 'HTTP_5XX_SERVER_ERROR',
      error_type: 'InternalServerError',
    },
    investigation: {
      investigation_degraded: false,
      inferences: ['Database deadlock during order write transaction'],
    },
    transient_retries: [
      {
        attempt_number: 1,
        retry_of: 'initial',
        backoff_delay_seconds: 0.5,
        canonical_request_fingerprint: 'fp_6_r1',
        outcome: {
          dispatched: true,
          status_code: 500,
          latency_ms: 95,
          passed: false,
        },
      },
    ],
    variation_tests: [
      {
        variation_number: 1,
        candidate_id: 'cand_1',
        canonical_request_fingerprint: 'fp_6_v1',
        outcome: {
          dispatched: true,
          status_code: 201,
          latency_ms: 80,
          passed: true,
        },
      },
      {
        variation_number: 2,
        candidate_id: 'cand_2',
        canonical_request_fingerprint: 'fp_6_v2',
        outcome: {
          dispatched: true,
          status_code: 500,
          latency_ms: 110,
          passed: false,
        },
      },
    ],
    final_status: 'failed',
    final_outcome: {
      dispatched: true,
      status_code: 500,
      latency_ms: 120,
      passed: false,
    },
  },
  {
    operation_id: 'delete_user',
    http_method: 'DELETE',
    path_template: '/users/{id}',
    initial_request_fingerprint: 'fp_7',
    initial_attempt: {
      dispatched: true,
      status_code: 403,
      latency_ms: 35,
      passed: false,
      failure_category: 'HTTP_4XX_CLIENT_ERROR',
    },
    investigation: {
      investigation_degraded: false,
      inferences: ['Missing required administrative auth scope'],
    },
    final_status: 'failed',
    final_outcome: {
      dispatched: true,
      status_code: 403,
      latency_ms: 35,
      passed: false,
    },
  },
];

const mockCompletedReport: DiagnosticReportData = {
  run_id: 'run-12345',
  project_id: 'proj-abc',
  spec_title: 'Store API',
  spec_version: '1.0.0',
  base_url: 'http://localhost:8000',
  generated_at: '2026-11-08T11:00:00Z',
  summary: {
    total_operations: 7,
    passed_operations: 5,
    failed_operations: 2,
    error_operations: 0,
    resolved_by_transient_retry: 0,
    total_transient_retries_dispatched: 1,
    total_requests_executed: 10,
    pass_rate_percentage: 71.4,
    total_duration_ms: 4500,
    variation_metrics: {
      candidates_generated: 4,
      candidates_schema_valid: 4,
      candidates_approved: 2,
      candidates_dispatched: 2,
      variation_tests_passed: 1,
      variation_tests_failed: 1,
      variation_success_rate_percentage: 50.0,
    },
  },
  operations: mockReportOperations,
  remediation_summary: [],
};

const mockCompletedRun: RunResponse = {
  id: 'run-12345',
  project_id: 'proj-abc',
  status: 'completed',
  concurrency: 4,
  started_at: '2026-11-08T11:00:00Z',
  completed_at: '2026-11-08T11:00:05Z',
  duration_ms: 4500,
  created_at: '2026-11-08T10:59:59Z',
  summary: mockCompletedReport.summary,
};

const initialEmptyMetrics: LiveMetrics = {
  totalOperations: null,
  completedOperations: 0,
  passedOperations: 0,
  failedOperations: 0,
  investigationsCount: 0,
  retestsCount: 0,
  variationsCount: 0,
  durationMs: null,
  lastHeartbeat: null,
  liveStatus: 'completed',
};

test('Scenario 1: Opening an already completed run hydrates operations and displays correct metrics', () => {
  // Live state is empty when opening an already-completed run
  const liveOperations: Record<string, LiveOperationProgress> = {};

  // Hydrate from report
  const hydrated = hydrateOperations(liveOperations, mockCompletedReport.operations);
  const hydratedKeys = Object.keys(hydrated);

  // 1. Operations table hydrated to 7 operations
  assert.equal(hydratedKeys.length, 7, 'Must hydrate all 7 operations from report');
  assert.equal(hydrated['get_users'].status, 'completed');
  assert.equal(hydrated['post_order'].status, 'failed');
  assert.equal(hydrated['post_order'].investigation?.inferencesCount, 1);
  assert.equal(hydrated['post_order'].retests.length, 1);
  assert.equal(hydrated['post_order'].variations.length, 2);

  // 2. Summary metrics correctly extracted
  const metrics = calculateRunDisplayMetrics(
    mockCompletedRun,
    mockCompletedReport,
    initialEmptyMetrics,
    true,
    null,
    hydratedKeys.length
  );

  assert.equal(metrics.totalOpsDisplay, 7);
  assert.equal(metrics.completedOpsCount, 7, 'Completed operations must show 7 (7 / 7)');
  assert.equal(metrics.passedOpsDisplay, 5, 'Passed operations must be 5');
  assert.equal(metrics.failedOpsDisplay, 2, 'Failed operations must be 2');
  assert.equal(metrics.investigationsDisplay, 2, 'Investigations must be 2 (only failed ops with investigation)');
  assert.equal(metrics.retestsDisplay, 1, 'Retests must be 1');
  assert.equal(metrics.variationsDisplay, 2, 'Variations must be 2');
  assert.equal(metrics.displayDuration, '4.50s');
});

test('Scenario 2: Refreshing a completed run page retains state from report data', () => {
  // Simulating refresh: in-memory live state resets to {}
  const freshlyLoadedLiveOperations: Record<string, LiveOperationProgress> = {};

  // Queries refetch run and report
  const hydrated = hydrateOperations(freshlyLoadedLiveOperations, mockCompletedReport.operations);

  assert.equal(Object.keys(hydrated).length, 7);
  assert.equal(hydrated['delete_user'].path, '/users/{id}');
  assert.equal(hydrated['delete_user'].status, 'failed');

  const metrics = calculateRunDisplayMetrics(
    mockCompletedRun,
    mockCompletedReport,
    initialEmptyMetrics,
    true,
    null,
    Object.keys(hydrated).length
  );

  assert.equal(metrics.completedOpsCount, 7);
  assert.equal(metrics.passedOpsDisplay, 5);
  assert.equal(metrics.failedOpsDisplay, 2);
});

test('Scenario 3: Opening a run after backend restart (empty SSE history) shows hydrated ops and clean empty-state message', () => {
  // Server rebooted: SSE history is empty, so timeline has 0 events
  const timelineEvents: unknown[] = [];
  const emptyOperations: Record<string, LiveOperationProgress> = {};

  // Tracked operations still hydrate properly from report
  const hydrated = hydrateOperations(emptyOperations, mockCompletedReport.operations);
  assert.equal(Object.keys(hydrated).length, 7);

  // Live Event Stream empty state message
  const emptyState = getTimelineEmptyMessage(true, 'all');
  assert.equal(
    emptyState.title,
    'Real-time event stream history is no longer active.'
  );
  assert.match(
    emptyState.description,
    /The test run has finished and event streaming has ended/
  );

  // If filtered
  const filteredEmptyState = getTimelineEmptyMessage(true, 'investigations');
  assert.equal(filteredEmptyState.description, 'No events match the selected "investigations" filter.');
});

test('Scenario 4: Opening a currently running execution gives live SSE updates precedence over report data', () => {
  // Currently running run
  const runningRun: RunResponse = {
    id: 'run-99999',
    project_id: 'proj-abc',
    status: 'running',
    concurrency: 2,
    started_at: '2026-11-08T11:15:00Z',
    created_at: '2026-11-08T11:14:59Z',
  };

  // Live operations received from SSE
  const liveActiveOperations: Record<string, LiveOperationProgress> = {
    'get_users': {
      operationId: 'get_users',
      method: 'GET',
      path: '/users',
      status: 'completed',
      retests: [],
      variations: [],
      passed: true,
    },
    'post_order': {
      operationId: 'post_order',
      method: 'POST',
      path: '/orders',
      status: 'running',
      retests: [],
      variations: [],
      passed: false,
    },
  };

  const activeLiveMetrics: LiveMetrics = {
    totalOperations: 5,
    completedOperations: 1,
    passedOperations: 1,
    failedOperations: 0,
    investigationsCount: 0,
    retestsCount: 0,
    variationsCount: 0,
    durationMs: null,
    lastHeartbeat: '2026-11-08T11:15:02Z',
    liveStatus: 'running',
  };

  // Even if an old/stale report object was provided, liveActiveOperations must NOT be overwritten!
  const hydrated = hydrateOperations(liveActiveOperations, mockReportOperations);
  assert.equal(Object.keys(hydrated).length, 2, 'Must keep the 2 live active operations');
  assert.equal(hydrated['post_order'].status, 'running', 'Live active status must be preserved');

  // Metrics for executing run prioritize liveMetrics
  const metrics = calculateRunDisplayMetrics(
    runningRun,
    undefined,
    activeLiveMetrics,
    false, // isRunCompleted = false
    12, // 12 seconds elapsed
    Object.keys(hydrated).length
  );

  assert.equal(metrics.completedOpsCount, 1, 'Live completed operations count is used');
  assert.equal(metrics.totalOpsDisplay, 5, 'Live total operations is used');
  assert.equal(metrics.passedOpsDisplay, 1, 'Live passed operations is used');
  assert.equal(metrics.failedOpsDisplay, 0, 'Live failed operations is used');
  assert.equal(metrics.displayDuration, '12s', 'Active elapsed seconds duration is formatted');
});

test('Scenario 5: Zero vs non-zero summary displays safely preserve zero values without fallback', () => {
  // Case A: A completed run where 0 tests passed (all failed)
  const allFailedSummary = {
    total_operations: 4,
    passed_operations: 0,
    failed_operations: 4,
    total_transient_retries_dispatched: 0,
    variation_metrics: {
      candidates_dispatched: 0,
      variation_tests_passed: 0,
      variation_tests_failed: 0,
    },
  };

  const allFailedRun: RunResponse = {
    id: 'run-zero-pass',
    project_id: 'proj-abc',
    status: 'completed',
    concurrency: 1,
    created_at: '2026-11-08T11:00:00Z',
    summary: allFailedSummary,
  };

  const metricsA = calculateRunDisplayMetrics(
    allFailedRun,
    undefined,
    initialEmptyMetrics,
    true,
    null,
    4
  );

  assert.strictEqual(metricsA.passedOpsDisplay, 0, 'passedOpsDisplay MUST be numeric 0, not fallback');
  assert.strictEqual(metricsA.failedOpsDisplay, 4, 'failedOpsDisplay must be 4');
  assert.strictEqual(metricsA.retestsDisplay, 0, 'retestsDisplay MUST be numeric 0');
  assert.strictEqual(metricsA.variationsDisplay, 0, 'variationsDisplay MUST be numeric 0');

  // Case B: A completed run where 0 tests failed (all passed)
  const allPassedSummary = {
    total_operations: 6,
    passed_operations: 6,
    failed_operations: 0,
    total_transient_retries_dispatched: 0,
  };

  const allPassedRun: RunResponse = {
    id: 'run-zero-fail',
    project_id: 'proj-abc',
    status: 'completed',
    concurrency: 1,
    created_at: '2026-11-08T11:00:00Z',
    summary: allPassedSummary,
  };

  const metricsB = calculateRunDisplayMetrics(
    allPassedRun,
    undefined,
    initialEmptyMetrics,
    true,
    null,
    6
  );

  assert.strictEqual(metricsB.passedOpsDisplay, 6);
  assert.strictEqual(metricsB.failedOpsDisplay, 0, 'failedOpsDisplay MUST be numeric 0');
  assert.strictEqual(metricsB.investigationsDisplay, 0, 'investigationsDisplay MUST be numeric 0');
  assert.strictEqual(metricsB.retestsDisplay, 0, 'retestsDisplay MUST be numeric 0');
  assert.strictEqual(metricsB.variationsDisplay, 0, 'variationsDisplay MUST be numeric 0');

  // Case C: Active run with live metrics at 0
  const activeZeroMetrics: LiveMetrics = {
    totalOperations: 3,
    completedOperations: 0,
    passedOperations: 0,
    failedOperations: 0,
    investigationsCount: 0,
    retestsCount: 0,
    variationsCount: 0,
    durationMs: null,
    lastHeartbeat: null,
    liveStatus: 'running',
  };

  const runningRun: RunResponse = {
    id: 'run-active',
    project_id: 'proj-abc',
    status: 'running',
    concurrency: 1,
    created_at: '2026-11-08T11:00:00Z',
  };

  const metricsC = calculateRunDisplayMetrics(
    runningRun,
    undefined,
    activeZeroMetrics,
    false,
    0,
    0
  );

  assert.strictEqual(metricsC.passedOpsDisplay, 0, 'Live passed ops at 0 must render 0');
  assert.strictEqual(metricsC.failedOpsDisplay, 0, 'Live failed ops at 0 must render 0');
  assert.strictEqual(metricsC.completedOpsCount, 0, 'Live completed ops at 0 must render 0');
});
