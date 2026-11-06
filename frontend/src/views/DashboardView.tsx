import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../services/api';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { CreateProjectModal } from '../components/projects/CreateProjectModal';
import {
  Layers,
  Activity,
  FileCode2,
  ArrowRight,
  Plus,
  Cpu,
  Workflow,
  SearchCheck,
} from 'lucide-react';
import { Spinner } from '../components/common/Loading';

export const DashboardView: React.FC = () => {
  const navigate = useNavigate();
  const [isCreateOpen, setIsCreateOpen] = useState(false);

  const { data: projects, isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: api.listProjects,
  });

  const projectCount = projects?.length ?? 0;
  const totalEndpoints = projects?.reduce((acc, p) => acc + (p.endpoints_count || 0), 0) ?? 0;

  return (
    <div className="space-y-8">
      {/* Welcome Banner */}
      <div className="relative overflow-hidden rounded-2xl bg-gradient-to-r from-slate-900 via-indigo-950/40 to-slate-900 border border-slate-800 p-8">
        <div className="relative z-10 max-w-3xl space-y-4">
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-500/10 border border-indigo-500/20 text-xs font-medium text-indigo-400">
            <Cpu className="w-3.5 h-3.5" />
            Phase 6C Management Active
          </div>
          <h2 className="text-2xl sm:text-3xl font-bold text-slate-100 tracking-tight">
            Autonomous API Testing & Failure Investigation
          </h2>
          <p className="text-sm sm:text-base text-slate-300 leading-relaxed">
            AI-driven contract exploration, boundary mutation, semantic failure investigation,
            and deterministic re-testing for OpenAPI specifications.
          </p>
          <div className="flex flex-wrap gap-3 pt-2">
            <Link to="/projects">
              <Button variant="primary" rightIcon={<ArrowRight className="w-4 h-4" />}>
                Explore Projects
              </Button>
            </Link>
            <Button
              variant="secondary"
              leftIcon={<Plus className="w-4 h-4" />}
              onClick={() => setIsCreateOpen(true)}
            >
              New Specification
            </Button>
          </div>
        </div>
      </div>

      {/* Metric Overview Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
        <Card>
          <CardContent className="p-6">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                Registered Projects
              </span>
              <div className="w-8 h-8 rounded-lg bg-indigo-950/60 border border-indigo-800/40 flex items-center justify-center text-indigo-400">
                <Layers className="w-4 h-4" />
              </div>
            </div>
            <div className="mt-4 flex items-baseline gap-2">
              {isLoading ? (
                <Spinner size="sm" />
              ) : (
                <span className="text-3xl font-bold text-slate-100">{projectCount}</span>
              )}
              <span className="text-xs text-slate-400">OpenAPI specs</span>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-6">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                Extracted Endpoints
              </span>
              <div className="w-8 h-8 rounded-lg bg-emerald-950/60 border border-emerald-800/40 flex items-center justify-center text-emerald-400">
                <FileCode2 className="w-4 h-4" />
              </div>
            </div>
            <div className="mt-4 flex items-baseline gap-2">
              {isLoading ? (
                <Spinner size="sm" />
              ) : (
                <span className="text-3xl font-bold text-slate-100">{totalEndpoints}</span>
              )}
              <span className="text-xs text-slate-400">active operations</span>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-6">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                Agent Pipeline
              </span>
              <div className="w-8 h-8 rounded-lg bg-purple-950/60 border border-purple-800/40 flex items-center justify-center text-purple-400">
                <Activity className="w-4 h-4" />
              </div>
            </div>
            <div className="mt-4 flex items-baseline gap-2">
              <span className="text-lg font-semibold text-emerald-400">Operational</span>
              <span className="text-xs text-slate-400">LangGraph ready</span>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Architectural Capabilities Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <Card className="lg:col-span-1">
          <CardHeader>
            <div className="flex items-center gap-2 text-indigo-400 mb-1">
              <Workflow className="w-4 h-4" />
              <CardTitle>Autonomous Testing Loop</CardTitle>
            </div>
            <CardDescription>
              End-to-end execution flow managed across phases
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4 text-xs text-slate-300">
            <div className="flex items-start gap-3">
              <div className="w-6 h-6 rounded bg-slate-800 flex items-center justify-center font-mono font-semibold text-indigo-400 shrink-0">
                1
              </div>
              <div>
                <p className="font-medium text-slate-200">Specification Parsing</p>
                <p className="text-slate-400 mt-0.5">Strict schema validation and operation extraction.</p>
              </div>
            </div>

            <div className="flex items-start gap-3">
              <div className="w-6 h-6 rounded bg-slate-800 flex items-center justify-center font-mono font-semibold text-indigo-400 shrink-0">
                2
              </div>
              <div>
                <p className="font-medium text-slate-200">Test Generation & Execution</p>
                <p className="text-slate-400 mt-0.5">Boundary values and contract conformance checks.</p>
              </div>
            </div>

            <div className="flex items-start gap-3">
              <div className="w-6 h-6 rounded bg-slate-800 flex items-center justify-center font-mono font-semibold text-indigo-400 shrink-0">
                3
              </div>
              <div>
                <p className="font-medium text-slate-200">Failure Investigation</p>
                <p className="text-slate-400 mt-0.5">Semantic RAG analysis and root-cause classification.</p>
              </div>
            </div>

            <div className="flex items-start gap-3">
              <div className="w-6 h-6 rounded bg-slate-800 flex items-center justify-center font-mono font-semibold text-indigo-400 shrink-0">
                4
              </div>
              <div>
                <p className="font-medium text-slate-200">Deterministic Retesting</p>
                <p className="text-slate-400 mt-0.5">Flakiness detection with parameter variation matrix.</p>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <div className="flex items-center gap-2 text-indigo-400 mb-1">
              <SearchCheck className="w-4 h-4" />
              <CardTitle>Recent Projects</CardTitle>
            </div>
            <CardDescription>
              Recently registered OpenAPI specifications ready for test execution
            </CardDescription>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <div className="py-8 flex justify-center">
                <Spinner size="md" />
              </div>
            ) : projectCount === 0 ? (
              <div className="text-center py-10 border border-dashed border-slate-800 rounded-lg">
                <FileCode2 className="w-8 h-8 text-slate-600 mx-auto mb-2" />
                <p className="text-sm font-medium text-slate-300">No projects registered yet</p>
                <p className="text-xs text-slate-500 mt-1 max-w-sm mx-auto">
                  Upload an OpenAPI 3.0 specification in the Projects section to begin autonomous testing.
                </p>
                <div className="mt-4">
                  <Button
                    variant="secondary"
                    size="sm"
                    leftIcon={<Plus className="w-3.5 h-3.5" />}
                    onClick={() => setIsCreateOpen(true)}
                  >
                    Register Specification
                  </Button>
                </div>
              </div>
            ) : (
              <div className="divide-y divide-slate-800">
                {projects?.slice(0, 5).map((project) => (
                  <div
                    key={project.id}
                    className="py-3 flex items-center justify-between hover:bg-slate-800/30 px-3 rounded-lg transition-colors"
                  >
                    <div>
                      <Link
                        to={`/projects/${project.id}`}
                        className="text-sm font-medium text-slate-200 hover:text-indigo-400 transition-colors"
                      >
                        {project.name}
                      </Link>
                      <p className="text-xs text-slate-400 mt-0.5 font-mono">
                        {project.base_url || 'No base URL override'}
                      </p>
                    </div>
                    <div className="flex items-center gap-3">
                      <span className="text-xs font-mono text-slate-400">
                        {project.endpoints_count ?? 0} endpoints
                      </span>
                      <Link to={`/projects/${project.id}`}>
                        <Button variant="ghost" size="sm">
                          View
                        </Button>
                      </Link>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Create Project Modal */}
      <CreateProjectModal
        isOpen={isCreateOpen}
        onClose={() => setIsCreateOpen(false)}
        onSuccess={(newProject) => {
          setIsCreateOpen(false);
          navigate(`/projects/${newProject.id}`);
        }}
      />
    </div>
  );
};
