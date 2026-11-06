import React, { useState } from 'react';
import type { OperationRunRecord } from '../../types/report';
import { HttpMethodBadge } from '../common/Badge';
import {
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Flame,
  Sliders,
  SearchCheck,
  ShieldAlert,
  Database,
} from 'lucide-react';

interface OperationDetailItemProps {
  operation: OperationRunRecord;
}

export const OperationDetailItem: React.FC<OperationDetailItemProps> = ({ operation }) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const [activeTab, setActiveTab] = useState<'execution' | 'investigation' | 'retests' | 'remediation'>('execution');

  const {
    http_method,
    path_template,
    operation_id,
    initial_attempt,
    final_status,
    investigation,
    transient_retries = [],
    variation_tests = [],
    followup_decisions = [],
    remediation_recommendations = [],
  } = operation;

  // Status pill styling
  const getStatusBadge = () => {
    switch (final_status) {
      case 'passed':
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-950/60 text-emerald-300 border border-emerald-800/60">
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
            Passed
          </span>
        );
      case 'resolved_after_transient_retry':
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-950/60 text-amber-300 border border-amber-800/60">
            <Flame className="w-3.5 h-3.5 text-amber-400" />
            Resolved after retry
          </span>
        );
      case 'failed':
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-rose-950/60 text-rose-300 border border-rose-800/60">
            <XCircle className="w-3.5 h-3.5 text-rose-400" />
            Failed
          </span>
        );
      case 'error':
      default:
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-purple-950/60 text-purple-300 border border-purple-800/60">
            <AlertTriangle className="w-3.5 h-3.5 text-purple-400" />
            Error
          </span>
        );
    }
  };

  const hasInvestigation = Boolean(investigation);
  const hasRemediation = remediation_recommendations.length > 0;

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 overflow-hidden transition-all hover:border-slate-700">
      {/* Header Bar */}
      <div
        onClick={() => setIsExpanded(!isExpanded)}
        className="p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 cursor-pointer select-none hover:bg-slate-850/50 transition-colors"
      >
        <div className="flex items-center gap-3 min-w-0">
          <button type="button" className="text-slate-500 hover:text-slate-300">
            {isExpanded ? (
              <ChevronDown className="w-4 h-4 text-indigo-400" />
            ) : (
              <ChevronRight className="w-4 h-4 text-slate-500" />
            )}
          </button>

          <HttpMethodBadge method={http_method} />

          <div className="min-w-0">
            <span className="font-mono text-sm font-semibold text-slate-200 truncate block">
              {path_template}
            </span>
            {operation_id && (
              <span className="font-mono text-[11px] text-slate-500 block truncate">
                {operation_id}
              </span>
            )}
          </div>
        </div>

        {/* Right Metric Tags */}
        <div className="flex flex-wrap items-center gap-2.5 shrink-0 pl-7 sm:pl-0">
          {initial_attempt.status_code !== undefined && initial_attempt.status_code !== null && (
            <span className="font-mono text-xs text-slate-400 bg-slate-950 px-2 py-0.5 rounded border border-slate-800">
              HTTP {initial_attempt.status_code}
            </span>
          )}

          {initial_attempt.latency_ms !== undefined && initial_attempt.latency_ms !== null && (
            <span className="font-mono text-xs text-slate-500">
              {initial_attempt.latency_ms.toFixed(1)}ms
            </span>
          )}

          {initial_attempt.failure_category && (
            <span className="font-mono text-[11px] px-2 py-0.5 rounded bg-purple-950/60 text-purple-300 border border-purple-800/60 flex items-center gap-1">
              <SearchCheck className="w-3 h-3 text-purple-400" />
              {initial_attempt.failure_category}
            </span>
          )}

          {getStatusBadge()}
        </div>
      </div>

      {/* Expanded Accordion Body */}
      {isExpanded && (
        <div className="border-t border-slate-800 bg-slate-950/80 p-5 space-y-5">
          {/* Internal Navigation Tabs */}
          <div className="flex flex-wrap items-center gap-2 border-b border-slate-800 pb-3">
            <button
              type="button"
              onClick={() => setActiveTab('execution')}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                activeTab === 'execution'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
              }`}
            >
              Execution Details
            </button>

            <button
              type="button"
              onClick={() => setActiveTab('investigation')}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 ${
                activeTab === 'investigation'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
              }`}
            >
              <SearchCheck className="w-3.5 h-3.5" />
              Failure Investigation & Evidence
              {hasInvestigation && (
                <span className="w-1.5 h-1.5 rounded-full bg-purple-400" />
              )}
            </button>

            <button
              type="button"
              onClick={() => setActiveTab('retests')}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 ${
                activeTab === 'retests'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
              }`}
            >
              <Sliders className="w-3.5 h-3.5" />
              Retests & Variations ({transient_retries.length + variation_tests.length})
            </button>

            {hasRemediation && (
              <button
                type="button"
                onClick={() => setActiveTab('remediation')}
                className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 ${
                  activeTab === 'remediation'
                    ? 'bg-indigo-600 text-white shadow-sm'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
                }`}
              >
                <AlertTriangle className="w-3.5 h-3.5 text-amber-400" />
                Remediation Guidance ({remediation_recommendations.length})
              </button>
            )}
          </div>

          {/* TAB 1: Execution Details */}
          {activeTab === 'execution' && (
            <div className="space-y-4 text-xs">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-slate-900/60 p-3.5 rounded-lg border border-slate-800">
                <div>
                  <span className="text-slate-500 block">Initial Status Code</span>
                  <span className="font-mono text-sm font-bold text-slate-200">
                    {initial_attempt.status_code ?? 'None (No response)'}
                  </span>
                </div>
                <div>
                  <span className="text-slate-500 block">Initial Latency</span>
                  <span className="font-mono text-sm font-bold text-slate-200">
                    {initial_attempt.latency_ms ? `${initial_attempt.latency_ms.toFixed(1)}ms` : '-'}
                  </span>
                </div>
                <div>
                  <span className="text-slate-500 block">Network Dispatched</span>
                  <span className="font-mono text-sm font-bold text-slate-200">
                    {initial_attempt.dispatched ? 'Yes (Live Wire)' : 'No (Local Block)'}
                  </span>
                </div>
                <div>
                  <span className="text-slate-500 block">Final Status</span>
                  <span className="font-mono text-sm font-bold text-slate-200 capitalize">
                    {final_status}
                  </span>
                </div>
              </div>

              {/* Failed Assertions */}
              {initial_attempt.failed_assertions && initial_attempt.failed_assertions.length > 0 && (
                <div className="p-3.5 rounded-lg bg-rose-950/40 border border-rose-800/60 space-y-2">
                  <span className="font-semibold text-rose-300 flex items-center gap-1.5">
                    <XCircle className="w-3.5 h-3.5 text-rose-400" />
                    Failed Assertions ({initial_attempt.failed_assertions.length})
                  </span>
                  <ul className="space-y-1 list-disc list-inside font-mono text-[11px] text-rose-200">
                    {initial_attempt.failed_assertions.map((assertion, aIdx) => (
                      <li key={aIdx}>{assertion}</li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Error Type */}
              {initial_attempt.error_type && (
                <div className="p-3 rounded-lg bg-slate-900 border border-slate-800 text-slate-300">
                  <span className="text-slate-500 block mb-0.5 font-medium">Error Classification:</span>
                  <span className="font-mono text-amber-300">{initial_attempt.error_type}</span>
                </div>
              )}

              {/* Request Fingerprint */}
              <div className="text-[11px] font-mono text-slate-500">
                <span>Fingerprint: </span>
                <span className="text-slate-400">{operation.initial_request_fingerprint}</span>
              </div>
            </div>
          )}

          {/* TAB 2: Failure Investigation & Evidence Inspector */}
          {activeTab === 'investigation' && (
            <div className="space-y-5 text-xs">
              {!investigation ? (
                <div className="p-6 text-center text-slate-500 rounded-lg border border-dashed border-slate-800">
                  <CheckCircle2 className="w-6 h-6 text-emerald-500/60 mx-auto mb-2" />
                  No failure investigation was required for this operation.
                </div>
              ) : (
                <>
                  {/* Degraded investigation warning if degraded */}
                  {investigation.investigation_degraded && (
                    <div className="p-3.5 rounded-lg bg-amber-950/40 border border-amber-800/60 text-amber-300 text-xs flex items-start gap-2.5">
                      <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
                      <div>
                        <span className="font-semibold">Investigation Degraded: </span>
                        <span>
                          {investigation.investigation_error_type ||
                            'Context or semantic retrieval encountered errors during root-cause inference.'}
                        </span>
                      </div>
                    </div>
                  )}

                  {/* Synthesized Inferences */}
                  {investigation.inferences && investigation.inferences.length > 0 && (
                    <div className="space-y-2">
                      <span className="font-semibold text-slate-300 flex items-center gap-1.5 uppercase tracking-wider text-[11px]">
                        <SearchCheck className="w-3.5 h-3.5 text-purple-400" />
                        Root-Cause Inferences & Synthesis
                      </span>
                      <div className="space-y-2 bg-slate-900/60 p-4 rounded-xl border border-slate-800">
                        {investigation.inferences.map((inference, iIdx) => (
                          <div key={iIdx} className="flex items-start gap-2 text-slate-200">
                            <span className="w-4 h-4 rounded-full bg-purple-950 text-purple-300 border border-purple-800 flex items-center justify-center font-mono text-[10px] shrink-0 mt-0.5">
                              {iIdx + 1}
                            </span>
                            <p className="leading-relaxed">{inference}</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Retrieved Evidence Chunks (Clearly marked as Untrusted RAG Context) */}
                  {investigation.retrieved_evidence && investigation.retrieved_evidence.length > 0 && (
                    <div className="space-y-3 pt-2">
                      <div className="flex items-center justify-between">
                        <span className="font-semibold text-slate-300 flex items-center gap-1.5 uppercase tracking-wider text-[11px]">
                          <Database className="w-3.5 h-3.5 text-indigo-400" />
                          Retrieved Evidence ({investigation.retrieved_evidence.length} chunks)
                        </span>
                        <span className="inline-flex items-center gap-1 text-[10px] font-mono px-2 py-0.5 rounded bg-amber-950/60 text-amber-300 border border-amber-800/50">
                          <ShieldAlert className="w-3 h-3 text-amber-400" />
                          Untrusted Context
                        </span>
                      </div>

                      <div className="space-y-2.5">
                        {investigation.retrieved_evidence.map((chunk, cIdx) => (
                          <div
                            key={cIdx}
                            className="bg-slate-900 border border-slate-800 rounded-lg p-3 space-y-2"
                          >
                            <div className="flex items-center justify-between text-[11px] text-slate-400 font-mono">
                              <span className="text-indigo-300">
                                Chunk: {chunk.chunk_id.slice(0, 12)}...
                              </span>
                              <div className="flex items-center gap-3">
                                <span>
                                  Relevance: {(chunk.relevance_score * 100).toFixed(1)}%
                                </span>
                                <span className="text-slate-500">
                                  Type: {chunk.source_type}
                                </span>
                              </div>
                            </div>
                            <p className="font-mono text-[11px] text-slate-300 bg-slate-950 p-2.5 rounded border border-slate-850 whitespace-pre-wrap leading-relaxed">
                              {chunk.content}
                            </p>
                            {chunk.source_ref && (
                              <span className="text-[10px] font-mono text-slate-500 block truncate">
                                Source: {chunk.source_ref}
                              </span>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* TAB 3: Retests & Variations */}
          {activeTab === 'retests' && (
            <div className="space-y-5 text-xs">
              {/* Transient Retries */}
              <div className="space-y-2">
                <span className="font-semibold text-slate-300 flex items-center gap-1.5 uppercase tracking-wider text-[11px]">
                  <Flame className="w-3.5 h-3.5 text-amber-400" />
                  Transient Retry Attempts ({transient_retries.length})
                </span>

                {transient_retries.length === 0 ? (
                  <p className="text-slate-500 italic p-3 bg-slate-900/40 rounded-lg border border-slate-800">
                    No transient retries executed for this operation.
                  </p>
                ) : (
                  <div className="divide-y divide-slate-800 bg-slate-900/60 rounded-lg border border-slate-800">
                    {transient_retries.map((tr, trIdx) => (
                      <div key={trIdx} className="p-3 flex items-center justify-between">
                        <div className="space-y-0.5">
                          <div className="flex items-center gap-2">
                            <span className="font-semibold text-slate-200">
                              Retry #{tr.attempt_number}
                            </span>
                            <span className="text-slate-500">
                              (Delay: {tr.backoff_delay_seconds}s)
                            </span>
                          </div>
                          <span className="text-[11px] text-slate-500 font-mono">
                            Retry of: {tr.retry_of}
                          </span>
                        </div>

                        <div className="flex items-center gap-3 font-mono">
                          {tr.outcome.status_code !== undefined && (
                            <span className="text-slate-300">
                              HTTP {tr.outcome.status_code}
                            </span>
                          )}
                          <span
                            className={
                              tr.outcome.passed ? 'text-emerald-400' : 'text-rose-400'
                            }
                          >
                            {tr.outcome.passed ? 'PASSED' : 'FAILED'}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Parameter Variation Tests */}
              <div className="space-y-2 pt-2">
                <span className="font-semibold text-slate-300 flex items-center gap-1.5 uppercase tracking-wider text-[11px]">
                  <Sliders className="w-3.5 h-3.5 text-cyan-400" />
                  Parameter Variation Tests ({variation_tests.length})
                </span>

                {variation_tests.length === 0 ? (
                  <p className="text-slate-500 italic p-3 bg-slate-900/40 rounded-lg border border-slate-800">
                    No variation tests were dispatched for this operation.
                  </p>
                ) : (
                  <div className="divide-y divide-slate-800 bg-slate-900/60 rounded-lg border border-slate-800">
                    {variation_tests.map((vt, vtIdx) => (
                      <div key={vtIdx} className="p-3 flex items-center justify-between">
                        <div className="space-y-0.5">
                          <span className="font-semibold text-slate-200">
                            Variation #{vt.variation_number} ({vt.candidate_id})
                          </span>
                          {vt.is_non_idempotent_mutation && (
                            <span className="text-amber-400 text-[10px] block">
                              [Authorized non-idempotent mutation]
                            </span>
                          )}
                        </div>

                        <div className="flex items-center gap-3 font-mono">
                          {vt.outcome.status_code !== undefined && (
                            <span className="text-slate-300">
                              HTTP {vt.outcome.status_code}
                            </span>
                          )}
                          <span
                            className={
                              vt.outcome.passed ? 'text-emerald-400' : 'text-rose-400'
                            }
                          >
                            {vt.outcome.passed ? 'PASSED' : 'FAILED'}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Follow-up Decisions Audit Trail */}
              {followup_decisions.length > 0 && (
                <div className="space-y-2 pt-2">
                  <span className="font-semibold text-slate-400 uppercase tracking-wider text-[11px] block">
                    Follow-up Decision Ledger ({followup_decisions.length})
                  </span>
                  <div className="space-y-1.5">
                    {followup_decisions.map((fd, fdIdx) => (
                      <div
                        key={fdIdx}
                        className="p-2.5 rounded bg-slate-900/80 border border-slate-800 font-mono text-[11px] text-slate-300 flex items-start justify-between gap-3"
                      >
                        <div>
                          <span className="text-indigo-400 font-semibold">{fd.action}</span>
                          <span className="text-slate-500"> ({fd.reason_category}): </span>
                          <span>{fd.sanitized_reason}</span>
                        </div>
                        <span className="text-slate-500 shrink-0">
                          {fd.dispatched ? 'Dispatched' : 'Skipped'}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* TAB 4: Remediation Recommendations */}
          {activeTab === 'remediation' && hasRemediation && (
            <div className="space-y-3 text-xs">
              {remediation_recommendations.map((rec, rIdx) => (
                <div
                  key={rIdx}
                  className="p-4 rounded-xl bg-slate-900 border border-slate-800 space-y-2"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-slate-200">
                      Target: {rec.target}
                    </span>
                    <span
                      className={`font-mono text-[10px] px-2 py-0.5 rounded uppercase font-semibold ${
                        rec.priority === 'critical'
                          ? 'bg-rose-950 text-rose-300 border border-rose-800'
                          : rec.priority === 'high'
                          ? 'bg-amber-950 text-amber-300 border border-amber-800'
                          : 'bg-blue-950 text-blue-300 border border-blue-800'
                      }`}
                    >
                      {rec.priority}
                    </span>
                  </div>
                  <p className="text-slate-300 leading-relaxed">{rec.finding}</p>
                  <div className="p-2.5 rounded bg-slate-950 border border-slate-800 text-indigo-300 font-mono text-[11px]">
                    <span className="font-bold text-slate-400">Action: </span>
                    {rec.action}
                  </div>
                  {rec.reference_urls && rec.reference_urls.length > 0 && (
                    <div className="space-y-1 pt-1">
                      <span className="text-[10px] text-slate-500 block uppercase">
                        References:
                      </span>
                      {rec.reference_urls.map((refUrl, uIdx) => (
                        <a
                          key={uIdx}
                          href={refUrl}
                          target="_blank"
                          rel="noreferrer"
                          className="text-[11px] font-mono text-indigo-400 hover:underline block truncate"
                        >
                          {refUrl}
                        </a>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
