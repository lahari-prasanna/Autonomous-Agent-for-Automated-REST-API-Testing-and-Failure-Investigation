import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { api, ApiClientError } from '../services/api';
import type { ProjectResponse } from '../types/api';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '../components/common/Card';
import { Button } from '../components/common/Button';
import { EmptyState, PageLoader } from '../components/common/Loading';
import { ConfirmModal } from '../components/common/ConfirmModal';
import { CreateProjectModal } from '../components/projects/CreateProjectModal';
import {
  Layers,
  Plus,
  Calendar,
  Globe,
  FileCode2,
  ArrowRight,
  Trash2,
  AlertCircle,
  Tag,
} from 'lucide-react';

export const ProjectsView: React.FC = () => {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [projectToDelete, setProjectToDelete] = useState<ProjectResponse | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const {
    data: projects,
    isLoading,
    isError,
    error,
    refetch,
  } = useQuery({
    queryKey: ['projects'],
    queryFn: api.listProjects,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteProject(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      setProjectToDelete(null);
      setDeleteError(null);
    },
    onError: (err) => {
      const msg =
        err instanceof ApiClientError
          ? err.message
          : err instanceof Error
          ? err.message
          : 'Failed to delete project.';
      setDeleteError(msg);
    },
  });

  if (isLoading) {
    return <PageLoader message="Loading registered projects..." />;
  }

  if (isError) {
    return (
      <div className="p-8 rounded-xl bg-rose-950/30 border border-rose-800/60 text-center space-y-3">
        <p className="text-sm font-medium text-rose-300">
          Failed to load projects: {error instanceof Error ? error.message : 'Unknown error'}
        </p>
        <Button variant="secondary" size="sm" onClick={() => refetch()}>
          Retry
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header with Title and Action */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 pb-2 border-b border-slate-800">
        <div>
          <h2 className="text-xl font-bold text-slate-100">API Projects</h2>
          <p className="text-xs sm:text-sm text-slate-400 mt-1">
            Registered OpenAPI specifications and test execution suites
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="primary"
            leftIcon={<Plus className="w-4 h-4" />}
            onClick={() => setIsCreateOpen(true)}
          >
            New Project
          </Button>
        </div>
      </div>

      {/* Deletion Error Banner */}
      {deleteError && (
        <div className="p-4 rounded-xl bg-rose-950/40 border border-rose-800/80 text-rose-300 text-xs flex items-center justify-between">
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

      {/* Projects List or Empty State */}
      {!projects || projects.length === 0 ? (
        <EmptyState
          icon={<Layers className="w-10 h-10 text-slate-600" />}
          title="No OpenAPI projects registered"
          description="Create your first project by uploading an OpenAPI 3.0 specification in JSON or YAML format."
          action={
            <Button
              variant="primary"
              size="sm"
              leftIcon={<Plus className="w-4 h-4" />}
              onClick={() => setIsCreateOpen(true)}
            >
              Register Specification
            </Button>
          }
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
          {projects.map((project) => (
            <Card
              key={project.id}
              className="flex flex-col justify-between hover:border-slate-700 transition-all group"
            >
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="w-8 h-8 rounded-lg bg-indigo-950/60 border border-indigo-800/40 flex items-center justify-center text-indigo-400 shrink-0">
                    <FileCode2 className="w-4 h-4" />
                  </div>
                  <div className="flex items-center gap-1.5">
                    {project.spec_version && (
                      <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-800/90 text-slate-400 border border-slate-700/60 flex items-center gap-1">
                        <Tag className="w-2.5 h-2.5" />
                        v{project.spec_version}
                      </span>
                    )}
                    <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700/60">
                      {project.endpoints_count ?? 0} endpoints
                    </span>
                  </div>
                </div>

                <div className="mt-3 flex items-start justify-between gap-2">
                  <CardTitle className="group-hover:text-indigo-300 transition-colors">
                    {project.name}
                  </CardTitle>
                  <button
                    type="button"
                    onClick={() => setProjectToDelete(project)}
                    className="p-1 rounded text-slate-500 hover:text-rose-400 hover:bg-slate-800/80 transition-colors shrink-0"
                    title="Delete project"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>

                <CardDescription className="line-clamp-2 min-h-[2.5rem]">
                  {project.description || project.spec_title || 'No description provided.'}
                </CardDescription>
              </CardHeader>

              <CardContent className="pt-0 space-y-3">
                <div className="text-xs text-slate-400 space-y-1.5 pt-2 border-t border-slate-800/60">
                  <div className="flex items-center gap-2 truncate">
                    <Globe className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                    <span className="truncate font-mono">
                      {project.base_url || 'Default spec servers'}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 text-slate-500">
                    <Calendar className="w-3.5 h-3.5 shrink-0" />
                    <span>Created {new Date(project.created_at).toLocaleDateString()}</span>
                  </div>
                </div>

                <div className="pt-3">
                  <Link to={`/projects/${project.id}`} className="block">
                    <Button
                      variant="secondary"
                      size="sm"
                      className="w-full justify-between group-hover:bg-slate-700"
                      rightIcon={<ArrowRight className="w-3.5 h-3.5" />}
                    >
                      View Details
                    </Button>
                  </Link>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Create Project Modal */}
      <CreateProjectModal
        isOpen={isCreateOpen}
        onClose={() => setIsCreateOpen(false)}
        onSuccess={(newProject) => {
          setIsCreateOpen(false);
          navigate(`/projects/${newProject.id}`);
        }}
      />

      {/* Delete Confirmation Modal */}
      <ConfirmModal
        isOpen={Boolean(projectToDelete)}
        onClose={() => setProjectToDelete(null)}
        title="Delete Project"
        message={`Are you sure you want to permanently delete "${projectToDelete?.name}"? All associated endpoints, execution runs, investigations, and diagnostic reports will be cascade-deleted.`}
        confirmText="Permanently Delete"
        isDestructive={true}
        isLoading={deleteMutation.isPending}
        onConfirm={() => {
          if (projectToDelete) {
            deleteMutation.mutate(projectToDelete.id);
          }
        }}
      />
    </div>
  );
};
