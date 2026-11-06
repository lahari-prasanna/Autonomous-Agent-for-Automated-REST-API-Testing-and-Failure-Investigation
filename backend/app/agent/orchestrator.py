"""
Outer workflow orchestrator (Phase 4C).
Coordinates compiled LangGraph execution, failure investigations,
bounded transient retries, fail-closed variation explorations, and report generation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import logging
import secrets
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from backend.app.agent.graph import agent_graph
from backend.app.agent.state import AgentError, AgentState, create_initial_state
from backend.app.schemas.execution import ExecutionRequest, ExecutionResult
from backend.app.schemas.failure import (
    FailureCategory,
    FailureClassification,
    FailureSeverity,
    RetryPolicy,
)
from backend.app.schemas.investigation import FailureInvestigation
from backend.app.schemas.report import (
    AttemptOutcomeSummary,
    DetachedInvestigationRecord,
    DetachedRetrievedEvidenceItem,
    DiagnosticReport,
    FollowupDecisionAuditRecord,
    OperationRunRecord,
    OverallRunStatus,
    RemediationRecommendation,
    TransientRetryRecord,
    VariationCandidateRecord,
    VariationTestRecord,
)
from backend.app.schemas.retest import (
    ParameterPerturbation,
    PerturbationStrategy,
    PolicyAuthorization,
    RetestAction,
    RetestDecision,
)
from backend.app.schemas.sanitizer import ReportDataSanitizer
from backend.app.schemas.spec import EndpointDefinition, ParsedAPISpec
from backend.app.services.failure_classifier import FailureClassifier
from backend.app.services.failure_investigator import FailureInvestigator
from backend.app.services.report_generator import DiagnosticReportGenerator
from backend.app.services.retest_controller import RetestController

logger = logging.getLogger(__name__)


class WorkflowOrchestrator:
    """
    Coordinates end-to-end API testing across single or batch operations.
    Treats the compiled Phase 3 agent_graph as an immutable external black box.
    """

    def __init__(
        self,
        knowledge_store: Optional[Any] = None,
        base_url: Optional[str] = None,
        max_concurrency: int = 1,
        ephemeral_hmac_key: Optional[bytes] = None,
        safe_dev_mode: bool = False,
    ) -> None:
        self.knowledge_store = knowledge_store
        self.base_url = base_url
        self.max_concurrency = max(1, min(max_concurrency, 5))
        # Ephemeral in-memory key for request fingerprints; never persisted
        self.ephemeral_hmac_key = ephemeral_hmac_key or secrets.token_bytes(32)
        self.safe_dev_mode = safe_dev_mode

    def prepare_initial_state(
        self,
        spec: ParsedAPISpec,
        operation: EndpointDefinition,
        raw_spec: Optional[Dict[str, Any]] = None,
        custom_base_url: Optional[str] = None,
    ) -> AgentState:
        """
        Constructs an isolated initial AgentState using only the approved Phase 3A public interface.
        """
        effective_base_url = custom_base_url or self.base_url
        metadata: Dict[str, Any] = {
            "operation_id": operation.operation_id,
            "path_template": operation.path,
            "method": operation.method.upper(),
        }
        if effective_base_url:
            metadata["base_url"] = effective_base_url
        if self.safe_dev_mode:
            metadata["safe_dev_mode"] = True

        return create_initial_state(
            spec=spec,
            raw_spec=raw_spec,
            selected_operation=operation,
            target_path=operation.path,
            target_method=operation.method.upper(),
            target_operation_id=operation.operation_id,
            status="idle",
            error=None,
            execution_result=None,
            assertion_report=None,
            metadata=metadata,
            logs=[f"Initialized orchestrator workflow for: {operation.method.upper()} {operation.path}"],
        )

    def _extract_attempt_summary(
        self,
        final_state: Union[AgentState, Dict[str, Any]],
        classification: FailureClassification,
    ) -> AttemptOutcomeSummary:
        """
        Extracts an AttemptOutcomeSummary based on actual transport evidence.
        """
        exec_result: Optional[ExecutionResult] = final_state.get("execution_result")
        err: Optional[AgentError] = final_state.get("error")
        cat = classification.primary_category

        # Determine transport dispatch
        if exec_result is not None:
            if exec_result.is_ssrf_blocked:
                # SSRF interception occurs before socket connection
                dispatched = False
                status_code = None
                uncertain_delivery = False
            elif exec_result.is_timeout:
                dispatched = True
                status_code = None  # No HTTP response received
                uncertain_delivery = True
            elif exec_result.is_network_error:
                dispatched = True
                status_code = None
                uncertain_delivery = False
            else:
                dispatched = True
                status_code = exec_result.status_code if exec_result.status_code > 0 else None
                uncertain_delivery = False
        else:
            # Failed prior to HTTP executor node
            dispatched = False
            status_code = None
            uncertain_delivery = False

        failed_assertions = []
        if classification.evidence and classification.evidence.contract_violation_summaries:
            failed_assertions.extend(classification.evidence.contract_violation_summaries)
        elif classification.evidence and classification.evidence.failed_assertion_types:
            failed_assertions.extend(classification.evidence.failed_assertion_types)

        error_type = None
        if err:
            error_type = err.error_type
        elif classification.evidence and classification.evidence.error_type:
            error_type = classification.evidence.error_type

        return AttemptOutcomeSummary(
            dispatched=dispatched,
            uncertain_delivery=uncertain_delivery,
            status_code=status_code,
            latency_ms=exec_result.latency_ms if exec_result else None,
            passed=not classification.has_failure,
            failure_category=cat,
            failed_assertions=tuple(failed_assertions),
            error_type=error_type,
        )

    def _detach_investigation(
        self,
        investigation: Optional[FailureInvestigation],
        degraded: bool = False,
        degraded_error_type: Optional[str] = None,
    ) -> Optional[DetachedInvestigationRecord]:
        """Deeply copies and detaches investigation evidence into immutable frozen models."""
        if not investigation and not degraded:
            return None

        detached_evidence: List[DetachedRetrievedEvidenceItem] = []
        inferences: List[str] = []
        source_refs: List[str] = []

        if investigation:
            inferences = list(investigation.inferences or [])
            source_refs = list(investigation.source_references or [])
            for item in investigation.retrieved_evidence or []:
                detached_evidence.append(
                    DetachedRetrievedEvidenceItem(
                        chunk_id=item.chunk_id,
                        document_id=item.document_id,
                        source_type=item.source_type,
                        source_ref=item.source_ref,
                        relevance_score=item.relevance_score,
                        distance=item.distance,
                        content=item.content,
                        http_method=item.http_method,
                        path_template=item.path_template,
                        operation_id=item.operation_id,
                        is_untrusted_evidence=True,
                    )
                )

        if degraded:
            inferences.append(f"Investigation degraded safely: {degraded_error_type or 'KnowledgeStoreUnavailable'}")

        return DetachedInvestigationRecord(
            investigation_degraded=degraded,
            investigation_error_type=degraded_error_type,
            inferences=tuple(inferences),
            source_references=tuple(source_refs),
            retrieved_evidence=tuple(detached_evidence),
        )

    async def run_operation(
        self,
        spec: ParsedAPISpec,
        operation: EndpointDefinition,
        raw_spec: Optional[Dict[str, Any]] = None,
        custom_base_url: Optional[str] = None,
    ) -> OperationRunRecord:
        """
        Executes a single endpoint operation workflow:
        1. Runs initial attempt via agent_graph.
        2. Classifies result via FailureClassifier.
        3. Investigates failure via FailureInvestigator (non-fatal fallback).
        4. Evaluates retest decisions and variation candidates in unified ledger.
        5. Executes bounded follow-ups (max 2).
        6. Assembles immutable OperationRunRecord.
        """
        initial_state = self.prepare_initial_state(
            spec=spec,
            operation=operation,
            raw_spec=raw_spec,
            custom_base_url=custom_base_url,
        )

        # 1. Execute initial attempt
        final_state = await agent_graph.ainvoke(initial_state)
        classification = FailureClassifier.classify(final_state)
        init_summary = self._extract_attempt_summary(final_state, classification)

        # Compute initial request fingerprint
        req: Optional[ExecutionRequest] = final_state.get("request_config")
        if req is not None:
            initial_fingerprint = RetestController.compute_canonical_request_fingerprint(
                req, self.ephemeral_hmac_key
            )
        else:
            # Fallback deterministic fingerprint for un-prepared requests
            dummy_req = ExecutionRequest(
                method=operation.method.upper(),
                url=f"http://placeholder.local{operation.path}",
            )
            initial_fingerprint = RetestController.compute_canonical_request_fingerprint(
                dummy_req, self.ephemeral_hmac_key
            )

        # 2. Failure Investigation (if failed)
        investigation_record: Optional[DetachedInvestigationRecord] = None
        if classification.has_failure:
            try:
                raw_inv = FailureInvestigator.investigate(final_state, store=self.knowledge_store)
                investigation_record = self._detach_investigation(raw_inv)
            except Exception as exc:
                logger.warning(f"Failure investigation degraded for {operation.path}: {exc}")
                investigation_record = self._detach_investigation(
                    None, degraded=True, degraded_error_type=exc.__class__.__name__
                )

        # 3. Follow-up coordination (Unified ledger)
        followup_decisions: List[FollowupDecisionAuditRecord] = []
        transient_retries: List[TransientRetryRecord] = []
        variation_candidates: List[VariationCandidateRecord] = []
        variation_tests: List[VariationTestRecord] = []

        followup_count = 0
        candidate_counter = 0

        # State tracking across follow-ups
        current_state = final_state
        current_classification = classification
        current_req = req

        # Bounded follow-up loop (at most 2 follow-ups)
        while followup_count < RetestController.MAX_FOLLOWUPS and current_classification.has_failure:
            followup_num = followup_count + 1

            # Check transient retry eligibility first (if idempotent and transient category)
            method = operation.method.upper()
            transient_categories = {
                FailureCategory.REQUEST_TIMEOUT,
                FailureCategory.NETWORK_FAILURE,
                FailureCategory.RATE_LIMITING,
                FailureCategory.SERVER_ERROR,
            }
            is_transient_eligible = (
                method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE")
                and (
                    current_classification.primary_category in transient_categories
                    or any(cat in transient_categories for cat in current_classification.secondary_categories)
                )
            )

            if is_transient_eligible:
                retest_decision = RetestController.decide_transient_retry(
                    http_method=method,
                    classification=current_classification,
                    followup_count=followup_count,
                    retry_of="initial" if followup_count == 0 else f"followup_{followup_count}",
                )

                if retest_decision.action == RetestAction.RETRY_TRANSIENT:
                    # Execute transient retry of identical request
                    if retest_decision.backoff_delay_seconds > 0:
                        await asyncio.sleep(min(retest_decision.backoff_delay_seconds, 0.05))

                    fresh_state = FailureClassifier.create_fresh_run_state(
                        current_state,
                        request_config=current_req,
                    )
                    retry_state = await agent_graph.ainvoke(fresh_state)
                    retry_classification = FailureClassifier.classify(retry_state)
                    retry_summary = self._extract_attempt_summary(retry_state, retry_classification)

                    retry_req = retry_state.get("request_config") or current_req
                    retry_fp = RetestController.compute_canonical_request_fingerprint(
                        retry_req, self.ephemeral_hmac_key
                    )

                    transient_retries.append(
                        TransientRetryRecord(
                            attempt_number=len(transient_retries) + 1,
                            retry_of="initial",
                            backoff_delay_seconds=retest_decision.backoff_delay_seconds,
                            canonical_request_fingerprint=retry_fp,
                            outcome=retry_summary,
                        )
                    )

                    followup_decisions.append(
                        FollowupDecisionAuditRecord(
                            followup_number=followup_num,
                            action=RetestAction.RETRY_TRANSIENT,
                            retry_of="initial",
                            target_candidate_id=None,
                            eligibility_policy=RetryPolicy.SAFE_TO_RETRY,
                            reason_category="dispatched_transient_retry",
                            sanitized_reason=retest_decision.rationale,
                            requested_delay_seconds=retest_decision.backoff_delay_seconds,
                            remaining_deadline_seconds=None,
                            dispatched=True,
                        )
                    )

                    followup_count += 1
                    current_state = retry_state
                    current_classification = retry_classification
                    current_req = retry_req

                    if not retry_classification.has_failure:
                        # Transient retry resolved the failure
                        break
                    continue
                else:
                    # Skipped transient retry decision
                    followup_decisions.append(
                        FollowupDecisionAuditRecord(
                            followup_number=followup_num,
                            action=RetestAction.NO_ACTION,
                            retry_of="initial",
                            target_candidate_id=None,
                            eligibility_policy=RetryPolicy.DO_NOT_RETRY,
                            reason_category="skipped_due_to_retry_after",
                            sanitized_reason=retest_decision.rationale,
                            requested_delay_seconds=retest_decision.backoff_delay_seconds,
                            remaining_deadline_seconds=None,
                            dispatched=False,
                        )
                    )
                    break

            # If not transient retry eligible, consider variation candidate (for 400, 422)
            is_validation_failure = (
                current_classification.primary_category == FailureCategory.REQUEST_VALIDATION_FAILURE
                or FailureCategory.REQUEST_VALIDATION_FAILURE in current_classification.secondary_categories
            )
            if is_validation_failure:
                candidate_counter += 1
                cand_id = f"cand_{candidate_counter}"

                var_decision, cand_data, cand_meta = RetestController.synthesize_variation_candidate(
                    candidate_id=cand_id,
                    operation=operation,
                    classification=current_classification,
                    followup_count=followup_count,
                    policy_authorization=None,  # Fail closed by default for non-idempotent
                )

                if var_decision.action == RetestAction.RUN_VARIATION_TEST and cand_data["approved_for_dispatch"]:
                    # Attempt dispatch
                    try:
                        perts = cand_meta.get("perturbations", [])
                        override_params = {}
                        for p in perts:
                            override_params[p.parameter_name] = p.varied_value

                        fresh_state = FailureClassifier.create_fresh_run_state(
                            current_state,
                            params=override_params,
                            request_config=None,  # Re-prepare request with perturbed params
                        )
                        var_state = await agent_graph.ainvoke(fresh_state)
                        var_classification = FailureClassifier.classify(var_state)
                        var_summary = self._extract_attempt_summary(var_state, var_classification)

                        var_req = var_state.get("request_config")
                        var_fp = RetestController.compute_canonical_request_fingerprint(
                            var_req, self.ephemeral_hmac_key
                        ) if var_req else initial_fingerprint

                        cand_data["dispatched"] = True
                        cand_data["canonical_request_fingerprint"] = var_fp
                        variation_candidates.append(VariationCandidateRecord(**cand_data))

                        variation_tests.append(
                            VariationTestRecord(
                                variation_number=len(variation_tests) + 1,
                                candidate_id=cand_id,
                                canonical_request_fingerprint=var_fp,
                                perturbations_applied=tuple(perts),
                                is_non_idempotent_mutation=(method in ("POST", "PATCH")),
                                authorization=None,
                                outcome=var_summary,
                            )
                        )

                        followup_decisions.append(
                            FollowupDecisionAuditRecord(
                                followup_number=followup_num,
                                action=RetestAction.RUN_VARIATION_TEST,
                                retry_of=None,
                                target_candidate_id=cand_id,
                                eligibility_policy=RetryPolicy.REQUIRES_MODIFICATION,
                                reason_category="dispatched_variation_test",
                                sanitized_reason=var_decision.rationale,
                                requested_delay_seconds=None,
                                remaining_deadline_seconds=None,
                                dispatched=True,
                            )
                        )

                        followup_count += 1
                        current_state = var_state
                        current_classification = var_classification
                        current_req = var_req
                        continue

                    except Exception as disp_exc:
                        cand_data["dispatched"] = False
                        cand_data["dispatch_error"] = f"DispatchException: {disp_exc.__class__.__name__}"
                        variation_candidates.append(VariationCandidateRecord(**cand_data))

                        followup_decisions.append(
                            FollowupDecisionAuditRecord(
                                followup_number=followup_num,
                                action=RetestAction.RUN_VARIATION_TEST,
                                retry_of=None,
                                target_candidate_id=cand_id,
                                eligibility_policy=RetryPolicy.REQUIRES_MODIFICATION,
                                reason_category="dispatch_failed",
                                sanitized_reason=f"Dispatch failed: {disp_exc.__class__.__name__}",
                                requested_delay_seconds=None,
                                remaining_deadline_seconds=None,
                                dispatched=False,
                            )
                        )
                        break
                else:
                    # Candidate was rejected before dispatch
                    cand_data["dispatched"] = False
                    variation_candidates.append(VariationCandidateRecord(**cand_data))

                    reason_cat = "schema_invalid"
                    if not cand_data["authorization_approved"]:
                        reason_cat = "authorization_denied"
                    elif not cand_data["budget_approved"]:
                        reason_cat = "budget_exhausted"

                    followup_decisions.append(
                        FollowupDecisionAuditRecord(
                            followup_number=followup_num,
                            action=RetestAction.NO_ACTION,
                            retry_of=None,
                            target_candidate_id=cand_id,
                            eligibility_policy=RetryPolicy.DO_NOT_RETRY,
                            reason_category=reason_cat,
                            sanitized_reason=var_decision.rationale,
                            requested_delay_seconds=None,
                            remaining_deadline_seconds=None,
                            dispatched=False,
                        )
                    )
                    break
            else:
                # Other non-retryable failure (e.g. 500, schema mismatch)
                break

        # 4. Determine final status and final outcome
        if not init_summary.passed:
            # Check if transient retry succeeded
            retries_of_initial = [r for r in transient_retries if r.retry_of == "initial"]
            if retries_of_initial and retries_of_initial[-1].outcome.passed:
                final_status = OverallRunStatus.RESOLVED_AFTER_TRANSIENT_RETRY
                final_outcome = retries_of_initial[-1].outcome
            else:
                # Check for pre-dispatch error vs failed
                is_pre_dispatch = (not init_summary.dispatched) and (init_summary.failure_category != FailureCategory.SSRF_SECURITY_BLOCK)
                is_eval_crash = (init_summary.failure_category == FailureCategory.EVALUATION_INCOMPLETE)

                if is_pre_dispatch or is_eval_crash:
                    final_status = OverallRunStatus.ERROR
                    final_outcome = init_summary
                else:
                    final_status = OverallRunStatus.FAILED
                    final_outcome = retries_of_initial[-1].outcome if retries_of_initial else init_summary
        else:
            final_status = OverallRunStatus.PASSED
            final_outcome = init_summary

        # 5. Extract remediation recommendations
        remediation_recommendations = []
        if classification.has_failure:
            remediation_recommendations.append(
                RemediationRecommendation(
                    priority=classification.severity,
                    target="server_implementation" if classification.primary_category == FailureCategory.SERVER_ERROR else "client_request",
                    finding=classification.summary,
                    action=classification.retry_guidance.reason,
                    reference_urls=tuple(),
                )
            )

        return OperationRunRecord(
            operation_id=operation.operation_id,
            http_method=operation.method.upper(),
            path_template=operation.path,
            initial_request_fingerprint=initial_fingerprint,
            initial_attempt=init_summary,
            followup_decisions=tuple(followup_decisions),
            transient_retries=tuple(transient_retries),
            variation_candidates=tuple(variation_candidates),
            variation_tests=tuple(variation_tests),
            final_status=final_status,
            final_outcome=final_outcome,
            investigation=investigation_record,
            remediation_recommendations=tuple(remediation_recommendations),
        )

    async def run_suite(
        self,
        spec: ParsedAPISpec,
        operations: Optional[Sequence[EndpointDefinition]] = None,
        raw_spec: Optional[Dict[str, Any]] = None,
        custom_base_url: Optional[str] = None,
    ) -> DiagnosticReport:
        """
        Runs an entire batch of operations with bounded concurrency,
        recording each operation's execution independently and returning a validated DiagnosticReport.
        """
        start_time = time.monotonic()
        ops_to_run = list(operations if operations is not None else spec.endpoints)

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def _run_bounded(op: EndpointDefinition) -> OperationRunRecord:
            async with semaphore:
                return await self.run_operation(
                    spec=spec,
                    operation=op,
                    raw_spec=raw_spec,
                    custom_base_url=custom_base_url,
                )

        # Execute bounded parallel tasks
        records: List[OperationRunRecord] = []
        if ops_to_run:
            records = await asyncio.gather(*[_run_bounded(op) for op in ops_to_run])

        duration_ms = (time.monotonic() - start_time) * 1000.0

        return DiagnosticReportGenerator.build_report(
            operations=records,
            api_title=spec.title,
            api_version=spec.version,
            base_url=custom_base_url or self.base_url or (spec.servers[0] if spec.servers else None),
            total_duration_ms=duration_ms,
        )
