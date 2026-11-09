import React, { useState } from 'react';
import type { TimelineEvent, SSEEventType } from '../../types/events';
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../common/Card';
import {
  Activity,
  ChevronDown,
  ChevronRight,
  Flame,
  SearchCheck,
  RefreshCw,
  Sliders,
  CheckCircle2,
  XCircle,
  HelpCircle,
  Clock,
} from 'lucide-react';

interface LiveEventTimelineProps {
  timeline: TimelineEvent[];
  isCompleted?: boolean;
}

function getEventBadgeStyle(type: SSEEventType): { bg: string; text: string; icon: React.ReactNode } {
  switch (type) {
    case 'run_created':
    case 'run_started':
      return {
        bg: 'bg-blue-50 border-blue-200',
        text: 'text-blue-700 font-medium',
        icon: <Activity className="w-3 h-3 text-blue-600" />,
      };

    case 'operation_started':
      return {
        bg: 'bg-sky-50 border-sky-200',
        text: 'text-sky-700 font-medium',
        icon: <RefreshCw className="w-3 h-3 text-sky-600 animate-spin" />,
      };

    case 'attempt_completed':
      return {
        bg: 'bg-slate-100 border-slate-200',
        text: 'text-slate-700 font-medium',
        icon: <Clock className="w-3 h-3 text-slate-500" />,
      };

    case 'investigation_completed':
      return {
        bg: 'bg-purple-50 border-purple-200',
        text: 'text-purple-700 font-medium',
        icon: <SearchCheck className="w-3 h-3 text-purple-600" />,
      };

    case 'retest_completed':
      return {
        bg: 'bg-amber-50 border-amber-200',
        text: 'text-amber-700 font-medium',
        icon: <Flame className="w-3 h-3 text-amber-600" />,
      };

    case 'variation_completed':
      return {
        bg: 'bg-indigo-50 border-indigo-200',
        text: 'text-indigo-700 font-medium',
        icon: <Sliders className="w-3 h-3 text-indigo-600" />,
      };

    case 'operation_completed':
      return {
        bg: 'bg-emerald-50 border-emerald-200',
        text: 'text-emerald-700 font-medium',
        icon: <CheckCircle2 className="w-3 h-3 text-emerald-600" />,
      };

    case 'run_completed':
      return {
        bg: 'bg-emerald-50 border-emerald-300',
        text: 'text-emerald-800 font-semibold',
        icon: <CheckCircle2 className="w-3 h-3 text-emerald-600" />,
      };

    case 'run_failed':
    case 'run_cancelled':
      return {
        bg: 'bg-rose-50 border-rose-200',
        text: 'text-rose-700 font-medium',
        icon: <XCircle className="w-3 h-3 text-rose-600" />,
      };

    case 'heartbeat':
    default:
      return {
        bg: 'bg-slate-50 border-slate-200',
        text: 'text-slate-500',
        icon: <HelpCircle className="w-3 h-3 text-slate-400" />,
      };
  }
}

export const LiveEventTimeline: React.FC<LiveEventTimelineProps> = ({
  timeline,
  isCompleted,
}) => {
  const [filterType, setFilterType] = useState<string>('all');
  const [expandedEvents, setExpandedEvents] = useState<Set<string>>(new Set());

  const toggleExpand = (id: string) => {
    setExpandedEvents((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const filteredTimeline = timeline.filter((ev) => {
    if (filterType === 'all') return ev.eventType !== 'heartbeat';
    if (filterType === 'all_with_hb') return true;
    if (filterType === 'investigations') return ev.eventType === 'investigation_completed';
    if (filterType === 'retests') {
      return ev.eventType === 'retest_completed' || ev.eventType === 'variation_completed';
    }
    if (filterType === 'operations') {
      return (
        ev.eventType === 'operation_started' ||
        ev.eventType === 'attempt_completed' ||
        ev.eventType === 'operation_completed'
      );
    }
    return true;
  });

  return (
    <Card>
      <CardHeader className="pb-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <CardTitle className="flex items-center gap-2">
            <Activity className="w-4 h-4 text-blue-600" />
            Live Event Stream
          </CardTitle>
          <CardDescription>
            Chronological Server-Sent Events dispatched by the execution orchestrator
          </CardDescription>
        </div>

        {/* Filter controls */}
        <div className="flex items-center gap-1.5 bg-slate-100 p-1 rounded-lg border border-slate-200 text-xs">
          <button
            type="button"
            onClick={() => setFilterType('all')}
            className={`px-2.5 py-1 rounded transition-colors ${
              filterType === 'all'
                ? 'bg-white text-blue-700 font-semibold shadow-sm'
                : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            All Events
          </button>
          <button
            type="button"
            onClick={() => setFilterType('operations')}
            className={`px-2.5 py-1 rounded transition-colors ${
              filterType === 'operations'
                ? 'bg-white text-blue-700 font-semibold shadow-sm'
                : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            Operations
          </button>
          <button
            type="button"
            onClick={() => setFilterType('investigations')}
            className={`px-2.5 py-1 rounded transition-colors ${
              filterType === 'investigations'
                ? 'bg-white text-blue-700 font-semibold shadow-sm'
                : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            Investigations
          </button>
          <button
            type="button"
            onClick={() => setFilterType('retests')}
            className={`px-2.5 py-1 rounded transition-colors ${
              filterType === 'retests'
                ? 'bg-white text-blue-700 font-semibold shadow-sm'
                : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            Retests & Variations
          </button>
        </div>
      </CardHeader>

      <CardContent className="p-0">
        {filteredTimeline.length === 0 ? (
          <div className="p-8 text-center text-slate-500 text-xs space-y-1">
            {filterType !== 'all' ? (
              <p>No events match the selected "{filterType}" filter.</p>
            ) : isCompleted ? (
              <>
                <p className="font-medium text-slate-700">Real-time event stream history is no longer active.</p>
                <p className="text-slate-500 text-[11px] max-w-md mx-auto mt-0.5">
                  The test run has finished and event streaming has ended. Full operation results, evidence, and remediations are preserved in the Tracked Operations tab and Diagnostic Report.
                </p>
              </>
            ) : (
              <p>No events recorded yet. Events will appear here in real-time as execution progresses.</p>
            )}
          </div>
        ) : (
          <div className="divide-y divide-slate-100 max-h-[460px] overflow-y-auto">
            {filteredTimeline.map((ev) => {
              const badge = getEventBadgeStyle(ev.eventType);
              const isExpanded = expandedEvents.has(ev.id);
              const opId =
                typeof ev.data.operation_id === 'string'
                  ? ev.data.operation_id
                  : undefined;

              return (
                <div key={ev.id} className="hover:bg-slate-50 transition-colors">
                  <div
                    onClick={() => toggleExpand(ev.id)}
                    className="p-3 flex items-center justify-between gap-3 text-xs cursor-pointer select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      {isExpanded ? (
                        <ChevronDown className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                      ) : (
                        <ChevronRight className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                      )}

                      <span
                        className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-mono border ${badge.bg} ${badge.text}`}
                      >
                        {badge.icon}
                        {ev.eventType}
                      </span>

                      {opId && (
                        <span className="font-mono text-slate-700 truncate max-w-[220px] sm:max-w-md">
                          {opId}
                        </span>
                      )}
                    </div>

                    <div className="flex items-center gap-3 shrink-0 text-slate-500 font-mono text-[11px]">
                      {ev.data.status_code !== undefined && (
                        <span className="text-slate-700">
                          HTTP {String(ev.data.status_code)}
                        </span>
                      )}
                      {ev.data.passed !== undefined && (
                        <span
                          className={ev.data.passed ? 'text-emerald-600 font-medium' : 'text-rose-600 font-medium'}
                        >
                          {ev.data.passed ? 'PASSED' : 'FAILED'}
                        </span>
                      )}
                      <span>{new Date(ev.timestamp).toLocaleTimeString()}</span>
                    </div>
                  </div>

                  {isExpanded && (
                    <div className="px-6 pb-3 pt-1 bg-slate-50/70 border-t border-slate-100">
                      <pre className="font-mono text-[11px] text-slate-800 p-3 rounded-lg bg-white border border-slate-200 overflow-x-auto shadow-sm">
                        {JSON.stringify(ev.data, null, 2)}
                      </pre>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
};
