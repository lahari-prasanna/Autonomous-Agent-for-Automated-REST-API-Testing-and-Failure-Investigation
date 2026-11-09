import React, { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Modal } from '../common/Modal';
import { Button } from '../common/Button';
import { HttpMethodBadge } from '../common/Badge';
import { api, ApiClientError } from '../../services/api';
import type { EndpointResponse, RunCreateRequest, RunResponse } from '../../types/api';
import {
  Play,
  Sliders,
  ShieldCheck,
  AlertCircle,
  Check,
  Search,
} from 'lucide-react';

interface StartRunModalProps {
  isOpen: boolean;
  onClose: () => void;
  projectId: string;
  projectName: string;
  defaultBaseUrl?: string | null;
  endpoints: EndpointResponse[];
  onSuccess: (run: RunResponse) => void;
}

export const StartRunModal: React.FC<StartRunModalProps> = ({
  isOpen,
  onClose,
  projectId,
  projectName,
  defaultBaseUrl,
  endpoints,
  onSuccess,
}) => {
  const queryClient = useQueryClient();

  const [concurrency, setConcurrency] = useState<number>(1);
  const [scopeMode, setScopeMode] = useState<'all' | 'custom'>('all');
  const [selectedEndpoints, setSelectedEndpoints] = useState<Set<string>>(new Set());
  const [endpointSearch, setEndpointSearch] = useState('');
  const [customBaseUrl, setCustomBaseUrl] = useState(defaultBaseUrl || '');
  const [safeDevMode, setSafeDevMode] = useState(true); // default true for developer convenience
  const [clientError, setClientError] = useState<string | null>(null);

  const resetForm = () => {
    setConcurrency(1);
    setScopeMode('all');
    setSelectedEndpoints(new Set());
    setEndpointSearch('');
    setCustomBaseUrl(defaultBaseUrl || '');
    setSafeDevMode(true);
    setClientError(null);
  };

  const runMutation = useMutation({
    mutationFn: (payload: RunCreateRequest) => api.createRun(projectId, payload),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ['project-runs', projectId] });
      resetForm();
      onSuccess(run);
    },
  });

  const toggleEndpoint = (path: string) => {
    setSelectedEndpoints((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const filteredEndpoints = endpoints.filter((ep) => {
    const q = endpointSearch.toLowerCase();
    return ep.path.toLowerCase().includes(q) || ep.method.toLowerCase().includes(q);
  });

  const selectAllFiltered = () => {
    setSelectedEndpoints((prev) => {
      const next = new Set(prev);
      filteredEndpoints.forEach((ep) => next.add(ep.path));
      return next;
    });
  };

  const deselectAllFiltered = () => {
    setSelectedEndpoints((prev) => {
      const next = new Set(prev);
      filteredEndpoints.forEach((ep) => next.delete(ep.path));
      return next;
    });
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setClientError(null);

    let targetEndpointsList: string[] | undefined = undefined;
    if (scopeMode === 'custom') {
      if (selectedEndpoints.size === 0) {
        setClientError('Please select at least one endpoint to test, or switch to "All Endpoints".');
        return;
      }
      targetEndpointsList = Array.from(selectedEndpoints);
    }

    const payload: RunCreateRequest = {
      concurrency,
      target_endpoints: targetEndpointsList,
      custom_base_url: customBaseUrl.trim() || undefined,
      safe_dev_mode: safeDevMode,
    };

    runMutation.mutate(payload);
  };

  const apiError = runMutation.error instanceof ApiClientError ? runMutation.error : null;
  const genericErrorMessage =
    runMutation.error && !apiError
      ? runMutation.error instanceof Error
        ? runMutation.error.message
        : 'Failed to initiate test run.'
      : null;

  return (
    <Modal
      isOpen={isOpen}
      onClose={() => {
        if (!runMutation.isPending) {
          resetForm();
          onClose();
        }
      }}
      title={`Start Test Run — ${projectName}`}
      description="Configure execution concurrency, target endpoints, and base URL"
      maxWidth="2xl"
      footer={
        <>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              resetForm();
              onClose();
            }}
            disabled={runMutation.isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="primary"
            size="sm"
            leftIcon={<Play className="w-4 h-4 fill-current" />}
            isLoading={runMutation.isPending}
            onClick={handleSubmit}
          >
            Launch Run
          </Button>
        </>
      }
    >
      <form onSubmit={handleSubmit} className="space-y-5">
        {/* Error Banners */}
        {clientError && (
          <div className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-700 text-xs flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 text-rose-500 mt-0.5" />
            <span>{clientError}</span>
          </div>
        )}

        {apiError && (
          <div className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-700 text-xs flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 text-rose-500 mt-0.5" />
            <div>
              <span className="font-semibold">
                {apiError.errorType ? `${apiError.errorType}: ` : ''}
              </span>
              <span>{apiError.message}</span>
            </div>
          </div>
        )}

        {genericErrorMessage && (
          <div className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-700 text-xs flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 text-rose-500 mt-0.5" />
            <span>{genericErrorMessage}</span>
          </div>
        )}

        {/* Concurrency Setting */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-semibold uppercase tracking-wider text-slate-700 flex items-center gap-1.5">
              <Sliders className="w-3.5 h-3.5 text-blue-600" />
              Concurrency Limit
            </label>
            <span className="text-xs font-mono px-2 py-0.5 rounded bg-blue-50 text-blue-700 border border-blue-200 font-semibold">
              {concurrency} {concurrency === 1 ? 'Worker' : 'Workers'}
            </span>
          </div>
          <div className="grid grid-cols-5 gap-2">
            {[1, 2, 3, 4, 5].map((lvl) => (
              <button
                key={lvl}
                type="button"
                onClick={() => setConcurrency(lvl)}
                className={`py-2 text-xs font-semibold rounded-lg border transition-all ${
                  concurrency === lvl
                    ? 'bg-blue-600 text-white border-blue-600 shadow-sm'
                    : 'bg-white text-slate-700 border-slate-300 hover:border-slate-400'
                }`}
              >
                {lvl}
              </button>
            ))}
          </div>
          <p className="text-[11px] text-slate-500 mt-1.5">
            Bounded to 1–5 concurrent workers to protect the target server from exhaustion.
          </p>
        </div>

        {/* Target Scope Selection */}
        <div>
          <label className="block text-xs font-semibold uppercase tracking-wider text-slate-700 mb-2">
            Endpoint Test Scope
          </label>
          <div className="grid grid-cols-2 p-1 bg-slate-100 border border-slate-200 rounded-lg mb-3">
            <button
              type="button"
              onClick={() => setScopeMode('all')}
              className={`py-2 text-xs font-medium rounded-md transition-all ${
                scopeMode === 'all'
                  ? 'bg-white text-blue-700 font-semibold shadow-sm'
                  : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              All Endpoints ({endpoints.length})
            </button>
            <button
              type="button"
              onClick={() => setScopeMode('custom')}
              className={`py-2 text-xs font-medium rounded-md transition-all ${
                scopeMode === 'custom'
                  ? 'bg-white text-blue-700 font-semibold shadow-sm'
                  : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              Select Specific ({selectedEndpoints.size})
            </button>
          </div>

          {scopeMode === 'custom' && (
            <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 space-y-3">
              {/* Search & Bulk Selection */}
              <div className="flex items-center gap-2">
                <div className="relative flex-1">
                  <Search className="w-3.5 h-3.5 absolute left-2.5 top-2.5 text-slate-400" />
                  <input
                    type="text"
                    value={endpointSearch}
                    onChange={(e) => setEndpointSearch(e.target.value)}
                    placeholder="Filter endpoints by path or method..."
                    className="w-full pl-8 pr-3 py-1.5 bg-white border border-slate-300 rounded-lg text-xs text-slate-900 placeholder-slate-400 focus:outline-none focus:border-blue-500"
                  />
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={selectAllFiltered}
                  className="text-xs shrink-0"
                >
                  Select All
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={deselectAllFiltered}
                  className="text-xs shrink-0"
                >
                  Clear
                </Button>
              </div>

              {/* Endpoint List */}
              <div className="max-h-48 overflow-y-auto divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white">
                {filteredEndpoints.length === 0 ? (
                  <p className="p-4 text-center text-xs text-slate-500">
                    No matching endpoints found.
                  </p>
                ) : (
                  filteredEndpoints.map((ep) => {
                    const isSelected = selectedEndpoints.has(ep.path);
                    return (
                      <div
                        key={ep.id}
                        onClick={() => toggleEndpoint(ep.path)}
                        className={`p-2.5 flex items-center justify-between cursor-pointer text-xs transition-colors ${
                          isSelected
                            ? 'bg-blue-50/70 text-slate-900 font-medium'
                            : 'hover:bg-slate-50 text-slate-700'
                        }`}
                      >
                        <div className="flex items-center gap-2 min-w-0">
                          <HttpMethodBadge method={ep.method} className="text-[10px] px-1.5" />
                          <span className="font-mono truncate">{ep.path}</span>
                        </div>
                        <div
                          className={`w-4 h-4 rounded border flex items-center justify-center shrink-0 ml-2 ${
                            isSelected
                              ? 'bg-blue-600 border-blue-600 text-white'
                              : 'border-slate-300 bg-white'
                          }`}
                        >
                          {isSelected && <Check className="w-3 h-3 stroke-[3]" />}
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          )}
        </div>

        {/* Execution Base URL Override */}
        <div>
          <label className="block text-xs font-semibold text-slate-700 mb-1">
            Custom Base URL Override <span className="text-slate-400 font-normal">(Optional)</span>
          </label>
          <input
            type="url"
            value={customBaseUrl}
            onChange={(e) => setCustomBaseUrl(e.target.value)}
            placeholder="e.g. http://127.0.0.1:8000 or https://staging.api.example.com"
            className="w-full rounded-lg bg-white border border-slate-300 px-3 py-2 text-sm font-mono text-slate-900 placeholder-slate-400 focus:outline-none focus:border-blue-500"
          />
        </div>

        {/* Safe Dev Mode Toggle */}
        <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 flex items-start gap-3">
          <input
            id="safe_dev_mode"
            type="checkbox"
            checked={safeDevMode}
            onChange={(e) => setSafeDevMode(e.target.checked)}
            className="mt-0.5 h-4 w-4 rounded border-slate-300 bg-white text-blue-600 focus:ring-blue-500"
          />
          <label htmlFor="safe_dev_mode" className="text-xs cursor-pointer select-none">
            <span className="font-semibold text-slate-900 flex items-center gap-1.5">
              <ShieldCheck className="w-3.5 h-3.5 text-emerald-600" />
              Safe Dev Mode (Permit 127.0.0.1 loopback)
            </span>
            <p className="text-slate-500 mt-0.5 leading-relaxed">
              Allows verified local loopback requests to mock test servers and prevents SSRF
              blocks on localhost.
            </p>
          </label>
        </div>
      </form>
    </Modal>
  );
};
