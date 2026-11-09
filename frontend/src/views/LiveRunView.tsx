import React, { useState, useEffect, useMemo } from 'react';
import { useParams, Link } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../services/api';
import { Card } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { StatusBadge } from '../components/common/Badge';
import { PageLoader } from '../components/common/Loading';
import { ConnectionBadge } from '../components/runs/ConnectionBadge';
import { LiveOperationsTable } from '../components/runs/LiveOperationsTable';
import { LiveEventTimeline } from '../components/runs/LiveEventTimeline';
import { useRunEvents } from '../hooks/useRunEvents';
import { hydrateOperations, calculateRunDisplayMetrics } from '../utils/runHelpers';
import {
  ArrowLeft,
  XOctagon,
  FileText,
  SearchCheck,
  Flame,
  Sliders,
  CheckCircle2,
  XCircle,
  AlertTriangle,
} from 'lucide-react';

export const LiveRunView: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const queryClient = useQueryClient();

  const [activeTab, setActiveTab] = useState<'operations' | 'events'>('operations');
  const [elapsedSeconds, setElapsedSeconds] = useState<number | null>(null);

  const {
    data: run,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: ['run', id],
    queryFn: () => (id ? api.getRun(id) : Promise.reject('No run ID')),
    enabled: Boolean(id),
  });

  const { connectionState, timeline, operations, metrics } = useRunEvents({
    runId: id,
    initialStatus: run?.status,
    onTerminalEvent: () => {
      // Re-fetch finalized run record from database once terminal event arrives
      queryClient.invalidateQueries({ queryKey: ['run', id] });
      queryClient.invalidateQueries({ queryKey: ['report', id] });
    },
  });

  const liveStatus = metrics.liveStatus || run?.status;
  const isExecuting = liveStatus === 'running' || liveStatus === 'pending';
  const isRunCompleted = run?.status === 'completed' || liveStatus === 'completed';

  // Fetch full report when run is completed to guarantee hydrated state
  const { data: report } = useQuery({
    queryKey: ['report', id],
    queryFn: () => (id ? api.getReport(id) : Promise.reject('No run ID')),
    enabled: Boolean(id && isRunCompleted),
    retry: false,
  });

  // Hydrate operations from persisted report when live operations state is empty
  const hydratedOperations = useMemo(
    () => hydrateOperations(operations, report?.operations),
    [operations, report?.operations]
  );

  // Live elapsed duration ticker
  useEffect(() => {
    if (run?.started_at && isExecuting) {
      const startTime = new Date(run.started_at).getTime();
      const tick = () => {
        setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startTime) / 1000)));
      };
      tick();
      const interval = setInterval(tick, 1000);
      return () => clearInterval(interval);
    } else {
      setElapsedSeconds(null);
    }
  }, [run?.started_at, isExecuting]);

  const cancelMutation = useMutation({
    mutationFn: () => (id ? api.cancelRun(id) : Promise.reject('No run ID')),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['run', id] });
    },
  });

  if (isLoading) {
    return <PageLoader message="Loading test run execution state..." />;
  }

  if (isError || !run) {
    return (
      <div className="space-y-4">
        <Link to="/projects">
          <Button variant="ghost" size="sm" leftIcon={<ArrowLeft className="w-4 h-4" />}>
            Back to Projects
          </Button>
        </Link>
        <div className="p-8 rounded-xl bg-rose-50 border border-rose-200 text-center space-y-3">
          <p className="text-sm font-medium text-rose-700">
            {error instanceof Error ? error.message : 'Run not found.'}
          </p>
          <Button variant="secondary" size="sm" onClick={() => refetch()}>
            Retry
          </Button>
        </div>
      </div>
    );
  }

  const {
    displayDuration,
    totalOpsDisplay,
    completedOpsCount,
    passedOpsDisplay,
    failedOpsDisplay,
    investigationsDisplay,
    retestsDisplay,
    variationsDisplay,
  } = calculateRunDisplayMetrics(
    run,
    report,
    metrics,
    isRunCompleted,
    elapsedSeconds,
    Object.keys(hydratedOperations).length
  );

  return (
    <div className="space-y-6">
      {/* Header & Controls */}
      <div>
        <Link to={`/projects/${run.project_id}`} className="inline-block mb-3">
          <Button variant="ghost" size="sm" leftIcon={<ArrowLeft className="w-4 h-4" />}>
            Back to Project
          </Button>
        </Link>

        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 pb-4 border-b border-slate-200">
          <div>
            <div className="flex items-center gap-3">
              <h2 className="text-2xl font-bold text-slate-900">Live Test Run</h2>
              <StatusBadge status={liveStatus || 'pending'} />
            </div>
            <p className="text-xs sm:text-sm font-mono text-slate-500 mt-1">
              Run ID: {run.id}
            </p>
          </div>

          <div className="flex items-center gap-3">
            <ConnectionBadge state={connectionState} />

            {isExecuting && (
              <Button
                variant="danger"
                size="sm"
                leftIcon={<XOctagon className="w-4 h-4" />}
                isLoading={cancelMutation.isPending}
                onClick={() => cancelMutation.mutate()}
              >
                Cancel Run
              </Button>
            )}

            {liveStatus === 'completed' && (
              <Link to={`/runs/${run.id}/report`}>
                <Button
                  variant="primary"
                  size="sm"
                  leftIcon={<FileText className="w-4 h-4" />}
                >
                  View Diagnostic Report
                </Button>
              </Link>
            )}
          </div>
        </div>
      </div>

      {/* Primary Execution Metrics Grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card className="p-4 bg-white border-slate-200 shadow-sm">
          <div className="text-xs text-slate-500 uppercase font-semibold">Status</div>
          <div className="mt-2 text-base font-bold text-slate-900 capitalize flex items-center gap-2">
            <span>{liveStatus}</span>
            {isExecuting && <span className="w-2 h-2 rounded-full bg-blue-600 animate-ping" />}
          </div>
        </Card>

        <Card className="p-4 bg-white border-slate-200 shadow-sm">
          <div className="text-xs text-slate-500 uppercase font-semibold">Duration</div>
          <div className="mt-2 text-base font-mono font-bold text-slate-900">
            {displayDuration}
          </div>
        </Card>

        <Card className="p-4 bg-white border-slate-200 shadow-sm">
          <div className="text-xs text-slate-500 uppercase font-semibold">Operations</div>
          <div className="mt-2 text-base font-mono font-bold text-slate-900">
            {completedOpsCount} / {totalOpsDisplay}
          </div>
        </Card>

        <Card className="p-4 bg-white border-slate-200 shadow-sm">
          <div className="text-xs text-slate-500 uppercase font-semibold">Workers Concurrency</div>
          <div className="mt-2 text-base font-mono font-bold text-slate-900">
            {run.concurrency} worker{run.concurrency === 1 ? '' : 's'}
          </div>
        </Card>
      </div>

      {/* Secondary Progress Counters */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
        <div className="p-3 rounded-xl bg-white border border-slate-200 shadow-sm flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-emerald-50 border border-emerald-200 flex items-center justify-center text-emerald-600 shrink-0">
            <CheckCircle2 className="w-4 h-4" />
          </div>
          <div>
            <span className="text-[10px] text-slate-500 uppercase font-semibold block">Passed</span>
            <span className="text-sm font-bold font-mono text-emerald-600">{passedOpsDisplay}</span>
          </div>
        </div>

        <div className="p-3 rounded-xl bg-white border border-slate-200 shadow-sm flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-rose-50 border border-rose-200 flex items-center justify-center text-rose-600 shrink-0">
            <XCircle className="w-4 h-4" />
          </div>
          <div>
            <span className="text-[10px] text-slate-500 uppercase font-semibold block">Failed</span>
            <span className="text-sm font-bold font-mono text-rose-600">{failedOpsDisplay}</span>
          </div>
        </div>

        <div className="p-3 rounded-xl bg-white border border-slate-200 shadow-sm flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-purple-50 border border-purple-200 flex items-center justify-center text-purple-600 shrink-0">
            <SearchCheck className="w-4 h-4" />
          </div>
          <div>
            <span className="text-[10px] text-slate-500 uppercase font-semibold block">Investigations</span>
            <span className="text-sm font-bold font-mono text-purple-600">{investigationsDisplay}</span>
          </div>
        </div>

        <div className="p-3 rounded-xl bg-white border border-slate-200 shadow-sm flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-amber-50 border border-amber-200 flex items-center justify-center text-amber-600 shrink-0">
            <Flame className="w-4 h-4" />
          </div>
          <div>
            <span className="text-[10px] text-slate-500 uppercase font-semibold block">Retests</span>
            <span className="text-sm font-bold font-mono text-amber-600">{retestsDisplay}</span>
          </div>
        </div>

        <div className="p-3 rounded-xl bg-white border border-slate-200 shadow-sm flex items-center gap-3 col-span-2 sm:col-span-1">
          <div className="w-8 h-8 rounded-lg bg-cyan-50 border border-cyan-200 flex items-center justify-center text-cyan-600 shrink-0">
            <Sliders className="w-4 h-4" />
          </div>
          <div>
            <span className="text-[10px] text-slate-500 uppercase font-semibold block">Variations</span>
            <span className="text-sm font-bold font-mono text-cyan-700">{variationsDisplay}</span>
          </div>
        </div>
      </div>

      {/* Error Message if run failed */}
      {run.error_message && (
        <div className="p-4 rounded-xl bg-rose-50 border border-rose-200 text-rose-800 text-xs flex items-start gap-3">
          <AlertTriangle className="w-4 h-4 text-rose-600 shrink-0 mt-0.5" />
          <div>
            <span className="font-bold">Execution Terminated with Error: </span>
            <p className="mt-0.5 font-mono">{run.error_message}</p>
          </div>
        </div>
      )}

      {/* View Switcher: Operations vs Live Event Timeline */}
      <div className="flex items-center justify-between border-b border-slate-200 pb-2">
        <div className="inline-flex p-1 bg-slate-100 rounded-lg border border-slate-200">
          <button
            type="button"
            onClick={() => setActiveTab('operations')}
            className={`px-3 py-1.5 rounded-md text-xs font-semibold transition-all ${
              activeTab === 'operations'
                ? 'bg-white text-blue-700 shadow-sm'
                : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            Tracked Operations ({Object.keys(hydratedOperations).length})
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('events')}
            className={`px-3 py-1.5 rounded-md text-xs font-semibold transition-all ${
              activeTab === 'events'
                ? 'bg-white text-blue-700 shadow-sm'
                : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            Live Event Stream ({timeline.length})
          </button>
        </div>

        {metrics.lastHeartbeat && (
          <span className="text-[11px] font-mono text-slate-500 hidden sm:inline">
            Heartbeat: {new Date(metrics.lastHeartbeat).toLocaleTimeString()}
          </span>
        )}
      </div>

      {/* Tab Panels */}
      {activeTab === 'operations' ? (
        <LiveOperationsTable operations={hydratedOperations} isCompleted={isRunCompleted} />
      ) : (
        <LiveEventTimeline timeline={timeline} isCompleted={isRunCompleted} />
      )}
    </div>
  );
};
