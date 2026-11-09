import React from 'react';
import type { DiagnosticReportData } from '../../types/report';
import { Card, CardHeader, CardTitle, CardContent } from '../common/Card';
import {
  Sliders,
  Lightbulb,
} from 'lucide-react';

interface ReportSummaryHeaderProps {
  report: DiagnosticReportData;
}

export const ReportSummaryHeader: React.FC<ReportSummaryHeaderProps> = ({ report }) => {
  const { summary, global_remediation_summary } = report;
  const varMetrics = summary.variation_metrics;

  return (
    <div className="space-y-5">
      {/* Primary KPI Metrics */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3.5">
        <Card className="p-4">
          <span className="text-xs text-slate-500 uppercase font-semibold">Pass Rate</span>
          <div className="mt-1 flex items-baseline gap-1.5">
            <span
              className={`text-2xl font-bold font-mono ${
                summary.pass_rate_percentage >= 90
                  ? 'text-emerald-600'
                  : summary.pass_rate_percentage >= 70
                  ? 'text-amber-600'
                  : 'text-rose-600'
              }`}
            >
              {summary.pass_rate_percentage.toFixed(1)}%
            </span>
          </div>
          <span className="text-[11px] text-slate-500 mt-1 block">
            {summary.passed_operations + summary.resolved_by_transient_retry} of {summary.total_operations} operations
          </span>
        </Card>

        <Card className="p-4">
          <span className="text-xs text-slate-500 uppercase font-semibold">Duration</span>
          <div className="mt-1 text-2xl font-bold font-mono text-slate-900">
            {(summary.total_duration_ms / 1000).toFixed(2)}s
          </div>
          <span className="text-[11px] text-slate-500 mt-1 block">Total run duration</span>
        </Card>

        <Card className="p-4">
          <span className="text-xs text-slate-500 uppercase font-semibold">Network Requests</span>
          <div className="mt-1 text-2xl font-bold font-mono text-slate-900">
            {summary.total_requests_executed}
          </div>
          <span className="text-[11px] text-slate-500 mt-1 block">HTTP dispatches across wire</span>
        </Card>

        <Card className="p-4">
          <span className="text-xs text-slate-500 uppercase font-semibold">Transient Retries</span>
          <div className="mt-1 text-2xl font-bold font-mono text-amber-600">
            {summary.resolved_by_transient_retry}
          </div>
          <span className="text-[11px] text-slate-500 mt-1 block">
            Resolved of {summary.total_transient_retries_dispatched} retries
          </span>
        </Card>

        <Card className="p-4 col-span-2 md:col-span-1">
          <span className="text-xs text-slate-500 uppercase font-semibold">Defects / Errors</span>
          <div className="mt-1 text-2xl font-bold font-mono text-rose-600">
            {summary.failed_operations + summary.error_operations}
          </div>
          <span className="text-[11px] text-slate-500 mt-1 block">
            {summary.failed_operations} failed, {summary.error_operations} error
          </span>
        </Card>
      </div>

      {/* Variation Metrics Sub-panel (if variation tests were generated) */}
      {varMetrics && varMetrics.candidates_generated > 0 && (
        <Card className="bg-white">
          <CardHeader className="py-3 px-4 border-b border-slate-100 flex flex-row items-center justify-between">
            <div className="flex items-center gap-2">
              <Sliders className="w-4 h-4 text-sky-600" />
              <CardTitle className="text-sm font-semibold text-slate-900">
                Parameter Variation Robustness Matrix
              </CardTitle>
            </div>
            <span className="text-xs font-mono text-sky-700 bg-sky-50 px-2 py-0.5 rounded border border-sky-200">
              {varMetrics.variation_success_rate_percentage.toFixed(1)}% Mutation Success
            </span>
          </CardHeader>
          <CardContent className="p-4">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
              <div className="bg-slate-50 p-2.5 rounded-lg border border-slate-200">
                <span className="text-slate-500 text-[11px] block">Candidates Generated</span>
                <span className="text-base font-bold font-mono text-slate-900">
                  {varMetrics.candidates_generated}
                </span>
                <span className="text-[10px] text-slate-400 block mt-0.5">
                  {varMetrics.candidates_schema_valid} schema valid
                </span>
              </div>

              <div className="bg-slate-50 p-2.5 rounded-lg border border-slate-200">
                <span className="text-slate-500 text-[11px] block">Approved & Dispatched</span>
                <span className="text-base font-bold font-mono text-slate-900">
                  {varMetrics.candidates_dispatched}
                </span>
                <span className="text-[10px] text-slate-400 block mt-0.5">
                  of {varMetrics.candidates_approved} approved
                </span>
              </div>

              <div className="bg-slate-50 p-2.5 rounded-lg border border-slate-200">
                <span className="text-slate-500 text-[11px] block">Variation Tests Passed</span>
                <span className="text-base font-bold font-mono text-emerald-600">
                  {varMetrics.variation_tests_passed}
                </span>
                <span className="text-[10px] text-slate-400 block mt-0.5">
                  {varMetrics.variation_tests_failed} failed
                </span>
              </div>

              <div className="bg-slate-50 p-2.5 rounded-lg border border-slate-200">
                <span className="text-slate-500 text-[11px] block">Safety Gate Rejections</span>
                <span className="text-base font-bold font-mono text-slate-700">
                  {varMetrics.candidates_rejected}
                </span>
                <span className="text-[10px] text-slate-400 block mt-0.5">
                  {varMetrics.rejections_unauthorized} auth, {varMetrics.rejections_budget_exhausted} budget
                </span>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Global Remediation Summary */}
      {global_remediation_summary && global_remediation_summary.length > 0 && (
        <Card className="border-blue-200 bg-blue-50/40">
          <CardHeader className="py-3 px-4 border-b border-blue-100 flex flex-row items-center gap-2">
            <Lightbulb className="w-4 h-4 text-blue-600" />
            <CardTitle className="text-sm font-semibold text-blue-900">
              Global Remediation Synthesis ({global_remediation_summary.length} item{global_remediation_summary.length === 1 ? '' : 's'})
            </CardTitle>
          </CardHeader>
          <CardContent className="p-4">
            <ul className="space-y-2 list-disc list-inside text-xs sm:text-sm text-slate-700">
              {global_remediation_summary.map((item, idx) => (
                <li key={idx} className="leading-relaxed">
                  {item}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  );
};
