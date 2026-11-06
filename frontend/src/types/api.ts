/**
 * TypeScript API contracts matching backend FastAPI schemas.
 */

export interface HealthResponse {
  status: string;
  environment: string;
  timestamp: string;
}

export interface EndpointParameter {
  name?: string;
  in?: string;
  required?: boolean;
  description?: string;
  schema?: {
    type?: string;
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

export interface EndpointResponse {
  id: string;
  project_id: string;
  method: string;
  path: string;
  operation_id?: string | null;
  summary?: string | null;
  description?: string | null;
  parameters?: EndpointParameter[] | null;
  request_body?: Record<string, unknown> | null;
  responses?: Record<string, unknown> | null;
  created_at: string;
}

export interface ProjectResponse {
  id: string;
  name: string;
  description?: string | null;
  base_url?: string | null;
  spec_title?: string | null;
  spec_version?: string | null;
  endpoints_count?: number | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectDetailResponse extends ProjectResponse {
  endpoints: EndpointResponse[];
}

export interface ProjectCreateRequest {
  name: string;
  spec_content: string;
  description?: string;
  base_url_override?: string;
}

export type RunStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface RunCreateRequest {
  concurrency?: number;
  target_endpoints?: string[];
  custom_base_url?: string;
  safe_dev_mode?: boolean;
}

export interface RunSummary {
  total_operations?: number;
  total_tests?: number;
  passed?: number;
  failed?: number;
  investigations?: number;
  retests?: number;
  retest_confirmed?: number;
  flaky_detected?: number;
  success_rate?: number;
  [key: string]: unknown;
}

export interface RunResponse {
  id: string;
  project_id: string;
  status: RunStatus;
  concurrency: number;
  target_endpoints?: string[] | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  summary?: RunSummary | null;
  error_message?: string | null;
  created_at: string;
}

import type { DiagnosticReportData } from './report';

export type DiagnosticReportResponse = DiagnosticReportData;

export interface ApiError {
  detail: string;
  error_type?: string;
}
