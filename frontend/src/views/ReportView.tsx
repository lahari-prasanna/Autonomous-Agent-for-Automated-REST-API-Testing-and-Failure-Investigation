import React, { useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api, ApiClientError } from '../services/api';
import type { OperationRunRecord } from '../types/report';
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { PageLoader } from '../components/common/Loading';
import { ReportSummaryHeader } from '../components/reports/ReportSummaryHeader';
import { OperationDetailItem } from '../components/reports/OperationDetailItem';
import {
  ArrowLeft,
  Download,
  Copy,
  Check,
  ShieldCheck,
  Clock,
  AlertTriangle,
  Search,
} from 'lucide-react';

export const ReportView: React.FC = () => {
  const { id } = useParams<{ id: string }>();

  const [copied, setCopied] = useState(false);
  const [activeTab, setActiveTab] = useState<'structured' | 'markdown'>('structured');
  const [searchTerm, setSearchTerm] = useState('');
  const [outcomeFilter, setOutcomeFilter] = useState<string>('all');
  const [categoryFilter, setCategoryFilter] = useState<string>('all');
  const [exportError, setExportError] = useState<string | null>(null);

  const {
    data: report,
    isLoading: isReportLoading,
    isError: isReportError,
    error: reportError,
    refetch,
  } = useQuery({
    queryKey: ['report', id],
    queryFn: () => (id ? api.getReport(id) : Promise.reject('No run ID')),
    enabled: Boolean(id),
    retry: false, // Don't retry 409 conflict
  });

  const {
    data: markdown,
    isLoading: isMarkdownLoading,
  } = useQuery({
    queryKey: ['report-markdown', id],
    queryFn: () => (id ? api.getReportMarkdown(id) : Promise.reject('No run ID')),
    enabled: Boolean(id) && activeTab === 'markdown',
  });

  const handleCopyMarkdown = async () => {
    try {
      setExportError(null);
      let textToCopy = markdown || report?.markdown_rendered;
      if (!textToCopy && id) {
        textToCopy = await api.getReportMarkdown(id);
      }
      if (textToCopy) {
        await navigator.clipboard.writeText(textToCopy);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      } else {
        setExportError('Markdown content not available to copy.');
      }
    } catch (err) {
      setExportError(err instanceof Error ? err.message : 'Failed to copy markdown.');
    }
  };

  const handleDownloadMarkdown = async () => {
    try {
      setExportError(null);
      let textToDownload = markdown || report?.markdown_rendered;
      if (!textToDownload && id) {
        textToDownload = await api.getReportMarkdown(id);
      }
      if (!textToDownload) {
        setExportError('Markdown report content is unavailable.');
        return;
      }
      const blob = new Blob([textToDownload], { type: 'text/markdown;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', `diagnostic-report-${id}.md`);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : 'Failed to download markdown report.');
    }
  };

  const handleDownloadJSON = () => {
    if (!report) return;
    try {
      setExportError(null);
      const blob = new Blob([JSON.stringify(report, null, 2)], {
        type: 'application/json;charset=utf-8;',
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.setAttribute('download', `diagnostic-report-${id}.json`);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : 'Failed to download JSON report.');
    }
  };

  if (isReportLoading) {
    return <PageLoader message="Loading validated diagnostic report..." />;
  }

  // Handle 409 Conflict (Run still executing)
  const isConflict409 =
    reportError instanceof ApiClientError && reportError.status === 409;

  if (isConflict409) {
    return (
      <div className="space-y-4">
        <Link to={id ? `/runs/${id}` : '/projects'}>
          <Button variant="ghost" size="sm" leftIcon={<ArrowLeft className="w-4 h-4" />}>
            Back to Live Run
          </Button>
        </Link>
        <div className="p-10 rounded-2xl bg-amber-950/25 border border-amber-800/60 text-center space-y-4 max-w-xl mx-auto my-8">
          <Clock className="w-12 h-12 text-amber-400 mx-auto" />
          <h3 className="text-xl font-bold text-slate-100">
            Diagnostic Report Not Ready
          </h3>
          <p className="text-sm text-slate-300 leading-relaxed">
            The test execution run is currently in progress. Diagnostic reports and synthesized
            remediations are compiled immediately upon run completion.
          </p>
          <div className="flex justify-center items-center gap-3 pt-3">
            <Link to={`/runs/${id}`}>
              <Button variant="primary" size="md">
                Monitor Live Run
              </Button>
            </Link>
            <Button variant="secondary" size="md" onClick={() => refetch()}>
              Refresh
            </Button>
          </div>
        </div>
      </div>
    );
  }

  if (isReportError || !report) {
    return (
      <div className="space-y-4">
        <Link to={id ? `/runs/${id}` : '/projects'}>
          <Button variant="ghost" size="sm" leftIcon={<ArrowLeft className="w-4 h-4" />}>
            Back to Run
          </Button>
        </Link>
        <div className="p-8 rounded-xl bg-rose-950/30 border border-rose-800/60 text-center space-y-3">
          <p className="text-sm font-medium text-rose-300">
            {reportError instanceof Error
              ? reportError.message
              : 'Diagnostic report not found.'}
          </p>
          <Button variant="secondary" size="sm" onClick={() => refetch()}>
            Retry
          </Button>
        </div>
      </div>
    );
  }

  const operations: OperationRunRecord[] = report.operations || [];

  // Extract unique failure categories for dropdown filter
  const failureCategories = Array.from(
    new Set(
      operations
        .map((op) => op.initial_attempt?.failure_category)
        .filter((cat): cat is string => Boolean(cat))
    )
  );

  // Filter operations based on user selection
  const filteredOperations = operations.filter((op) => {
    // Search text matching
    if (searchTerm.trim()) {
      const term = searchTerm.toLowerCase();
      const matchesSearch =
        op.path_template.toLowerCase().includes(term) ||
        op.http_method.toLowerCase().includes(term) ||
        (op.operation_id && op.operation_id.toLowerCase().includes(term));
      if (!matchesSearch) return false;
    }

    // Outcome filter
    if (outcomeFilter !== 'all') {
      if (outcomeFilter === 'passed' && op.final_status !== 'passed') return false;
      if (
        outcomeFilter === 'resolved_after_transient_retry' &&
        op.final_status !== 'resolved_after_transient_retry'
      )
        return false;
      if (outcomeFilter === 'failed' && op.final_status !== 'failed') return false;
      if (outcomeFilter === 'error' && op.final_status !== 'error') return false;
    }

    // Category filter
    if (categoryFilter !== 'all') {
      if (op.initial_attempt?.failure_category !== categoryFilter) return false;
    }

    return true;
  });

  return (
    <div className="space-y-6">
      {/* Top Header & Export Controls */}
      <div>
        <Link to={`/runs/${id}`} className="inline-block mb-3">
          <Button variant="ghost" size="sm" leftIcon={<ArrowLeft className="w-4 h-4" />}>
            Back to Live Run
          </Button>
        </Link>

        {exportError && (
          <div className="mb-4 p-3 rounded-lg bg-rose-950/40 border border-rose-800 text-rose-300 text-xs flex items-center justify-between">
            <div className="flex items-center gap-2">
              <AlertTriangle className="w-4 h-4 shrink-0 text-rose-400" />
              <span>{exportError}</span>
            </div>
            <button
              onClick={() => setExportError(null)}
              className="text-slate-400 hover:text-slate-200"
            >
              Dismiss
            </button>
          </div>
        )}

        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 pb-4 border-b border-slate-800">
          <div>
            <div className="flex items-center gap-3">
              <h2 className="text-2xl font-bold text-slate-100">Diagnostic Report</h2>
              <span className="font-mono text-xs px-2 py-0.5 rounded bg-emerald-950/60 text-emerald-300 border border-emerald-800/50 flex items-center gap-1">
                <ShieldCheck className="w-3.5 h-3.5" />
                Sanitized
              </span>
            </div>
            <p className="text-sm text-slate-400 mt-1">
              API: <span className="text-slate-200 font-medium">{report.api_title}</span> (v{report.api_version})
              {report.base_url && (
                <span className="ml-2 text-slate-500 font-mono text-xs">
                  • {report.base_url}
                </span>
              )}
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {/* View Mode Toggle */}
            <div className="bg-slate-900 border border-slate-800 rounded-lg p-0.5 flex">
              <button
                type="button"
                onClick={() => setActiveTab('structured')}
                className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                  activeTab === 'structured'
                    ? 'bg-indigo-600 text-white shadow-sm'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                Interactive
              </button>
              <button
                type="button"
                onClick={() => setActiveTab('markdown')}
                className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                  activeTab === 'markdown'
                    ? 'bg-indigo-600 text-white shadow-sm'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                Markdown Preview
              </button>
            </div>

            {/* Markdown Actions */}
            <Button
              variant="secondary"
              size="sm"
              leftIcon={copied ? <Check className="w-4 h-4 text-emerald-400" /> : <Copy className="w-4 h-4" />}
              onClick={handleCopyMarkdown}
            >
              {copied ? 'Copied' : 'Copy MD'}
            </Button>

            <Button
              variant="secondary"
              size="sm"
              leftIcon={<Download className="w-4 h-4" />}
              onClick={handleDownloadMarkdown}
              title="Download sanitized Markdown report file"
            >
              Markdown
            </Button>

            {/* JSON Export */}
            <Button
              variant="secondary"
              size="sm"
              leftIcon={<Download className="w-4 h-4" />}
              onClick={handleDownloadJSON}
              title="Download structured JSON report snapshot"
            >
              JSON
            </Button>
          </div>
        </div>
      </div>

      {activeTab === 'structured' ? (
        <div className="space-y-6">
          {/* Summary KPIs & Global Remediations */}
          <ReportSummaryHeader report={report} />

          {/* Operations Section Header & Search / Filters */}
          <div className="space-y-3 pt-3">
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
              <div>
                <h3 className="text-lg font-bold text-slate-100">
                  Tested Operations ({filteredOperations.length} of {operations.length})
                </h3>
                <p className="text-xs text-slate-400">
                  Inspect execution outcomes, failure classifications, and investigation evidence
                </p>
              </div>

              {/* Search Bar */}
              <div className="relative w-full sm:w-64">
                <Search className="w-3.5 h-3.5 absolute left-2.5 top-2.5 text-slate-500" />
                <input
                  type="text"
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  placeholder="Filter by path or method..."
                  className="w-full pl-8 pr-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
                />
              </div>
            </div>

            {/* Filters Row */}
            <div className="flex flex-wrap items-center gap-2 text-xs">
              {/* Outcome filter buttons */}
              <div className="flex flex-wrap items-center gap-1 bg-slate-900/80 p-1 rounded-lg border border-slate-800">
                <button
                  type="button"
                  onClick={() => setOutcomeFilter('all')}
                  className={`px-2.5 py-1 rounded transition-colors ${
                    outcomeFilter === 'all'
                      ? 'bg-indigo-600 text-white font-medium'
                      : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  All ({operations.length})
                </button>
                <button
                  type="button"
                  onClick={() => setOutcomeFilter('passed')}
                  className={`px-2.5 py-1 rounded transition-colors ${
                    outcomeFilter === 'passed'
                      ? 'bg-emerald-600 text-white font-medium'
                      : 'text-slate-400 hover:text-emerald-400'
                  }`}
                >
                  Passed ({report.summary.passed_operations})
                </button>
                {report.summary.resolved_by_transient_retry > 0 && (
                  <button
                    type="button"
                    onClick={() => setOutcomeFilter('resolved_after_transient_retry')}
                    className={`px-2.5 py-1 rounded transition-colors ${
                      outcomeFilter === 'resolved_after_transient_retry'
                        ? 'bg-amber-600 text-white font-medium'
                        : 'text-slate-400 hover:text-amber-400'
                    }`}
                  >
                    Resolved after retry ({report.summary.resolved_by_transient_retry})
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setOutcomeFilter('failed')}
                  className={`px-2.5 py-1 rounded transition-colors ${
                    outcomeFilter === 'failed'
                      ? 'bg-rose-600 text-white font-medium'
                      : 'text-slate-400 hover:text-rose-400'
                  }`}
                >
                  Failed ({report.summary.failed_operations})
                </button>
                {report.summary.error_operations > 0 && (
                  <button
                    type="button"
                    onClick={() => setOutcomeFilter('error')}
                    className={`px-2.5 py-1 rounded transition-colors ${
                      outcomeFilter === 'error'
                        ? 'bg-purple-600 text-white font-medium'
                        : 'text-slate-400 hover:text-purple-400'
                    }`}
                  >
                    Error ({report.summary.error_operations})
                  </button>
                )}
              </div>

              {/* Failure Category Dropdown Filter */}
              {failureCategories.length > 0 && (
                <div className="flex items-center gap-1.5 ml-auto">
                  <span className="text-slate-500 text-[11px] font-medium">Category:</span>
                  <select
                    value={categoryFilter}
                    onChange={(e) => setCategoryFilter(e.target.value)}
                    className="bg-slate-900 border border-slate-800 rounded-lg px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-indigo-500 font-mono"
                  >
                    <option value="all">All Categories</option>
                    {failureCategories.map((cat) => (
                      <option key={cat} value={cat}>
                        {cat}
                      </option>
                    ))}
                  </select>
                </div>
              )}
            </div>
          </div>

          {/* Operations List */}
          {filteredOperations.length === 0 ? (
            <div className="p-10 text-center text-slate-500 rounded-xl border border-dashed border-slate-800 bg-slate-900/30">
              <p className="text-sm font-medium text-slate-300">No operations matched your criteria</p>
              <p className="text-xs text-slate-500 mt-1">
                Try clearing search terms or selecting a different outcome filter.
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {filteredOperations.map((op, idx) => (
                <OperationDetailItem key={idx} operation={op} />
              ))}
            </div>
          )}
        </div>
      ) : (
        /* TAB 2: Pre-rendered Markdown View */
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-3">
            <div>
              <CardTitle>Markdown Diagnostic Report</CardTitle>
              <CardDescription>
                Sanitized GitHub-flavored Markdown representation (Content-Neutralized)
              </CardDescription>
            </div>
            <Button
              variant="secondary"
              size="sm"
              leftIcon={<Download className="w-4 h-4" />}
              onClick={handleDownloadMarkdown}
            >
              Download .md
            </Button>
          </CardHeader>
          <CardContent>
            {isMarkdownLoading ? (
              <PageLoader message="Loading markdown..." />
            ) : (
              <pre className="p-5 bg-slate-950 rounded-xl border border-slate-800 font-mono text-xs text-slate-300 overflow-x-auto whitespace-pre-wrap leading-relaxed max-h-[750px] overflow-y-auto">
                {markdown || report.markdown_rendered || 'No markdown content available.'}
              </pre>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
};
