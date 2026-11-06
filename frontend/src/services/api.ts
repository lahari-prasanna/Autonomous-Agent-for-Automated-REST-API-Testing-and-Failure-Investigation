import type {
  DiagnosticReportResponse,
  HealthResponse,
  ProjectCreateRequest,
  ProjectDetailResponse,
  ProjectResponse,
  RunCreateRequest,
  RunResponse,
} from '../types/api';

export interface ValidationErrorItem {
  loc?: (string | number)[];
  msg?: string;
  type?: string;
}

class ApiClientError extends Error {
  status: number;
  errorType?: string;
  errors?: ValidationErrorItem[];

  constructor(
    message: string,
    status: number,
    errorType?: string,
    errors?: ValidationErrorItem[]
  ) {
    super(message);
    this.name = 'ApiClientError';
    this.status = status;
    this.errorType = errorType;
    this.errors = errors;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers || {});
  if (!headers.has('Content-Type') && options.body && typeof options.body === 'string') {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(path, {
    ...options,
    headers,
  });

  if (!response.ok) {
    let errorDetail = `Request failed with status ${response.status}`;
    let errorType: string | undefined;
    let validationErrors: ValidationErrorItem[] | undefined;

    try {
      const data = await response.json();
      if (typeof data.detail === 'string') {
        errorDetail = data.detail;
      } else if (Array.isArray(data.detail)) {
        errorDetail = data.detail.map((d: { msg?: string }) => d.msg || JSON.stringify(d)).join('; ');
      }

      if (Array.isArray(data.errors)) {
        validationErrors = data.errors;
        const subMsgs = data.errors.map((e: ValidationErrorItem) => e.msg).filter(Boolean);
        if (subMsgs.length > 0) {
          errorDetail = `${errorDetail}: ${subMsgs.join('; ')}`;
        }
      }

      errorType = data.error_type;
    } catch {
      // Body not JSON
    }

    throw new ApiClientError(errorDetail, response.status, errorType, validationErrors);
  }

  if (response.status === 204) {
    return undefined as unknown as T;
  }

  const contentType = response.headers.get('content-type');
  if (contentType && contentType.includes('application/json')) {
    return response.json();
  }

  return response.text() as unknown as T;
}

export const api = {
  checkHealth: (): Promise<HealthResponse> => {
    return request<HealthResponse>('/health');
  },

  listProjects: (): Promise<ProjectResponse[]> => {
    return request<ProjectResponse[]>('/api/v1/projects');
  },

  getProject: (id: string): Promise<ProjectDetailResponse> => {
    return request<ProjectDetailResponse>(`/api/v1/projects/${id}`);
  },

  createProject: (payload: ProjectCreateRequest): Promise<ProjectDetailResponse> => {
    return request<ProjectDetailResponse>('/api/v1/projects', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  deleteProject: (id: string): Promise<void> => {
    return request<void>(`/api/v1/projects/${id}`, {
      method: 'DELETE',
    });
  },

  listProjectRuns: (projectId: string): Promise<RunResponse[]> => {
    return request<RunResponse[]>(`/api/v1/projects/${projectId}/runs`);
  },

  createRun: (projectId: string, payload: RunCreateRequest = {}): Promise<RunResponse> => {
    return request<RunResponse>(`/api/v1/projects/${projectId}/runs`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  getRun: (runId: string): Promise<RunResponse> => {
    return request<RunResponse>(`/api/v1/runs/${runId}`);
  },

  cancelRun: (runId: string): Promise<RunResponse> => {
    return request<RunResponse>(`/api/v1/runs/${runId}/cancel`, {
      method: 'POST',
    });
  },

  getReport: (runId: string): Promise<DiagnosticReportResponse> => {
    return request<DiagnosticReportResponse>(`/api/v1/runs/${runId}/report`);
  },

  getReportMarkdown: (runId: string): Promise<string> => {
    return request<string>(`/api/v1/runs/${runId}/report?format=markdown`);
  },
};

export { ApiClientError };
