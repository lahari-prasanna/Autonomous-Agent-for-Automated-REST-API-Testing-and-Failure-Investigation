/**
 * Server-Sent Events (SSE) contract types matching backend event_manager and run_service.
 */

export type SSEEventType =
  | 'heartbeat'
  | 'run_created'
  | 'run_started'
  | 'operation_started'
  | 'attempt_completed'
  | 'investigation_completed'
  | 'retest_completed'
  | 'variation_completed'
  | 'operation_completed'
  | 'run_completed'
  | 'run_cancelled'
  | 'run_failed';

export const ALL_SSE_EVENT_TYPES: SSEEventType[] = [
  'heartbeat',
  'run_created',
  'run_started',
  'operation_started',
  'attempt_completed',
  'investigation_completed',
  'retest_completed',
  'variation_completed',
  'operation_completed',
  'run_completed',
  'run_cancelled',
  'run_failed',
];

export const TERMINAL_SSE_EVENT_TYPES: Set<string> = new Set([
  'run_completed',
  'run_failed',
  'run_cancelled',
]);

export interface HeartbeatData {
  heartbeat: boolean;
}

export interface RunCreatedData {
  run_id: string;
  project_id: string;
  status: string;
  concurrency: number;
  target_endpoints?: string[] | null;
}

export interface RunStartedData {
  run_id: string;
  project_id: string;
  total_operations: number;
  started_at: string;
}

export interface OperationStartedData {
  operation_id: string;
  method: string;
  path: string;
}

export interface AttemptCompletedData {
  operation_id: string;
  attempt_number: number;
  type: 'initial' | string;
  status_code?: number | null;
  latency_ms?: number | null;
  passed: boolean;
  dispatched: boolean;
  error_type?: string | null;
}

export interface InvestigationCompletedData {
  operation_id: string;
  failure_category?: string | null;
  inferences_count: number;
  investigation_degraded: boolean;
}

export interface RetestCompletedData {
  operation_id: string;
  type: string;
  attempt_number: number;
  retry_of: string;
  status_code?: number | null;
  passed: boolean;
}

export interface VariationCompletedData {
  operation_id: string;
  type: string;
  variation_number: number;
  candidate_id: string;
  status_code?: number | null;
  passed: boolean;
}

export interface OperationCompletedData {
  operation_id: string;
  method: string;
  path: string;
  final_status: string;
  passed: boolean;
}

export interface RunCompletedData {
  run_id: string;
  status: 'completed';
  total_operations: number;
  passed_operations: number;
  failed_operations: number;
  duration_ms?: number | null;
  pass_rate_percentage?: number | null;
}

export interface RunCancelledData {
  run_id: string;
  status: 'cancelled';
  reason?: string | null;
  completed_at?: string | null;
}

export interface RunFailedData {
  run_id: string;
  status: 'failed';
  error_message?: string | null;
}

export type SSEEventData =
  | HeartbeatData
  | RunCreatedData
  | RunStartedData
  | OperationStartedData
  | AttemptCompletedData
  | InvestigationCompletedData
  | RetestCompletedData
  | VariationCompletedData
  | OperationCompletedData
  | RunCompletedData
  | RunCancelledData
  | RunFailedData
  | Record<string, unknown>;

export interface SSEMessagePayload<T = SSEEventData> {
  run_id: string;
  event_type: SSEEventType;
  timestamp: string;
  data: T;
}

export interface TimelineEvent {
  id: string;
  eventType: SSEEventType;
  timestamp: string;
  data: Record<string, unknown>;
}

export interface LiveOperationProgress {
  operationId: string;
  method: string;
  path: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  initialAttempt?: {
    statusCode?: number | null;
    latencyMs?: number | null;
    passed: boolean;
    errorType?: string | null;
  };
  investigation?: {
    failureCategory?: string | null;
    inferencesCount: number;
    degraded: boolean;
  };
  retests: Array<{
    attemptNumber: number;
    statusCode?: number | null;
    passed: boolean;
  }>;
  variations: Array<{
    variationNumber: number;
    candidateId: string;
    statusCode?: number | null;
    passed: boolean;
  }>;
  finalStatus?: string;
  passed?: boolean;
}

export type SSEConnectionState =
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'completed'
  | 'disconnected';
