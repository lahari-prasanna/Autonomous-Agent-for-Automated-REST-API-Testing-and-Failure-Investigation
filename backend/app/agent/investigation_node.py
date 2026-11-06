"""
Additive LangGraph Node for Evidence-Supported Failure Investigation (Phase 4B.5).

Provides an optional, modular graph node function that evaluates execution outcomes
and assertion reports, retrieves relevant knowledge from the store with snapshot
isolation, and produces an evidence-supported FailureInvestigation without modifying
underlying deterministic classification or core graph topology.
"""

from typing import Any, Dict, Optional, Union

from backend.app.agent.state import AgentError, AgentState
from backend.app.services.failure_investigator import FailureInvestigator


def investigate_failure_node(
    state: Union[AgentState, Dict[str, Any]],
    store: Optional[Any] = None,
    top_k: int = 5,
    filter: Optional[Union[Any, Dict[str, Any]]] = None,
    source_type: Optional[Any] = None,
    auto_filter_operation: bool = False,
) -> Dict[str, Any]:
    """
    Additive LangGraph Node: Integrates deterministic failure classification with
    RAG knowledge retrieval (Phase 4B.5).

    Evaluates execution outcomes and assertion reports, retrieves relevant
    knowledge from the store with snapshot isolation, and produces an
    evidence-supported FailureInvestigation without modifying deterministic
    classification or altering the immutable Phase 3 core graph.

    Returns partial state update with 'investigation' and log entries.
    """
    try:
        active_store = (
            store
            or (state.get("knowledge_store") if isinstance(state, dict) else None)
            or (
                state.get("metadata", {}).get("knowledge_store")
                if isinstance(state, dict) and isinstance(state.get("metadata"), dict)
                else None
            )
        )

        investigation = FailureInvestigator.investigate(
            state=state,
            store=active_store,
            top_k=top_k,
            filter=filter,
            source_type=source_type,
            auto_filter_operation=auto_filter_operation,
        )

        logs = [
            f"Failure investigation completed (has_failure={investigation.classification.has_failure}, "
            f"retrieved_evidence={len(investigation.retrieved_evidence)}): {investigation.classification.summary}"
        ]

        return {
            "investigation": investigation,
            "logs": logs,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": AgentError.from_exception(
                exc,
                step="evaluated",
                details={"step": "investigate_failure"},
            ),
            "logs": [f"Failure investigation encountered an unexpected error: {str(exc)}"],
        }

