import { useEffect, useRef, useState, useCallback } from 'react';
import type { RunStatus } from '../types/api';
import {
  ALL_SSE_EVENT_TYPES,
  type SSEConnectionState,
  type SSEMessagePayload,
  type TimelineEvent,
  type LiveOperationProgress,
  type RunStartedData,
  type OperationStartedData,
  type AttemptCompletedData,
  type InvestigationCompletedData,
  type RetestCompletedData,
  type VariationCompletedData,
  type OperationCompletedData,
  type RunCompletedData,
} from '../types/events';

interface UseRunEventsOptions {
  runId: string | undefined;
  initialStatus: RunStatus | undefined;
  onTerminalEvent?: () => void;
}

export interface LiveMetrics {
  totalOperations: number | null;
  completedOperations: number;
  passedOperations: number;
  failedOperations: number;
  investigationsCount: number;
  retestsCount: number;
  variationsCount: number;
  durationMs: number | null;
  lastHeartbeat: string | null;
  liveStatus: RunStatus | undefined;
}

export function useRunEvents({
  runId,
  initialStatus,
  onTerminalEvent,
}: UseRunEventsOptions) {
  const [connectionState, setConnectionState] = useState<SSEConnectionState>(() => {
    if (initialStatus === 'completed' || initialStatus === 'failed' || initialStatus === 'cancelled') {
      return 'completed';
    }
    return 'connecting';
  });

  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [operations, setOperations] = useState<Record<string, LiveOperationProgress>>({});
  const [metrics, setMetrics] = useState<LiveMetrics>({
    totalOperations: null,
    completedOperations: 0,
    passedOperations: 0,
    failedOperations: 0,
    investigationsCount: 0,
    retestsCount: 0,
    variationsCount: 0,
    durationMs: null,
    lastHeartbeat: null,
    liveStatus: initialStatus,
  });

  const processedEventIds = useRef<Set<string>>(new Set());
  const eventSourceRef = useRef<EventSource | null>(null);
  const onTerminalEventRef = useRef(onTerminalEvent);
  onTerminalEventRef.current = onTerminalEvent;

  // Sync initial status when query loads or updates
  useEffect(() => {
    if (initialStatus) {
      setMetrics((prev) => ({
        ...prev,
        liveStatus: prev.liveStatus || initialStatus,
      }));

      if (
        initialStatus === 'completed' ||
        initialStatus === 'failed' ||
        initialStatus === 'cancelled'
      ) {
        setConnectionState('completed');
      }
    }
  }, [initialStatus]);

  const handleEvent = useCallback((event: MessageEvent) => {
    const rawEventId = event.lastEventId;

    // Deduplicate replay on reconnect (skip hb which is not an incrementing event id)
    if (rawEventId && rawEventId !== 'hb') {
      if (processedEventIds.current.has(rawEventId)) {
        return;
      }
      processedEventIds.current.add(rawEventId);
    }

    let payload: SSEMessagePayload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }

    const { event_type, timestamp, data } = payload;
    const eventId = rawEventId || `ev_${Date.now()}_${Math.random().toString(36).substring(2, 6)}`;

    // Add to chronological timeline (keep latest 300 items)
    setTimeline((prev) => {
      const next = [
        {
          id: eventId,
          eventType: event_type,
          timestamp,
          data: data as Record<string, unknown>,
        },
        ...prev,
      ];
      return next.slice(0, 300);
    });

    // Handle typed progress updates
    switch (event_type) {
      case 'heartbeat': {
        setMetrics((prev) => ({ ...prev, lastHeartbeat: timestamp }));
        break;
      }

      case 'run_created': {
        setMetrics((prev) => ({ ...prev, liveStatus: 'pending' }));
        break;
      }

      case 'run_started': {
        const d = data as RunStartedData;
        setMetrics((prev) => ({
          ...prev,
          liveStatus: 'running',
          totalOperations: d.total_operations,
        }));
        break;
      }

      case 'operation_started': {
        const d = data as OperationStartedData;
        setOperations((prev) => {
          const current = prev[d.operation_id];
          return {
            ...prev,
            [d.operation_id]: {
              operationId: d.operation_id,
              method: d.method,
              path: d.path,
              status: 'running',
              retests: current?.retests || [],
              variations: current?.variations || [],
              initialAttempt: current?.initialAttempt,
              investigation: current?.investigation,
              finalStatus: current?.finalStatus,
              passed: current?.passed,
            },
          };
        });
        break;
      }

      case 'attempt_completed': {
        const d = data as AttemptCompletedData;
        setOperations((prev) => {
          const current = prev[d.operation_id];
          return {
            ...prev,
            [d.operation_id]: {
              operationId: d.operation_id,
              method: current?.method || 'OP',
              path: current?.path || d.operation_id,
              status: current?.status || 'running',
              retests: current?.retests || [],
              variations: current?.variations || [],
              initialAttempt: {
                statusCode: d.status_code,
                latencyMs: d.latency_ms,
                passed: d.passed,
                errorType: d.error_type,
              },
            },
          };
        });
        break;
      }

      case 'investigation_completed': {
        const d = data as InvestigationCompletedData;
        setMetrics((prev) => ({
          ...prev,
          investigationsCount: prev.investigationsCount + 1,
        }));
        setOperations((prev) => {
          const current = prev[d.operation_id];
          if (!current) return prev;
          return {
            ...prev,
            [d.operation_id]: {
              ...current,
              investigation: {
                failureCategory: d.failure_category,
                inferencesCount: d.inferences_count,
                degraded: d.investigation_degraded,
              },
            },
          };
        });
        break;
      }

      case 'retest_completed': {
        const d = data as RetestCompletedData;
        setMetrics((prev) => ({
          ...prev,
          retestsCount: prev.retestsCount + 1,
        }));
        setOperations((prev) => {
          const current = prev[d.operation_id];
          if (!current) return prev;
          return {
            ...prev,
            [d.operation_id]: {
              ...current,
              retests: [
                ...current.retests,
                {
                  attemptNumber: d.attempt_number,
                  statusCode: d.status_code,
                  passed: d.passed,
                },
              ],
            },
          };
        });
        break;
      }

      case 'variation_completed': {
        const d = data as VariationCompletedData;
        setMetrics((prev) => ({
          ...prev,
          variationsCount: prev.variationsCount + 1,
        }));
        setOperations((prev) => {
          const current = prev[d.operation_id];
          if (!current) return prev;
          return {
            ...prev,
            [d.operation_id]: {
              ...current,
              variations: [
                ...current.variations,
                {
                  variationNumber: d.variation_number,
                  candidateId: d.candidate_id,
                  statusCode: d.status_code,
                  passed: d.passed,
                },
              ],
            },
          };
        });
        break;
      }

      case 'operation_completed': {
        const d = data as OperationCompletedData;
        setOperations((prev) => {
          const current = prev[d.operation_id];
          return {
            ...prev,
            [d.operation_id]: {
              operationId: d.operation_id,
              method: d.method || current?.method || 'OP',
              path: d.path || current?.path || d.operation_id,
              status: d.passed ? 'completed' : 'failed',
              retests: current?.retests || [],
              variations: current?.variations || [],
              finalStatus: d.final_status,
              passed: d.passed,
            },
          };
        });

        setMetrics((prev) => ({
          ...prev,
          completedOperations: prev.completedOperations + 1,
          passedOperations: d.passed ? prev.passedOperations + 1 : prev.passedOperations,
          failedOperations: !d.passed ? prev.failedOperations + 1 : prev.failedOperations,
        }));
        break;
      }

      case 'run_completed': {
        const d = data as RunCompletedData;
        setMetrics((prev) => ({
          ...prev,
          liveStatus: 'completed',
          totalOperations: d.total_operations ?? prev.totalOperations,
          passedOperations: d.passed_operations ?? prev.passedOperations,
          failedOperations: d.failed_operations ?? prev.failedOperations,
          durationMs: d.duration_ms ?? prev.durationMs,
        }));
        setConnectionState('completed');
        if (eventSourceRef.current) {
          eventSourceRef.current.close();
          eventSourceRef.current = null;
        }
        onTerminalEventRef.current?.();
        break;
      }

      case 'run_cancelled': {
        setMetrics((prev) => ({ ...prev, liveStatus: 'cancelled' }));
        setConnectionState('completed');
        if (eventSourceRef.current) {
          eventSourceRef.current.close();
          eventSourceRef.current = null;
        }
        onTerminalEventRef.current?.();
        break;
      }

      case 'run_failed': {
        setMetrics((prev) => ({ ...prev, liveStatus: 'failed' }));
        setConnectionState('completed');
        if (eventSourceRef.current) {
          eventSourceRef.current.close();
          eventSourceRef.current = null;
        }
        onTerminalEventRef.current?.();
        break;
      }
    }
  }, []);

  // Connect EventSource
  useEffect(() => {
    if (!runId) return;

    // If initial status was already terminal, don't open SSE stream
    if (
      initialStatus === 'completed' ||
      initialStatus === 'failed' ||
      initialStatus === 'cancelled'
    ) {
      setConnectionState('completed');
      return;
    }

    setConnectionState('connecting');

    const sseUrl = `/api/v1/runs/${runId}/events`;
    const es = new EventSource(sseUrl);
    eventSourceRef.current = es;

    es.onopen = () => {
      setConnectionState('connected');
    };

    es.onerror = () => {
      // Browser EventSource automatically attempts to reconnect and sends Last-Event-ID
      setConnectionState((curr) => (curr === 'completed' ? 'completed' : 'reconnecting'));
    };

    // Register all named backend event types
    ALL_SSE_EVENT_TYPES.forEach((type) => {
      es.addEventListener(type, handleEvent);
    });

    return () => {
      ALL_SSE_EVENT_TYPES.forEach((type) => {
        es.removeEventListener(type, handleEvent);
      });
      es.close();
      eventSourceRef.current = null;
      setConnectionState((curr) => (curr === 'completed' ? 'completed' : 'disconnected'));
    };
  }, [runId, initialStatus, handleEvent]);

  return {
    connectionState,
    timeline,
    operations,
    metrics,
  };
}
