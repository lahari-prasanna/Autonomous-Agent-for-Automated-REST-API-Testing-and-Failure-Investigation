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
}

function getEventBadgeStyle(type: SSEEventType): { bg: string; text: string; icon: React.ReactNode } {
  switch (type) {
    case 'run_created':
    case 'run_started':
      return {
        bg: 'bg-indigo-950/70 border-indigo-800/60',
        text: 'text-indigo-300',
        icon: <Activity className="w-3 h-3 text-indigo-400" />,
      };

    case 'operation_started':
      return {
        bg: 'bg-blue-950/70 border-blue-800/60',
        text: 'text-blue-300',
        icon: <RefreshCw className="w-3 h-3 text-blue-400 animate-spin" />,
      };

    case 'attempt_completed':
      return {
        bg: 'bg-slate-800 border-slate-700',
        text: 'text-slate-300',
        icon: <Clock className="w-3 h-3 text-slate-400" />,
      };

    case 'investigation_completed':
      return {
        bg: 'bg-purple-950/70 border-purple-800/60',
        text: 'text-purple-300',
        icon: <SearchCheck className="w-3 h-3 text-purple-400" />,
      };

    case 'retest_completed':
      return {
        bg: 'bg-amber-950/70 border-amber-800/60',
        text: 'text-amber-300',
        icon: <Flame className="w-3 h-3 text-amber-400" />,
      };

    case 'variation_completed':
      return {
        bg: 'bg-cyan-950/70 border-cyan-800/60',
        text: 'text-cyan-300',
        icon: <Sliders className="w-3 h-3 text-cyan-400" />,
      };

    case 'operation_completed':
      return {
        bg: 'bg-emerald-950/70 border-emerald-800/60',
        text: 'text-emerald-300',
        icon: <CheckCircle2 className="w-3 h-3 text-emerald-400" />,
      };

    case 'run_completed':
      return {
        bg: 'bg-emerald-950/80 border-emerald-700',
        text: 'text-emerald-200 font-semibold',
        icon: <CheckCircle2 className="w-3 h-3 text-emerald-400" />,
      };

    case 'run_failed':
    case 'run_cancelled':
      return {
        bg: 'bg-rose-950/80 border-rose-800',
        text: 'text-rose-300',
        icon: <XCircle className="w-3 h-3 text-rose-400" />,
      };

    case 'heartbeat':
    default:
      return {
        bg: 'bg-slate-900 border-slate-800',
        text: 'text-slate-500',
        icon: <HelpCircle className="w-3 h-3 text-slate-500" />,
      };
  }
}

export const LiveEventTimeline: React.FC<LiveEventTimelineProps> = ({ timeline }) => {
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
            <Activity className="w-4 h-4 text-indigo-400" />
            Live Event Stream
          </CardTitle>
          <CardDescription>
            Chronological Server-Sent Events dispatched by the execution orchestrator
          </CardDescription>
        </div>

        {/* Filter controls */}
        <div className="flex items-center gap-1.5 bg-slate-950 p-1 rounded-lg border border-slate-800 text-xs">
          <button
            type="button"
            onClick={() => setFilterType('all')}
            className={`px-2.5 py-1 rounded transition-colors ${
              filterType === 'all'
                ? 'bg-indigo-600 text-white font-medium'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            All Events
          </button>
          <button
            type="button"
            onClick={() => setFilterType('operations')}
            className={`px-2.5 py-1 rounded transition-colors ${
              filterType === 'operations'
                ? 'bg-indigo-600 text-white font-medium'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Operations
          </button>
          <button
            type="button"
            onClick={() => setFilterType('investigations')}
            className={`px-2.5 py-1 rounded transition-colors ${
              filterType === 'investigations'
                ? 'bg-indigo-600 text-white font-medium'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Investigations
          </button>
          <button
            type="button"
            onClick={() => setFilterType('retests')}
            className={`px-2.5 py-1 rounded transition-colors ${
              filterType === 'retests'
                ? 'bg-indigo-600 text-white font-medium'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Retests & Variations
          </button>
        </div>
      </CardHeader>

      <CardContent className="p-0">
        {filteredTimeline.length === 0 ? (
          <div className="p-8 text-center text-slate-500 text-xs">
            No events recorded yet. Events will appear here in real-time as execution progresses.
          </div>
        ) : (
          <div className="divide-y divide-slate-800 max-h-[460px] overflow-y-auto">
            {filteredTimeline.map((ev) => {
              const badge = getEventBadgeStyle(ev.eventType);
              const isExpanded = expandedEvents.has(ev.id);
              const opId =
                typeof ev.data.operation_id === 'string'
                  ? ev.data.operation_id
                  : undefined;

              return (
                <div key={ev.id} className="hover:bg-slate-850/40 transition-colors">
                  <div
                    onClick={() => toggleExpand(ev.id)}
                    className="p-3 flex items-center justify-between gap-3 text-xs cursor-pointer select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      {isExpanded ? (
                        <ChevronDown className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                      ) : (
                        <ChevronRight className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                      )}

                      <span
                        className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-mono border ${badge.bg} ${badge.text}`}
                      >
                        {badge.icon}
                        {ev.eventType}
                      </span>

                      {opId && (
                        <span className="font-mono text-slate-300 truncate max-w-[220px] sm:max-w-md">
                          {opId}
                        </span>
                      )}
                    </div>

                    <div className="flex items-center gap-3 shrink-0 text-slate-500 font-mono text-[11px]">
                      {ev.data.status_code !== undefined && (
                        <span className="text-slate-400">
                          HTTP {String(ev.data.status_code)}
                        </span>
                      )}
                      {ev.data.passed !== undefined && (
                        <span
                          className={ev.data.passed ? 'text-emerald-400' : 'text-rose-400'}
                        >
                          {ev.data.passed ? 'PASSED' : 'FAILED'}
                        </span>
                      )}
                      <span>{new Date(ev.timestamp).toLocaleTimeString()}</span>
                    </div>
                  </div>

                  {isExpanded && (
                    <div className="px-6 pb-3 pt-1 bg-slate-950/70 border-t border-slate-800/60">
                      <pre className="font-mono text-[11px] text-slate-300 p-3 rounded-lg bg-slate-900 border border-slate-800 overflow-x-auto">
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
