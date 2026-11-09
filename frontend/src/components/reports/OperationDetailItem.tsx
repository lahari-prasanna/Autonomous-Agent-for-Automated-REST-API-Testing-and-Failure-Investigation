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
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-50 text-emerald-700 border border-emerald-200">
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" />
            Passed
          </span>
        );
      case 'resolved_after_transient_retry':
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-50 text-amber-700 border border-amber-200">
            <Flame className="w-3.5 h-3.5 text-amber-600" />
            Resolved after retry
          </span>
        );
      case 'failed':
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-rose-50 text-rose-700 border border-rose-200">
            <XCircle className="w-3.5 h-3.5 text-rose-600" />
            Failed
          </span>
        );
      case 'error':
      default:
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium bg-purple-50 text-purple-700 border border-purple-200">
            <AlertTriangle className="w-3.5 h-3.5 text-purple-600" />
            Error
          </span>
        );
    }
  };

  const hasInvestigation = Boolean(investigation);
  const hasRemediation = remediation_recommendations.length > 0;

  return (
    <div className="rounded-xl border border-slate-200 bg-white overflow-hidden transition-all hover:border-slate-300 shadow-sm">
      {/* Header Bar */}
      <div
        onClick={() => setIsExpanded(!isExpanded)}
        className="p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 cursor-pointer select-none hover:bg-slate-50 transition-colors"
      >
        <div className="flex items-center gap-3 min-w-0">
          <button type="button" className="text-slate-400 hover:text-slate-600">
            {isExpanded ? (
              <ChevronDown className="w-4 h-4 text-blue-600" />
            ) : (
              <ChevronRight className="w-4 h-4 text-slate-400" />
            )}
          </button>

          <HttpMethodBadge method={http_method} />

          <div className="min-w-0">
            <span className="font-mono text-sm font-semibold text-slate-900 truncate block">
              {path_template}
            </span>
            {operation_id && (
              <span className="font-mono text-[11px] text-slate-400 block truncate">
                {operation_id}
              </span>
            )}
          </div>
        </div>

        {/* Right Metric Tags */}
        <div className="flex flex-wrap items-center gap-2.5 shrink-0 pl-7 sm:pl-0">
          {initial_attempt.status_code !== undefined && initial_attempt.status_code !== null && (
            <span className="font-mono text-xs text-slate-700 bg-slate-100 px-2 py-0.5 rounded border border-slate-200">
              HTTP {initial_attempt.status_code}
            </span>
          )}

          {initial_attempt.latency_ms !== undefined && initial_attempt.latency_ms !== null && (
            <span className="font-mono text-xs text-slate-500">
              {initial_attempt.latency_ms.toFixed(1)}ms
            </span>
          )}

          {initial_attempt.failure_category && (
            <span className="font-mono text-[11px] px-2 py-0.5 rounded bg-purple-50 text-purple-700 border border-purple-200 flex items-center gap-1">
              <SearchCheck className="w-3 h-3 text-purple-600" />
              {initial_attempt.failure_category}
            </span>
          )}

          {getStatusBadge()}
        </div>
      </div>

      {/* Expanded Accordion Body */}
      {isExpanded && (
        <div className="border-t border-slate-100 bg-slate-50/50 p-5 space-y-5">
          {/* Internal Navigation Tabs */}
          <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 pb-3">
            <button
              type="button"
              onClick={() => setActiveTab('execution')}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                activeTab === 'execution'
                  ? 'bg-white text-blue-700 font-semibold shadow-sm border border-slate-200'
                  : 'text-slate-600 hover:text-slate-900 hover:bg-slate-100'
              }`}
            >
              Execution Details
            </button>

            <button
              type="button"
              onClick={() => setActiveTab('investigation')}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 ${
                activeTab === 'investigation'
                  ? 'bg-white text-blue-700 font-semibold shadow-sm border border-slate-200'
                  : 'text-slate-600 hover:text-slate-900 hover:bg-slate-100'
              }`}
            >
              <SearchCheck className="w-3.5 h-3.5 text-purple-600" />
              Failure Investigation & Evidence
              {hasInvestigation && (
                <span className="w-1.5 h-1.5 rounded-full bg-purple-600" />
              )}
            </button>

            <button
              type="button"
              onClick={() => setActiveTab('retests')}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 ${
                activeTab === 'retests'
                  ? 'bg-white text-blue-700 font-semibold shadow-sm border border-slate-200'
                  : 'text-slate-600 hover:text-slate-900 hover:bg-slate-100'
              }`}
            >
              <Sliders className="w-3.5 h-3.5 text-sky-600" />
              Retests & Variations ({transient_retries.length + variation_tests.length})
            </button>

            {hasRemediation && (
              <button
                type="button"
                onClick={() => setActiveTab('remediation')}
                className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 ${
                  activeTab === 'remediation'
                    ? 'bg-white text-blue-700 font-semibold shadow-sm border border-slate-200'
                    : 'text-slate-600 hover:text-slate-900 hover:bg-slate-100'
                }`}
              >
                <AlertTriangle className="w-3.5 h-3.5 text-amber-600" />
                Remediation Guidance ({remediation_recommendations.length})
              </button>
            )}
          </div>

          {/* TAB 1: Execution Details */}
          {activeTab === 'execution' && (
            <div className="space-y-4 text-xs">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-white p-3.5 rounded-lg border border-slate-200 shadow-sm">
                <div>
                  <span className="text-slate-400 block font-medium">Initial Status Code</span>
                  <span className="font-mono text-sm font-bold text-slate-900">
                    {initial_attempt.status_code ?? 'None (No response)'}
                  </span>
                </div>
                <div>
                  <span className="text-slate-400 block font-medium">Initial Latency</span>
                  <span className="font-mono text-sm font-bold text-slate-900">
                    {initial_attempt.latency_ms ? `${initial_attempt.latency_ms.toFixed(1)}ms` : '-'}
                  </span>
                </div>
                <div>
                  <span className="text-slate-400 block font-medium">Network Dispatched</span>
                  <span className="font-mono text-sm font-bold text-slate-900">
                    {initial_attempt.dispatched ? 'Yes (Live Wire)' : 'No (Local Block)'}
                  </span>
                </div>
                <div>
                  <span className="text-slate-400 block font-medium">Final Status</span>
                  <span className="font-mono text-sm font-bold text-slate-900 capitalize">
                    {final_status}
                  </span>
                </div>
              </div>

              {/* Failed Assertions */}
              {initial_attempt.failed_assertions && initial_attempt.failed_assertions.length > 0 && (
                <div className="p-3.5 rounded-lg bg-rose-50 border border-rose-200 space-y-2">
                  <span className="font-semibold text-rose-800 flex items-center gap-1.5">
                    <XCircle className="w-3.5 h-3.5 text-rose-600" />
                    Failed Assertions ({initial_attempt.failed_assertions.length})
                  </span>
                  <ul className="space-y-1 list-disc list-inside font-mono text-[11px] text-rose-700">
                    {initial_attempt.failed_assertions.map((assertion, aIdx) => (
                      <li key={aIdx}>{assertion}</li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Error Type */}
              {initial_attempt.error_type && (
                <div className="p-3 rounded-lg bg-white border border-slate-200 text-slate-800 shadow-sm">
                  <span className="text-slate-500 block mb-0.5 font-medium">Error Classification:</span>
                  <span className="font-mono text-amber-700 font-semibold">{initial_attempt.error_type}</span>
                </div>
              )}

              {/* Request Fingerprint */}
              <div className="text-[11px] font-mono text-slate-500">
                <span>Fingerprint: </span>
                <span className="text-slate-600">{operation.initial_request_fingerprint}</span>
              </div>
            </div>
          )}

          {/* TAB 2: Failure Investigation & Evidence Inspector */}
          {activeTab === 'investigation' && (
            <div className="space-y-5 text-xs">
              {!investigation ? (
                <div className="p-6 text-center text-slate-500 rounded-lg border border-dashed border-slate-300 bg-white">
                  <CheckCircle2 className="w-6 h-6 text-emerald-600 mx-auto mb-2" />
                  No failure investigation was required for this operation.
                </div>
              ) : (
                <>
                  {/* Degraded investigation warning if degraded */}
                  {investigation.investigation_degraded && (
                    <div className="p-3.5 rounded-lg bg-amber-50 border border-amber-200 text-amber-800 text-xs flex items-start gap-2.5">
                      <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
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
                      <span className="font-semibold text-slate-700 flex items-center gap-1.5 uppercase tracking-wider text-[11px]">
                        <SearchCheck className="w-3.5 h-3.5 text-purple-600" />
                        Root-Cause Inferences & Synthesis
                      </span>
                      <div className="space-y-2 bg-white p-4 rounded-xl border border-slate-200 shadow-sm">
                        {investigation.inferences.map((inference, iIdx) => (
                          <div key={iIdx} className="flex items-start gap-2 text-slate-800">
                            <span className="w-4 h-4 rounded-full bg-purple-50 text-purple-700 border border-purple-200 flex items-center justify-center font-mono text-[10px] shrink-0 mt-0.5">
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
                        <span className="font-semibold text-slate-700 flex items-center gap-1.5 uppercase tracking-wider text-[11px]">
                          <Database className="w-3.5 h-3.5 text-blue-600" />
                          Retrieved Evidence ({investigation.retrieved_evidence.length} chunks)
                        </span>
                        <span className="inline-flex items-center gap-1 text-[10px] font-mono px-2 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200">
                          <ShieldAlert className="w-3 h-3 text-amber-600" />
                          Untrusted Context
                        </span>
                      </div>

                      <div className="space-y-2.5">
                        {investigation.retrieved_evidence.map((chunk, cIdx) => (
                          <div
                            key={cIdx}
                            className="bg-white border border-slate-200 rounded-lg p-3 space-y-2 shadow-sm"
                          >
                            <div className="flex items-center justify-between text-[11px] text-slate-500 font-mono">
                              <span className="text-blue-700 font-semibold">
                                Chunk: {chunk.chunk_id.slice(0, 12)}...
                              </span>
                              <div className="flex items-center gap-3">
                                <span>
                                  Relevance: {(chunk.relevance_score * 100).toFixed(1)}%
                                </span>
                                <span className="text-slate-400">
                                  Type: {chunk.source_type}
                                </span>
                              </div>
                            </div>
                            <p className="font-mono text-[11px] text-slate-800 bg-slate-50 p-2.5 rounded border border-slate-200 whitespace-pre-wrap leading-relaxed">
                              {chunk.content}
                            </p>
                            {chunk.source_ref && (
                              <span className="text-[10px] font-mono text-slate-400 block truncate">
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
                <span className="font-semibold text-slate-700 flex items-center gap-1.5 uppercase tracking-wider text-[11px]">
                  <Flame className="w-3.5 h-3.5 text-amber-600" />
                  Transient Retry Attempts ({transient_retries.length})
                </span>

                {transient_retries.length === 0 ? (
                  <p className="text-slate-500 italic p-3 bg-white rounded-lg border border-slate-200 shadow-sm">
                    No transient retries executed for this operation.
                  </p>
                ) : (
                  <div className="divide-y divide-slate-100 bg-white rounded-lg border border-slate-200 shadow-sm">
                    {transient_retries.map((tr, trIdx) => (
                      <div key={trIdx} className="p-3 flex items-center justify-between">
                        <div className="space-y-0.5">
                          <div className="flex items-center gap-2">
                            <span className="font-semibold text-slate-900">
                              Retry #{tr.attempt_number}
                            </span>
                            <span className="text-slate-400">
                              (Delay: {tr.backoff_delay_seconds}s)
                            </span>
                          </div>
                          <span className="text-[11px] text-slate-400 font-mono">
                            Retry of: {tr.retry_of}
                          </span>
                        </div>

                        <div className="flex items-center gap-3 font-mono">
                          {tr.outcome.status_code !== undefined && (
                            <span className="text-slate-700">
                              HTTP {tr.outcome.status_code}
                            </span>
                          )}
                          <span
                            className={
                              tr.outcome.passed ? 'text-emerald-600 font-medium' : 'text-rose-600 font-medium'
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
                <span className="font-semibold text-slate-700 flex items-center gap-1.5 uppercase tracking-wider text-[11px]">
                  <Sliders className="w-3.5 h-3.5 text-sky-600" />
                  Parameter Variation Tests ({variation_tests.length})
                </span>

                {variation_tests.length === 0 ? (
                  <p className="text-slate-500 italic p-3 bg-white rounded-lg border border-slate-200 shadow-sm">
                    No variation tests were dispatched for this operation.
                  </p>
                ) : (
                  <div className="divide-y divide-slate-100 bg-white rounded-lg border border-slate-200 shadow-sm">
                    {variation_tests.map((vt, vtIdx) => (
                      <div key={vtIdx} className="p-3 flex items-center justify-between">
                        <div className="space-y-0.5">
                          <span className="font-semibold text-slate-900">
                            Variation #{vt.variation_number} ({vt.candidate_id})
                          </span>
                          {vt.is_non_idempotent_mutation && (
                            <span className="text-amber-600 text-[10px] block">
                              [Authorized non-idempotent mutation]
                            </span>
                          )}
                        </div>

                        <div className="flex items-center gap-3 font-mono">
                          {vt.outcome.status_code !== undefined && (
                            <span className="text-slate-700">
                              HTTP {vt.outcome.status_code}
                            </span>
                          )}
                          <span
                            className={
                              vt.outcome.passed ? 'text-emerald-600 font-medium' : 'text-rose-600 font-medium'
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
                  <span className="font-semibold text-slate-500 uppercase tracking-wider text-[11px] block">
                    Follow-up Decision Ledger ({followup_decisions.length})
                  </span>
                  <div className="space-y-1.5">
                    {followup_decisions.map((fd, fdIdx) => (
                      <div
                        key={fdIdx}
                        className="p-2.5 rounded bg-white border border-slate-200 font-mono text-[11px] text-slate-700 flex items-start justify-between gap-3 shadow-sm"
                      >
                        <div>
                          <span className="text-blue-700 font-semibold">{fd.action}</span>
                          <span className="text-slate-400"> ({fd.reason_category}): </span>
                          <span>{fd.sanitized_reason}</span>
                        </div>
                        <span className="text-slate-400 shrink-0">
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
                  className="p-4 rounded-xl bg-white border border-slate-200 space-y-2 shadow-sm"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-slate-900">
                      Target: {rec.target}
                    </span>
                    <span
                      className={`font-mono text-[10px] px-2 py-0.5 rounded uppercase font-semibold ${
                        rec.priority === 'critical'
                          ? 'bg-rose-50 text-rose-700 border border-rose-200'
                          : rec.priority === 'high'
                          ? 'bg-amber-50 text-amber-700 border border-amber-200'
                          : 'bg-blue-50 text-blue-700 border border-blue-200'
                      }`}
                    >
                      {rec.priority}
                    </span>
                  </div>
                  <p className="text-slate-700 leading-relaxed">{rec.finding}</p>
                  <div className="p-2.5 rounded bg-slate-50 border border-slate-200 text-blue-700 font-mono text-[11px]">
                    <span className="font-bold text-slate-500">Action: </span>
                    {rec.action}
                  </div>
                  {rec.reference_urls && rec.reference_urls.length > 0 && (
                    <div className="space-y-1 pt-1">
                      <span className="text-[10px] text-slate-400 block uppercase font-medium">
                        References:
                      </span>
                      {rec.reference_urls.map((refUrl, uIdx) => (
                        <a
                          key={uIdx}
                          href={refUrl}
                          target="_blank"
                          rel="noreferrer"
                          className="text-[11px] font-mono text-blue-600 hover:underline block truncate"
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
