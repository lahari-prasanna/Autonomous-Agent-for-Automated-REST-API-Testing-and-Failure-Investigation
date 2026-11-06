import React, { useState } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api, ApiClientError } from '../services/api';
import type { EndpointResponse } from '../types/api';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { HttpMethodBadge, StatusBadge } from '../components/common/Badge';
import { PageLoader } from '../components/common/Loading';
import { ConfirmModal } from '../components/common/ConfirmModal';
import { StartRunModal } from '../components/runs/StartRunModal';
import {
  ArrowLeft,
  Play,
  FileCode2,
  Calendar,
  Globe,
  Trash2,
  Search,
  ChevronDown,
  ChevronRight,
  Clock,
  AlertCircle,
} from 'lucide-react';

export const ProjectDetailView: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [isStartRunOpen, setIsStartRunOpen] = useState(false);
  const [isDeleteOpen, setIsDeleteOpen] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [endpointSearch, setEndpointSearch] = useState('');
  const [expandedEndpoints, setExpandedEndpoints] = useState<Set<string>>(new Set());

  const {
    data: project,
    isLoading: isProjectLoading,
    isError: isProjectError,
    error: projectError,
    refetch: refetchProject,
  } = useQuery({
    queryKey: ['project', id],
    queryFn: () => (id ? api.getProject(id) : Promise.reject('No project ID')),
    enabled: Boolean(id),
  });

  const {
    data: runs,
    isLoading: isRunsLoading,
    refetch: refetchRuns,
  } = useQuery({
    queryKey: ['project-runs', id],
    queryFn: () => (id ? api.listProjectRuns(id) : Promise.reject('No project ID')),
    enabled: Boolean(id),
  });

  const deleteMutation = useMutation({
    mutationFn: () => (id ? api.deleteProject(id) : Promise.reject('No project ID')),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      navigate('/projects');
    },
    onError: (err) => {
      const msg =
        err instanceof ApiClientError
          ? err.message
          : err instanceof Error
          ? err.message
          : 'Failed to delete project.';
      setDeleteError(msg);
      setIsDeleteOpen(false);
    },
  });

  const toggleEndpointExpand = (epId: string) => {
    setExpandedEndpoints((prev) => {
      const next = new Set(prev);
      if (next.has(epId)) {
        next.delete(epId);
      } else {
        next.add(epId);
      }
      return next;
    });
  };

  if (isProjectLoading) {
    return <PageLoader message="Loading project details and endpoints..." />;
  }

  if (isProjectError || !project) {
    return (
      <div className="space-y-4">
        <Link to="/projects">
          <Button variant="ghost" size="sm" leftIcon={<ArrowLeft className="w-4 h-4" />}>
            Back to Projects
          </Button>
        </Link>
        <div className="p-8 rounded-xl bg-rose-950/30 border border-rose-800/60 text-center space-y-3">
          <p className="text-sm font-medium text-rose-300">
            {projectError instanceof Error ? projectError.message : 'Project not found.'}
          </p>
          <Button variant="secondary" size="sm" onClick={() => refetchProject()}>
            Retry
          </Button>
        </div>
      </div>
    );
  }

  const endpoints: EndpointResponse[] = project.endpoints || [];
  const filteredEndpoints = endpoints.filter((ep) => {
    if (!endpointSearch.trim()) return true;
    const term = endpointSearch.toLowerCase();
    return (
      ep.path.toLowerCase().includes(term) ||
      ep.method.toLowerCase().includes(term) ||
      (ep.summary && ep.summary.toLowerCase().includes(term)) ||
      (ep.operation_id && ep.operation_id.toLowerCase().includes(term))
    );
  });

  return (
    <div className="space-y-6">
      {/* Navigation & Header */}
      <div>
        <Link to="/projects" className="inline-block mb-3">
          <Button variant="ghost" size="sm" leftIcon={<ArrowLeft className="w-4 h-4" />}>
            Back to Projects
          </Button>
        </Link>

        {deleteError && (
          <div className="mb-4 p-3 rounded-lg bg-rose-950/50 border border-rose-800 text-rose-300 text-xs flex items-center justify-between">
            <div className="flex items-center gap-2">
              <AlertCircle className="w-4 h-4 shrink-0 text-rose-400" />
              <span>{deleteError}</span>
            </div>
            <button
              onClick={() => setDeleteError(null)}
              className="text-slate-400 hover:text-slate-200"
            >
              Dismiss
            </button>
          </div>
        )}

        <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4 pb-4 border-b border-slate-800">
          <div>
            <div className="flex items-center gap-3">
              <h2 className="text-2xl font-bold text-slate-100">{project.name}</h2>
              {project.spec_version && (
                <span className="font-mono text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
                  OpenAPI {project.spec_version}
                </span>
              )}
            </div>
            <p className="text-sm text-slate-400 mt-1 max-w-2xl">
              {project.description || project.spec_title || 'No description provided.'}
            </p>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <Button
              variant="outline"
              size="sm"
              leftIcon={<Trash2 className="w-3.5 h-3.5 text-rose-400" />}
              onClick={() => setIsDeleteOpen(true)}
              className="text-rose-300 hover:text-rose-200 hover:border-rose-700/60"
            >
              Delete
            </Button>
            <Button
              variant="primary"
              size="sm"
              leftIcon={<Play className="w-4 h-4 fill-current" />}
              onClick={() => setIsStartRunOpen(true)}
            >
              Start Test Run
            </Button>
          </div>
        </div>
      </div>

      {/* Project Meta Information Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card className="p-4 flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-indigo-950/60 border border-indigo-800/40 flex items-center justify-center text-indigo-400 shrink-0">
            <Globe className="w-5 h-5" />
          </div>
          <div className="min-w-0">
            <p className="text-xs text-slate-400 uppercase font-semibold">Base URL</p>
            <p className="text-sm font-mono text-slate-200 truncate">
              {project.base_url || 'Default OpenAPI spec server'}
            </p>
          </div>
        </Card>

        <Card className="p-4 flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-emerald-950/60 border border-emerald-800/40 flex items-center justify-center text-emerald-400 shrink-0">
            <FileCode2 className="w-5 h-5" />
          </div>
          <div>
            <p className="text-xs text-slate-400 uppercase font-semibold">Extracted Endpoints</p>
            <p className="text-sm font-bold text-slate-200">
              {endpoints.length} operations
            </p>
          </div>
        </Card>

        <Card className="p-4 flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-slate-800 border border-slate-700 flex items-center justify-center text-slate-300 shrink-0">
            <Calendar className="w-5 h-5" />
          </div>
          <div>
            <p className="text-xs text-slate-400 uppercase font-semibold">Registered</p>
            <p className="text-sm text-slate-200">
              {new Date(project.created_at).toLocaleDateString()}
            </p>
          </div>
        </Card>
      </div>

      {/* Main Grid: Endpoints Table (2 cols) & Runs History (1 col) */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Endpoints List */}
        <div className="lg:col-span-2 space-y-4">
          <Card>
            <CardHeader className="pb-4">
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                <div>
                  <CardTitle>Extracted API Endpoints</CardTitle>
                  <CardDescription>
                    {filteredEndpoints.length} of {endpoints.length} discovered operations
                  </CardDescription>
                </div>
                {/* Search Filter */}
                <div className="relative w-full sm:w-64">
                  <Search className="w-3.5 h-3.5 absolute left-2.5 top-2.5 text-slate-500" />
                  <input
                    type="text"
                    value={endpointSearch}
                    onChange={(e) => setEndpointSearch(e.target.value)}
                    placeholder="Search endpoints..."
                    className="w-full pl-8 pr-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500"
                  />
                </div>
              </div>
            </CardHeader>

            <CardContent className="p-0">
              {endpoints.length === 0 ? (
                <div className="p-8 text-center text-slate-500 text-sm">
                  No endpoints extracted from this specification.
                </div>
              ) : filteredEndpoints.length === 0 ? (
                <div className="p-8 text-center text-slate-500 text-sm">
                  No endpoints match the filter "{endpointSearch}".
                </div>
              ) : (
                <div className="divide-y divide-slate-800 max-h-[600px] overflow-y-auto">
                  {filteredEndpoints.map((ep) => {
                    const isExpanded = expandedEndpoints.has(ep.id);
                    return (
                      <div key={ep.id} className="hover:bg-slate-850/40 transition-colors">
                        <div
                          onClick={() => toggleEndpointExpand(ep.id)}
                          className="p-4 flex items-start justify-between gap-4 cursor-pointer select-none"
                        >
                          <div className="space-y-1 min-w-0">
                            <div className="flex items-center gap-2.5">
                              {isExpanded ? (
                                <ChevronDown className="w-4 h-4 text-slate-500 shrink-0" />
                              ) : (
                                <ChevronRight className="w-4 h-4 text-slate-500 shrink-0" />
                              )}
                              <HttpMethodBadge method={ep.method} />
                              <span className="font-mono text-sm text-slate-200 truncate">
                                {ep.path}
                              </span>
                            </div>
                            {ep.summary && (
                              <p className="text-xs text-slate-400 pl-6">{ep.summary}</p>
                            )}
                          </div>
                          {ep.operation_id && (
                            <span className="font-mono text-[11px] text-slate-500 shrink-0 hidden sm:inline">
                              {ep.operation_id}
                            </span>
                          )}
                        </div>

                        {/* Expandable Operation Details */}
                        {isExpanded && (
                          <div className="px-6 pb-4 pt-1 bg-slate-950/60 border-t border-slate-800/60 text-xs space-y-3">
                            {ep.description && (
                              <div>
                                <span className="font-semibold text-slate-400">Description:</span>
                                <p className="text-slate-300 mt-0.5">{ep.description}</p>
                              </div>
                            )}

                            {/* Parameters inspection */}
                            {Boolean(Array.isArray(ep.parameters) && ep.parameters.length > 0) && (
                              <div>
                                <span className="font-semibold text-slate-400 block mb-1">
                                  Parameters ({ep.parameters!.length}):
                                </span>
                                <div className="space-y-1">
                                  {ep.parameters!.map((param, pIdx) => (
                                    <div
                                      key={pIdx}
                                      className="flex items-center gap-2 font-mono text-[11px] bg-slate-900/80 px-2.5 py-1 rounded border border-slate-800"
                                    >
                                      <span className="text-indigo-400 font-semibold">
                                        {param.name || `param_${pIdx}`}
                                      </span>
                                      <span className="text-slate-500">in</span>
                                      <span className="text-amber-400">{param.in || 'query'}</span>
                                      {param.required && (
                                        <span className="text-rose-400 text-[10px] font-sans">
                                          [required]
                                        </span>
                                      )}
                                      {param.schema?.type && (
                                        <span className="text-slate-400 text-[10px]">
                                          ({param.schema.type})
                                        </span>
                                      )}
                                    </div>
                                  ))}
                                </div>
                              </div>
                            )}

                            {/* Request Body Inspection */}
                            {Boolean(ep.request_body) && (
                              <div>
                                <span className="font-semibold text-slate-400 block mb-1">
                                  Request Body:
                                </span>
                                <div className="font-mono text-[11px] bg-slate-900/80 p-2 rounded border border-slate-800 text-slate-300">
                                  {typeof ep.request_body === 'object'
                                    ? JSON.stringify(ep.request_body, null, 2)
                                    : String(ep.request_body)}
                                </div>
                              </div>
                            )}

                            {/* Responses Inspection */}
                            {Boolean(ep.responses && typeof ep.responses === 'object') && (
                              <div>
                                <span className="font-semibold text-slate-400 block mb-1">
                                  Defined Responses:
                                </span>
                                <div className="flex flex-wrap gap-1.5">
                                  {Object.keys(ep.responses!).map((statusCode) => (
                                    <span
                                      key={statusCode}
                                      className="font-mono text-[11px] px-2 py-0.5 rounded bg-slate-900 border border-slate-800 text-slate-300"
                                    >
                                      {statusCode}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Test Run History */}
        <div className="space-y-4">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between pb-3">
              <div>
                <CardTitle>Execution History</CardTitle>
                <CardDescription>Autonomous runs for this project</CardDescription>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => refetchRuns()}
                className="text-xs"
              >
                Refresh
              </Button>
            </CardHeader>
            <CardContent className="p-0">
              {isRunsLoading ? (
                <div className="p-6 flex justify-center">
                  <PageLoader message="Loading runs..." />
                </div>
              ) : !runs || runs.length === 0 ? (
                <div className="p-6 text-center space-y-3">
                  <Clock className="w-8 h-8 text-slate-600 mx-auto" />
                  <p className="text-xs text-slate-400">
                    No runs executed yet for this project.
                  </p>
                  <Button
                    variant="primary"
                    size="sm"
                    leftIcon={<Play className="w-3.5 h-3.5 fill-current" />}
                    onClick={() => setIsStartRunOpen(true)}
                  >
                    Launch First Run
                  </Button>
                </div>
              ) : (
                <div className="divide-y divide-slate-800 max-h-[600px] overflow-y-auto">
                  {runs.map((run) => (
                    <div
                      key={run.id}
                      className="p-4 hover:bg-slate-850/50 transition-colors space-y-2.5"
                    >
                      <div className="flex items-center justify-between">
                        <StatusBadge status={run.status} />
                        <span className="text-[11px] text-slate-400 font-mono">
                          {new Date(run.created_at).toLocaleTimeString()}
                        </span>
                      </div>

                      {/* Run Summary metrics if available */}
                      {run.summary && (
                        <div className="flex items-center gap-3 text-[11px] font-mono text-slate-400 bg-slate-950/60 p-2 rounded border border-slate-800/60">
                          <span>Tests: {run.summary.total_tests ?? 0}</span>
                          <span className="text-emerald-400">
                            P: {run.summary.passed ?? 0}
                          </span>
                          <span className="text-rose-400">
                            F: {run.summary.failed ?? 0}
                          </span>
                        </div>
                      )}

                      <div className="flex items-center justify-between text-xs text-slate-400 pt-1">
                        <span className="font-mono text-[11px] truncate max-w-[100px]" title={run.id}>
                          {run.id.slice(0, 8)}...
                        </span>
                        <div className="flex items-center gap-2">
                          <Link to={`/runs/${run.id}`}>
                            <Button variant="ghost" size="sm" className="text-xs px-2 py-1">
                              Live
                            </Button>
                          </Link>
                          {run.status === 'completed' && (
                            <Link to={`/runs/${run.id}/report`}>
                              <Button
                                variant="secondary"
                                size="sm"
                                className="text-xs px-2 py-1"
                              >
                                Report
                              </Button>
                            </Link>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* Start Run Modal */}
      <StartRunModal
        isOpen={isStartRunOpen}
        onClose={() => setIsStartRunOpen(false)}
        projectId={project.id}
        projectName={project.name}
        defaultBaseUrl={project.base_url}
        endpoints={endpoints}
        onSuccess={(createdRun) => {
          setIsStartRunOpen(false);
          navigate(`/runs/${createdRun.id}`);
        }}
      />

      {/* Delete Project Confirmation Modal */}
      <ConfirmModal
        isOpen={isDeleteOpen}
        onClose={() => setIsDeleteOpen(false)}
        title="Delete Project"
        message={`Are you sure you want to permanently delete "${project.name}"? All ${endpoints.length} extracted endpoints and associated test execution runs will be permanently removed.`}
        confirmText="Permanently Delete"
        isDestructive={true}
        isLoading={deleteMutation.isPending}
        onConfirm={() => deleteMutation.mutate()}
      />
    </div>
  );
};
