"""
Evidence-Supported Failure Investigation Service (Phase 4B.5).

Coordinates deterministic Phase 4A failure classification with Phase 4B.4
semantic knowledge retrieval. Retrieves relevant OpenAPI documentation, runbooks,
or failure history, manages reader snapshot session lifecycles, and synthesizes
structured failure investigations with strictly separated execution facts,
untrusted documentation evidence, and rule-based diagnostic inferences.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union
import copy
import logging

if TYPE_CHECKING:
    from backend.app.agent.state import AgentState
    from backend.app.services.knowledge_store import LocalKnowledgeStore, QueryReaderSession, RetrievalFilter

from backend.app.schemas.failure import (
    FailureCategory,
    FailureClassification,
    FailureSeverity,
    SanitizedFailureEvidence,
)
from backend.app.schemas.investigation import (
    FailureInvestigation,
    ObservedExecutionFacts,
    RetrievedEvidenceItem,
)
from backend.app.schemas.knowledge import KnowledgeSourceType
from backend.app.schemas.spec import EndpointDefinition
from backend.app.services.failure_classifier import FailureClassifier

logger = logging.getLogger(__name__)


class FailureInvestigator:
    """
    Evidence-supported failure investigation engine (Phase 4B.5).
    Consumes deterministic Phase 4A failure classification and augments it with
    sanitized, evidence-backed documentation from the knowledge store without altering
    underlying deterministic classification rules.
    """

    @classmethod
    def investigate(
        cls,
        state: Union[AgentState, Dict[str, Any]],
        store: Optional[Any] = None,
        top_k: int = 5,
        filter: Optional[Union[Any, Dict[str, Any]]] = None,
        source_type: Optional[Union[KnowledgeSourceType, str]] = None,
        session: Optional[Any] = None,
        auto_filter_operation: bool = False,
    ) -> FailureInvestigation:
        """
        Main failure investigation entrypoint (Phase 4B.5).
        Combines deterministic Phase 4A failure classification, observed execution facts,
        retrieved documentation evidence from the knowledge store, and rule-based explanations.
        Ensures strict reader snapshot isolation and zero exposure of sensitive data.
        """
        # 1. Deterministic classification runs first; never modified by retrieval
        classification = FailureClassifier.classify(state)

        # 2. Extract strictly allowlisted observed execution facts
        ev = classification.evidence
        op: Optional[EndpointDefinition] = state.get("selected_operation") if isinstance(state, dict) else getattr(state, "get", lambda k: None)("selected_operation")
        op_id = None
        if op is not None and hasattr(op, "operation_id") and op.operation_id:
            op_id = op.operation_id
        elif isinstance(state, dict) and state.get("target_operation_id"):
            op_id = state.get("target_operation_id")

        method = ev.http_method or (state.get("target_method") if isinstance(state, dict) else None) or "UNKNOWN"
        path_template = ev.path_template

        state_status = state.get("status") if isinstance(state, dict) else None
        observed_facts = ObservedExecutionFacts(
            has_failure=classification.has_failure,
            http_method=method,
            path_template=path_template,
            operation_id=op_id,
            status_code=ev.status_code,
            sanitized_url=ev.sanitized_url,
            lifecycle_step=ev.lifecycle_step or state_status,
            error_type=ev.error_type,
            failed_assertion_types=list(ev.failed_assertion_types),
            schema_keywords=list(ev.schema_keywords),
            json_paths=list(ev.json_paths),
            contract_violation_summaries=list(ev.contract_violation_summaries),
        )

        # 3. For successful tests (has_failure is False), return immediately without retrieval
        if not classification.has_failure:
            return FailureInvestigation(
                classification=classification,
                observed_facts=observed_facts,
                retrieved_evidence=[],
                inferences=["All deterministic contract assertions passed and HTTP exchange completed successfully."],
                source_references=[],
            )

        # 4. Construct sanitized retrieval query from observed facts and violations
        query_parts: List[str] = []
        if method and method != "UNKNOWN":
            query_parts.append(f"HTTP {method}")
        if path_template:
            query_parts.append(path_template)
        if op_id:
            query_parts.append(f"operation {op_id}")
        if ev.status_code and ev.status_code > 0:
            query_parts.append(f"status {ev.status_code}")
        if classification.primary_category:
            query_parts.append(classification.primary_category.value.replace("_", " "))
        if ev.contract_violation_summaries:
            query_parts.extend(ev.contract_violation_summaries)
        elif ev.failed_assertion_types:
            query_parts.append(f"failed assertions: {' '.join(ev.failed_assertion_types)}")
        if ev.schema_keywords:
            query_parts.append(f"schema keywords: {' '.join(ev.schema_keywords)}")
        if ev.error_type:
            query_parts.append(f"error: {ev.error_type}")

        query_text = " ".join(part.strip() for part in query_parts if part and part.strip()).strip()
        if not query_text:
            query_text = "API test execution failure"
        if len(query_text) > 4096:
            query_text = query_text[:4096]

        # 5. Resolve metadata filters
        effective_filter: Optional[Union[Any, Dict[str, Any]]] = None
        if auto_filter_operation:
            filter_dict: Dict[str, Any] = {}
            if filter is not None:
                if isinstance(filter, dict):
                    filter_dict = dict(filter)
                elif hasattr(filter, "model_dump"):
                    filter_dict = filter.model_dump(exclude_unset=True)
            if method and method != "UNKNOWN" and "http_method" not in filter_dict:
                filter_dict["http_method"] = method
            if path_template and "path_template" not in filter_dict:
                filter_dict["path_template"] = path_template
            if op_id and "operation_id" not in filter_dict:
                filter_dict["operation_id"] = op_id
            effective_filter = filter_dict if filter_dict else None
        else:
            effective_filter = filter

        # 6. Retrieve relevant knowledge using reader snapshot session
        retrieved_items: List[RetrievedEvidenceItem] = []
        source_refs: List[str] = []

        if store is not None:
            managed_session = None
            try:
                active_session = None
                if session is not None:
                    active_session = session
                elif hasattr(store, "acquire_query_session"):
                    managed_session = store.acquire_query_session()
                    active_session = managed_session

                try:
                    results = []
                    if active_session is not None:
                        results = store.retrieve(
                            query_text=query_text,
                            top_k=top_k,
                            filter=effective_filter,
                            source_type=source_type,
                            session=active_session,
                        )
                    else:
                        results = store.retrieve(
                            query_text=query_text,
                            top_k=top_k,
                            filter=effective_filter,
                            source_type=source_type,
                        )

                    for item in results:
                        chunk = getattr(item, "chunk", item)
                        score = getattr(item, "score", 0.0)
                        distance = getattr(item, "distance", 0.0)

                        st_val = getattr(chunk, "source_type", "openapi_spec")
                        if hasattr(st_val, "value"):
                            st_val = st_val.value
                        else:
                            st_val = str(st_val)

                        cid = getattr(chunk, "chunk_id", "")
                        did = getattr(chunk, "document_id", "")
                        sref = getattr(chunk, "source_ref", None)
                        content = getattr(chunk, "content", "")
                        chunk_method = getattr(chunk, "http_method", None)
                        chunk_path = getattr(chunk, "path_template", None)
                        chunk_op = getattr(chunk, "operation_id", None)

                        clamped_score = max(0.0, min(1.0, float(score)))
                        clamped_distance = max(0.0, float(distance))

                        evidence_item = RetrievedEvidenceItem(
                            chunk_id=cid,
                            document_id=did,
                            source_type=st_val,
                            source_ref=sref,
                            relevance_score=clamped_score,
                            distance=clamped_distance,
                            content=content,
                            http_method=chunk_method,
                            path_template=chunk_path,
                            operation_id=chunk_op,
                            is_untrusted_evidence=True,
                        )
                        retrieved_items.append(evidence_item)

                        ref_key = sref or f"{st_val}:{did}"
                        if ref_key not in source_refs:
                            source_refs.append(ref_key)

                finally:
                    if managed_session is not None and getattr(managed_session, "is_active", False):
                        try:
                            managed_session.close()
                        except Exception as close_exc:
                            logger.warning("Error releasing query session in failure investigation: %s", close_exc)
            except Exception as retrieve_exc:
                # Graceful fallback: If retrieval fails, fall back to deterministic classification and observed facts
                logger.warning("Retrieval failed during failure investigation; falling back to deterministic facts: %s", retrieve_exc)
                retrieved_items = []
                source_refs = []

        # 7. Generate rule-based explanations and inferences (independent of retrieved text execution)
        inferences: List[str] = []
        all_cats = ([classification.primary_category] if classification.primary_category else []) + list(classification.secondary_categories)
        status_code = ev.status_code

        if FailureCategory.AUTHENTICATION_FAILURE in all_cats or status_code == 401:
            inferences.append("Observed HTTP 401 Unauthorized indicates missing, invalid, or expired authentication credentials.")
        elif FailureCategory.AUTHORIZATION_FAILURE in all_cats or status_code == 403:
            inferences.append("Observed HTTP 403 Forbidden indicates caller lacks necessary scopes or role permissions for this resource.")
        elif FailureCategory.RESOURCE_NOT_FOUND in all_cats or status_code == 404:
            inferences.append("Observed HTTP 404 Not Found indicates the requested endpoint URL or target resource ID does not exist.")
        elif FailureCategory.REQUEST_VALIDATION_FAILURE in all_cats or status_code in (400, 422):
            inferences.append("Observed request validation failure (HTTP 400/422); request payload, query, or path parameters violate expected constraints.")
        elif FailureCategory.RATE_LIMITING in all_cats or status_code == 429:
            inferences.append("Observed HTTP 429 Too Many Requests; rate quota exceeded for this endpoint.")
        elif FailureCategory.SERVER_ERROR in all_cats or (status_code and status_code >= 500):
            inferences.append(f"Observed HTTP {status_code or 500} server defect; internal server error occurred processing the request.")
        elif FailureCategory.RESPONSE_SCHEMA_MISMATCH in all_cats:
            paths_str = ", ".join(ev.json_paths) if ev.json_paths else "root"
            keywords_str = ", ".join(ev.schema_keywords) if ev.schema_keywords else "schema validation"
            inferences.append(f"API response body violates documented schema at '{paths_str}' (failed keyword: {keywords_str}).")
        elif FailureCategory.RESPONSE_CONTENT_TYPE_MISMATCH in all_cats:
            inferences.append(f"Response Content-Type '{ev.observed_content_type}' does not match documented contract ({ev.expected_content_types}).")
        elif FailureCategory.REQUEST_TIMEOUT in all_cats:
            inferences.append("Request timed out before the remote server returned a response.")
        elif FailureCategory.SSRF_SECURITY_BLOCK in all_cats:
            inferences.append("Destination IP or hostname was blocked by SSRF egress security policy.")
        elif any(c in all_cats for c in (FailureCategory.NETWORK_FAILURE, FailureCategory.TRANSPORT_FAILURE)):
            inferences.append("Transport-level connection error prevented completing the HTTP transaction.")
        elif FailureCategory.SPEC_OR_OPERATION_FAILURE in all_cats:
            inferences.append("Specification parsing or operation resolution failed during pipeline setup.")
        elif FailureCategory.REQUEST_PREPARATION_FAILURE in all_cats:
            inferences.append("Request envelope preparation failed due to parameter binding or schema constraints.")
        elif FailureCategory.EVALUATION_INCOMPLETE in all_cats:
            inferences.append("Contract evaluation could not be completed for the executed transaction.")
        else:
            inferences.append(f"Execution failed with primary category: {classification.primary_category.value if classification.primary_category else 'unknown'}.")

        if classification.primary_category == FailureCategory.UNEXPECTED_HTTP_STATUS and status_code:
            inferences.append(f"HTTP status code {status_code} was unexpected and not declared in the API specification ({ev.expected_status_codes}).")

        if retrieved_items:
            inferences.append(
                f"Retrieved {len(retrieved_items)} relevant documentation excerpt(s) from {', '.join(source_refs[:3])} for operational context."
            )
        else:
            inferences.append(
                "No matching knowledge store documentation was retrieved; diagnostic analysis is based strictly on observed execution facts."
            )

        # 8. Return structured FailureInvestigation
        return FailureInvestigation(
            classification=classification,
            observed_facts=observed_facts,
            retrieved_evidence=retrieved_items,
            inferences=inferences,
            source_references=source_refs,
        )


def investigate_failure(
    state: Union[AgentState, Dict[str, Any]],
    store: Optional[Any] = None,
    top_k: int = 5,
    filter: Optional[Union[Any, Dict[str, Any]]] = None,
    source_type: Optional[Union[KnowledgeSourceType, str]] = None,
    session: Optional[Any] = None,
    auto_filter_operation: bool = False,
) -> FailureInvestigation:
    """Convenience functional wrapper forwarding to FailureInvestigator.investigate (Phase 4B.5)."""
    return FailureInvestigator.investigate(
        state=state,
        store=store,
        top_k=top_k,
        filter=filter,
        source_type=source_type,
        session=session,
        auto_filter_operation=auto_filter_operation,
    )

