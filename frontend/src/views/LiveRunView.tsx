import React, { useState, useEffect } from 'react';
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
    },
  });

  const liveStatus = metrics.liveStatus || run?.status;
  const isExecuting = liveStatus === 'running' || liveStatus === 'pending';

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
        <div className="p-8 rounded-xl bg-rose-950/30 border border-rose-800/60 text-center space-y-3">
          <p className="text-sm font-medium text-rose-300">
            {error instanceof Error ? error.message : 'Run not found.'}
          </p>
          <Button variant="secondary" size="sm" onClick={() => refetch()}>
            Retry
          </Button>
        </div>
      </div>
    );
  }

  // Determine display values for metrics
  const displayDuration =
    run.duration_ms !== null && run.duration_ms !== undefined
      ? `${(run.duration_ms / 1000).toFixed(2)}s`
      : metrics.durationMs !== null
      ? `${(metrics.durationMs / 1000).toFixed(2)}s`
      : elapsedSeconds !== null
      ? `${elapsedSeconds}s`
      : '-';

  const totalOpsDisplay =
    metrics.totalOperations ??
    run.summary?.total_operations ??
    (run.target_endpoints ? run.target_endpoints.length : null) ??
    '-';

  const passedOpsDisplay =
    metrics.passedOperations || (run.summary?.passed as number | undefined) || 0;

  const failedOpsDisplay =
    metrics.failedOperations || (run.summary?.failed as number | undefined) || 0;

  const investigationsDisplay =
    metrics.investigationsCount ||
    (run.summary?.failure_investigations as number | undefined) ||
    0;

  const retestsDisplay =
    metrics.retestsCount || (run.summary?.retests as number | undefined) || 0;

  const variationsDisplay =
    metrics.variationsCount ||
    (run.summary?.variation_tests as number | undefined) ||
    0;

  return (
    <div className="space-y-6">
      {/* Header & Controls */}
      <div>
        <Link to={`/projects/${run.project_id}`} className="inline-block mb-3">
          <Button variant="ghost" size="sm" leftIcon={<ArrowLeft className="w-4 h-4" />}>
            Back to Project
          </Button>
        </Link>

        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 pb-4 border-b border-slate-800">
          <div>
            <div className="flex items-center gap-3">
              <h2 className="text-2xl font-bold text-slate-100">Live Test Run</h2>
              <StatusBadge status={liveStatus || 'pending'} />
            </div>
            <p className="text-xs sm:text-sm font-mono text-slate-400 mt-1">
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
        <Card className="p-4">
          <div className="text-xs text-slate-400 uppercase font-semibold">Status</div>
          <div className="mt-2 text-base font-bold text-slate-200 capitalize flex items-center gap-2">
            <span>{liveStatus}</span>
            {isExecuting && <span className="w-2 h-2 rounded-full bg-blue-400 animate-ping" />}
          </div>
        </Card>

        <Card className="p-4">
          <div className="text-xs text-slate-400 uppercase font-semibold">Duration</div>
          <div className="mt-2 text-base font-mono font-bold text-slate-200">
            {displayDuration}
          </div>
        </Card>

        <Card className="p-4">
          <div className="text-xs text-slate-400 uppercase font-semibold">Operations</div>
          <div className="mt-2 text-base font-mono font-bold text-slate-200">
            {metrics.completedOperations} / {totalOpsDisplay}
          </div>
        </Card>

        <Card className="p-4">
          <div className="text-xs text-slate-400 uppercase font-semibold">Workers Concurrency</div>
          <div className="mt-2 text-base font-mono font-bold text-slate-200">
            {run.concurrency} worker{run.concurrency === 1 ? '' : 's'}
          </div>
        </Card>
      </div>

      {/* Secondary Progress Counters */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
        <div className="p-3 rounded-xl bg-slate-900 border border-slate-800 flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-emerald-950/60 border border-emerald-800/40 flex items-center justify-center text-emerald-400 shrink-0">
            <CheckCircle2 className="w-4 h-4" />
          </div>
          <div>
            <span className="text-[10px] text-slate-400 uppercase font-semibold block">Passed</span>
            <span className="text-sm font-bold font-mono text-emerald-400">{passedOpsDisplay}</span>
          </div>
        </div>

        <div className="p-3 rounded-xl bg-slate-900 border border-slate-800 flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-rose-950/60 border border-rose-800/40 flex items-center justify-center text-rose-400 shrink-0">
            <XCircle className="w-4 h-4" />
          </div>
          <div>
            <span className="text-[10px] text-slate-400 uppercase font-semibold block">Failed</span>
            <span className="text-sm font-bold font-mono text-rose-400">{failedOpsDisplay}</span>
          </div>
        </div>

        <div className="p-3 rounded-xl bg-slate-900 border border-slate-800 flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-purple-950/60 border border-purple-800/40 flex items-center justify-center text-purple-400 shrink-0">
            <SearchCheck className="w-4 h-4" />
          </div>
          <div>
            <span className="text-[10px] text-slate-400 uppercase font-semibold block">Investigations</span>
            <span className="text-sm font-bold font-mono text-purple-400">{investigationsDisplay}</span>
          </div>
        </div>

        <div className="p-3 rounded-xl bg-slate-900 border border-slate-800 flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-amber-950/60 border border-amber-800/40 flex items-center justify-center text-amber-400 shrink-0">
            <Flame className="w-4 h-4" />
          </div>
          <div>
            <span className="text-[10px] text-slate-400 uppercase font-semibold block">Retests</span>
            <span className="text-sm font-bold font-mono text-amber-400">{retestsDisplay}</span>
          </div>
        </div>

        <div className="p-3 rounded-xl bg-slate-900 border border-slate-800 flex items-center gap-3 col-span-2 sm:col-span-1">
          <div className="w-8 h-8 rounded-lg bg-cyan-950/60 border border-cyan-800/40 flex items-center justify-center text-cyan-400 shrink-0">
            <Sliders className="w-4 h-4" />
          </div>
          <div>
            <span className="text-[10px] text-slate-400 uppercase font-semibold block">Variations</span>
            <span className="text-sm font-bold font-mono text-cyan-400">{variationsDisplay}</span>
          </div>
        </div>
      </div>

      {/* Error Message if run failed */}
      {run.error_message && (
        <div className="p-4 rounded-xl bg-rose-950/40 border border-rose-800/80 text-rose-300 text-xs flex items-start gap-3">
          <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0 mt-0.5" />
          <div>
            <span className="font-bold">Execution Terminated with Error: </span>
            <p className="mt-0.5 font-mono">{run.error_message}</p>
          </div>
        </div>
      )}

      {/* View Switcher: Operations vs Live Event Timeline */}
      <div className="flex items-center justify-between border-b border-slate-800 pb-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setActiveTab('operations')}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
              activeTab === 'operations'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
            }`}
          >
            Tracked Operations ({Object.keys(operations).length})
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('events')}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
              activeTab === 'events'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
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
        <LiveOperationsTable operations={operations} />
      ) : (
        <LiveEventTimeline timeline={timeline} />
      )}
    </div>
  );
};
