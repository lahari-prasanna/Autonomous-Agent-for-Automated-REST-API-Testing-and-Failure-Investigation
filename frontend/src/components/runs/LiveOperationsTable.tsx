import React, { useState } from 'react';
import type { LiveOperationProgress } from '../../types/events';
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../common/Card';
import { HttpMethodBadge } from '../common/Badge';
import {
  FileCode2,
  SearchCheck,
  Flame,
  Sliders,
  AlertCircle,
  CheckCircle2,
  Search,
  Activity,
} from 'lucide-react';

interface LiveOperationsTableProps {
  operations: Record<string, LiveOperationProgress>;
}

export const LiveOperationsTable: React.FC<LiveOperationsTableProps> = ({ operations }) => {
  const [searchTerm, setSearchTerm] = useState('');

  const opList = Object.values(operations);
  const filteredOps = opList.filter((op) => {
    if (!searchTerm.trim()) return true;
    const term = searchTerm.toLowerCase();
    return (
      op.path.toLowerCase().includes(term) ||
      op.method.toLowerCase().includes(term) ||
      op.operationId.toLowerCase().includes(term)
    );
  });

  return (
    <Card>
      <CardHeader className="pb-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <CardTitle className="flex items-center gap-2">
            <FileCode2 className="w-4 h-4 text-emerald-400" />
            Operations Progress
          </CardTitle>
          <CardDescription>
            {opList.length} operation{opList.length === 1 ? '' : 's'} tracked in this execution
          </CardDescription>
        </div>

        {/* Search */}
        <div className="relative w-full sm:w-60">
          <Search className="w-3.5 h-3.5 absolute left-2.5 top-2.5 text-slate-500" />
          <input
            type="text"
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            placeholder="Search operations..."
            className="w-full pl-8 pr-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
          />
        </div>
      </CardHeader>

      <CardContent className="p-0">
        {opList.length === 0 ? (
          <div className="p-8 text-center text-slate-500 text-xs">
            <Activity className="w-6 h-6 text-slate-600 mx-auto mb-2 animate-pulse" />
            Awaiting operation dispatches from the execution engine...
          </div>
        ) : filteredOps.length === 0 ? (
          <div className="p-6 text-center text-slate-500 text-xs">
            No operations match "{searchTerm}".
          </div>
        ) : (
          <div className="divide-y divide-slate-800 max-h-[460px] overflow-y-auto">
            {filteredOps.map((op) => {
              const isRunning = op.status === 'running';
              const isPassed = op.status === 'completed';
              const isFailed = op.status === 'failed';

              return (
                <div
                  key={op.operationId}
                  className="p-3.5 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 hover:bg-slate-850/40 transition-colors text-xs"
                >
                  <div className="space-y-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <HttpMethodBadge method={op.method} />
                      <span className="font-mono text-slate-200 font-medium truncate">
                        {op.path}
                      </span>
                    </div>

                    <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono text-slate-400">
                      {op.initialAttempt?.latencyMs !== undefined && (
                        <span>{op.initialAttempt.latencyMs?.toFixed(1)}ms</span>
                      )}
                      {op.initialAttempt?.statusCode !== undefined && (
                        <span className="text-slate-300">
                          HTTP {op.initialAttempt.statusCode}
                        </span>
                      )}
                      {op.initialAttempt?.errorType && (
                        <span className="text-rose-400 font-sans">
                          [{op.initialAttempt.errorType}]
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Badges for Investigation, Retests, Variations, and Final Status */}
                  <div className="flex flex-wrap items-center gap-2 shrink-0">
                    {/* Failure Investigation Tag */}
                    {op.investigation && (
                      <span
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-purple-950/70 border border-purple-800/60 text-purple-300 font-mono text-[10px]"
                        title={`Failure Category: ${op.investigation.failureCategory || 'Unknown'} | Inferences: ${op.investigation.inferencesCount}`}
                      >
                        <SearchCheck className="w-3 h-3 text-purple-400" />
                        {op.investigation.failureCategory || 'Investigated'}
                      </span>
                    )}

                    {/* Retest Attempts */}
                    {op.retests.length > 0 && (
                      <span
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-amber-950/70 border border-amber-800/60 text-amber-300 font-mono text-[10px]"
                        title={`${op.retests.length} transient retest attempt(s)`}
                      >
                        <Flame className="w-3 h-3 text-amber-400" />
                        Retests ({op.retests.length})
                      </span>
                    )}

                    {/* Variation Candidates */}
                    {op.variations.length > 0 && (
                      <span
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-cyan-950/70 border border-cyan-800/60 text-cyan-300 font-mono text-[10px]"
                        title={`${op.variations.length} parameter variation test(s)`}
                      >
                        <Sliders className="w-3 h-3 text-cyan-400" />
                        Variations ({op.variations.length})
                      </span>
                    )}

                    {/* Status Pill */}
                    {isRunning && (
                      <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-blue-950/60 text-blue-300 border border-blue-800/60">
                        <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-ping" />
                        Executing
                      </span>
                    )}

                    {isPassed && (
                      <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-emerald-950/60 text-emerald-300 border border-emerald-800/60">
                        <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                        Passed
                      </span>
                    )}

                    {isFailed && (
                      <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-rose-950/60 text-rose-300 border border-rose-800/60">
                        <AlertCircle className="w-3.5 h-3.5 text-rose-400" />
                        Failed
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
};
