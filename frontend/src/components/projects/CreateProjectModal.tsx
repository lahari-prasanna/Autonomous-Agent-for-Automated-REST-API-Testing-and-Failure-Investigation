import React, { useState, useRef } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Modal } from '../common/Modal';
import { Button } from '../common/Button';
import { api, ApiClientError } from '../../services/api';
import type { ProjectCreateRequest, ProjectDetailResponse } from '../../types/api';
import {
  Upload,
  FileCode,
  AlertCircle,
  X,
  Code,
} from 'lucide-react';

interface CreateProjectModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: (project: ProjectDetailResponse) => void;
}

export const CreateProjectModal: React.FC<CreateProjectModalProps> = ({
  isOpen,
  onClose,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [inputTab, setInputTab] = useState<'upload' | 'paste'>('upload');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [baseUrlOverride, setBaseUrlOverride] = useState('');
  const [specContent, setSpecContent] = useState('');
  const [fileName, setFileName] = useState<string | null>(null);
  const [fileSize, setFileSize] = useState<number | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [clientError, setClientError] = useState<string | null>(null);

  const resetForm = () => {
    setName('');
    setDescription('');
    setBaseUrlOverride('');
    setSpecContent('');
    setFileName(null);
    setFileSize(null);
    setClientError(null);
    setInputTab('upload');
  };

  const createMutation = useMutation({
    mutationFn: (payload: ProjectCreateRequest) => api.createProject(payload),
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      resetForm();
      onSuccess(project);
    },
  });

  const handleFileProcess = async (file: File) => {
    setClientError(null);
    const validExtensions = ['.json', '.yaml', '.yml'];
    const hasValidExt = validExtensions.some((ext) =>
      file.name.toLowerCase().endsWith(ext)
    );

    if (!hasValidExt) {
      setClientError('Please select a valid OpenAPI specification file (.json, .yaml, or .yml).');
      return;
    }

    try {
      const content = await file.text();
      setSpecContent(content);
      setFileName(file.name);
      setFileSize(file.size);

      // Default project name from file name if empty
      if (!name.trim()) {
        const cleanName = file.name.replace(/\.(yaml|yml|json)$/i, '');
        setName(cleanName);
      }
    } catch {
      setClientError('Failed to read file from local disk.');
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      handleFileProcess(file);
    }
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) {
      handleFileProcess(file);
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setClientError(null);

    const trimmedName = name.trim();
    if (!trimmedName) {
      setClientError('Project name is required.');
      return;
    }

    const trimmedSpec = specContent.trim();
    if (!trimmedSpec) {
      setClientError('OpenAPI specification content is required. Please upload or paste a spec.');
      return;
    }

    const payload: ProjectCreateRequest = {
      name: trimmedName,
      spec_content: trimmedSpec,
      description: description.trim() || undefined,
      base_url_override: baseUrlOverride.trim() || undefined,
    };

    createMutation.mutate(payload);
  };

  const apiError = createMutation.error instanceof ApiClientError ? createMutation.error : null;
  const genericErrorMessage =
    createMutation.error && !apiError
      ? createMutation.error instanceof Error
        ? createMutation.error.message
        : 'Failed to create project.'
      : null;

  return (
    <Modal
      isOpen={isOpen}
      onClose={() => {
        if (!createMutation.isPending) {
          resetForm();
          onClose();
        }
      }}
      title="Create API Project"
      description="Import an OpenAPI 3.0.x or 3.1.x specification to generate test suites and boundary mutations"
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
            disabled={createMutation.isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="primary"
            size="sm"
            isLoading={createMutation.isPending}
            onClick={handleSubmit}
          >
            Create & Parse Project
          </Button>
        </>
      }
    >
      <form onSubmit={handleSubmit} className="space-y-5">
        {/* Error Banners */}
        {clientError && (
          <div className="p-3 rounded-lg bg-rose-950/50 border border-rose-800/80 text-rose-300 text-xs flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 text-rose-400 mt-0.5" />
            <span>{clientError}</span>
          </div>
        )}

        {apiError && (
          <div className="p-3 rounded-lg bg-rose-950/50 border border-rose-800/80 text-rose-300 text-xs space-y-1">
            <div className="flex items-center gap-2 font-semibold">
              <AlertCircle className="w-4 h-4 shrink-0 text-rose-400" />
              <span>
                {apiError.errorType ? `${apiError.errorType}: ` : 'Validation Error: '}
                {apiError.message}
              </span>
            </div>
          </div>
        )}

        {genericErrorMessage && (
          <div className="p-3 rounded-lg bg-rose-950/50 border border-rose-800/80 text-rose-300 text-xs flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 text-rose-400 mt-0.5" />
            <span>{genericErrorMessage}</span>
          </div>
        )}

        {/* Input Method Tabs */}
        <div>
          <label className="block text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2">
            Specification Source
          </label>
          <div className="grid grid-cols-2 p-1 bg-slate-950 border border-slate-800 rounded-lg">
            <button
              type="button"
              onClick={() => setInputTab('upload')}
              className={`flex items-center justify-center gap-2 py-2 text-xs font-medium rounded-md transition-all ${
                inputTab === 'upload'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <Upload className="w-3.5 h-3.5" />
              Upload YAML / JSON
            </button>
            <button
              type="button"
              onClick={() => setInputTab('paste')}
              className={`flex items-center justify-center gap-2 py-2 text-xs font-medium rounded-md transition-all ${
                inputTab === 'paste'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              <Code className="w-3.5 h-3.5" />
              Paste Specification
            </button>
          </div>
        </div>

        {/* Tab 1: Upload Dropzone */}
        {inputTab === 'upload' ? (
          <div>
            <input
              ref={fileInputRef}
              type="file"
              accept=".json,.yaml,.yml"
              onChange={handleFileChange}
              className="hidden"
            />

            {!fileName ? (
              <div
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all ${
                  isDragging
                    ? 'border-indigo-500 bg-indigo-950/20'
                    : 'border-slate-800 hover:border-slate-700 bg-slate-950/40 hover:bg-slate-950/60'
                }`}
              >
                <Upload className="w-8 h-8 text-slate-500 mx-auto mb-2" />
                <p className="text-sm font-medium text-slate-200">
                  Drop OpenAPI specification file here, or{' '}
                  <span className="text-indigo-400 underline underline-offset-2">browse</span>
                </p>
                <p className="text-xs text-slate-500 mt-1">
                  Supports JSON, YAML, and YML format
                </p>
              </div>
            ) : (
              <div className="p-4 rounded-xl bg-slate-950 border border-slate-800 flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <div className="w-9 h-9 rounded-lg bg-indigo-950/80 border border-indigo-800/60 flex items-center justify-center text-indigo-400">
                    <FileCode className="w-5 h-5" />
                  </div>
                  <div>
                    <p className="text-sm font-medium text-slate-200">{fileName}</p>
                    <p className="text-xs text-slate-400 font-mono">
                      {fileSize ? `${(fileSize / 1024).toFixed(1)} KB` : ''} •{' '}
                      <span className="text-emerald-400">Ready for parsing</span>
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    setFileName(null);
                    setFileSize(null);
                    setSpecContent('');
                    if (fileInputRef.current) fileInputRef.current.value = '';
                  }}
                  className="p-1 rounded text-slate-400 hover:text-slate-200 hover:bg-slate-800 transition-colors"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            )}
          </div>
        ) : (
          /* Tab 2: Raw Paste */
          <div>
            <textarea
              rows={8}
              value={specContent}
              onChange={(e) => {
                setSpecContent(e.target.value);
                setFileName(null);
                setFileSize(null);
              }}
              placeholder="Paste raw OpenAPI 3.0 JSON or YAML content here..."
              className="w-full rounded-xl bg-slate-950 border border-slate-800 p-3 font-mono text-xs text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition-colors"
            />
            <div className="flex justify-between items-center text-[11px] text-slate-500 mt-1">
              <span>Must be valid OpenAPI YAML or JSON</span>
              <span>{specContent.length} characters</span>
            </div>
          </div>
        )}

        {/* Project Metadata Fields */}
        <div className="space-y-4 pt-2 border-t border-slate-800/80">
          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-1">
              Project Name <span className="text-rose-400">*</span>
            </label>
            <input
              type="text"
              required
              maxLength={255}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Petstore API"
              className="w-full rounded-lg bg-slate-950 border border-slate-800 px-3 py-2 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition-colors"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-1">
              Target Base URL Override <span className="text-slate-500 font-normal">(Optional)</span>
            </label>
            <input
              type="url"
              maxLength={1024}
              value={baseUrlOverride}
              onChange={(e) => setBaseUrlOverride(e.target.value)}
              placeholder="e.g. https://api.staging.example.com or http://127.0.0.1:8080"
              className="w-full rounded-lg bg-slate-950 border border-slate-800 px-3 py-2 text-sm font-mono text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition-colors"
            />
            <p className="text-[11px] text-slate-500 mt-1">
              Overrides server URLs defined inside the OpenAPI specification.
            </p>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-300 mb-1">
              Description <span className="text-slate-500 font-normal">(Optional)</span>
            </label>
            <textarea
              rows={2}
              maxLength={2000}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Scope, environment details, or notes..."
              className="w-full rounded-lg bg-slate-950 border border-slate-800 px-3 py-2 text-sm text-slate-200 placeholder-slate-600 focus:outline-none focus:border-indigo-500 transition-colors"
            />
          </div>
        </div>
      </form>
    </Modal>
  );
};
