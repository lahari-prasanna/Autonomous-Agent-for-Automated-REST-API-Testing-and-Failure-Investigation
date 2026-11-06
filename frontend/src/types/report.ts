/**
 * Detailed Diagnostic Report types matching backend schemas in backend/app/schemas/report.py.
 */

export type OverallRunStatus =
  | 'passed'
  | 'resolved_after_transient_retry'
  | 'failed'
  | 'error';

export interface AttemptOutcomeSummary {
  dispatched: boolean;
  uncertain_delivery?: boolean;
  status_code?: number | null;
  latency_ms?: number | null;
  passed: boolean;
  failure_category?: string | null;
  failed_assertions?: string[];
  error_type?: string | null;
}

export interface FollowupDecisionAuditRecord {
  followup_number: number;
  action: string;
  retry_of?: string | null;
  target_candidate_id?: string | null;
  eligibility_policy: string;
  reason_category: string;
  sanitized_reason: string;
  requested_delay_seconds?: number | null;
  remaining_deadline_seconds?: number | null;
  dispatched: boolean;
}

export interface TransientRetryRecord {
  attempt_number: number;
  retry_of: string;
  backoff_delay_seconds: number;
  canonical_request_fingerprint: string;
  outcome: AttemptOutcomeSummary;
}

export interface VariationCandidateRecord {
  candidate_id: string;
  strategy: string;
  target_parameter: string;
  parameter_location: 'path' | 'query' | 'header' | 'body' | string;
  applicable_constraint: string;
  canonical_request_fingerprint?: string | null;
  schema_validation_passed: boolean;
  authorization_approved: boolean;
  budget_approved: boolean;
  approved_for_dispatch: boolean;
  dispatched: boolean;
  rejection_reason?: string | null;
  dispatch_error?: string | null;
}

export interface ParameterPerturbation {
  parameter_name: string;
  original_value?: unknown;
  perturbed_value?: unknown;
  strategy?: string;
  pre_execution_validated?: boolean;
}

export interface VariationTestRecord {
  variation_number: number;
  candidate_id: string;
  canonical_request_fingerprint: string;
  perturbations_applied?: ParameterPerturbation[];
  is_non_idempotent_mutation?: boolean;
  outcome: AttemptOutcomeSummary;
}

export interface RemediationRecommendation {
  priority: 'critical' | 'high' | 'medium' | 'low' | string;
  target: 'server_implementation' | 'client_request' | 'openapi_spec' | string;
  finding: string;
  action: string;
  reference_urls?: string[];
}

export interface DetachedRetrievedEvidenceItem {
  chunk_id: string;
  document_id: string;
  source_type: string;
  source_ref?: string | null;
  relevance_score: number;
  distance: number;
  content: string;
  http_method?: string | null;
  path_template?: string | null;
  operation_id?: string | null;
  is_untrusted_evidence: boolean;
}

export interface DetachedInvestigationRecord {
  investigation_degraded: boolean;
  investigation_error_type?: string | null;
  inferences?: string[];
  source_references?: string[];
  retrieved_evidence?: DetachedRetrievedEvidenceItem[];
}

export interface OperationRunRecord {
  operation_id?: string | null;
  http_method: string;
  path_template: string;
  initial_request_fingerprint: string;
  initial_attempt: AttemptOutcomeSummary;
  followup_decisions?: FollowupDecisionAuditRecord[];
  transient_retries?: TransientRetryRecord[];
  variation_candidates?: VariationCandidateRecord[];
  variation_tests?: VariationTestRecord[];
  final_status: OverallRunStatus | string;
  final_outcome: AttemptOutcomeSummary;
  investigation?: DetachedInvestigationRecord | null;
  remediation_recommendations?: RemediationRecommendation[];
}

export interface VariationMetricsSummary {
  candidates_generated: number;
  candidates_schema_valid: number;
  candidates_rejected: number;
  candidates_approved: number;
  candidates_dispatched: number;
  candidates_dispatch_failed: number;
  rejections_schema_invalid: number;
  rejections_unauthorized: number;
  rejections_budget_exhausted: number;
  variation_tests_passed: number;
  variation_tests_failed: number;
  variation_success_rate_percentage: number;
}

export interface RunSummaryStatistics {
  total_operations: number;
  passed_operations: number;
  resolved_by_transient_retry: number;
  failed_operations: number;
  error_operations: number;
  total_transient_retries_dispatched: number;
  total_requests_executed: number;
  pass_rate_percentage: number;
  total_duration_ms: number;
  variation_metrics?: VariationMetricsSummary;
}

export interface DiagnosticReportData {
  id: string;
  run_id: string;
  report_id: string;
  api_title: string;
  api_version: string;
  base_url?: string | null;
  summary: RunSummaryStatistics;
  operations: OperationRunRecord[];
  global_remediation_summary?: string[] | null;
  markdown_rendered?: string | null;
  created_at: string;
}
