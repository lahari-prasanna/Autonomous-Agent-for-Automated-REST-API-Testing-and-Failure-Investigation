"""
LangGraph Assembly and Conditional Routing (Phase 3C).
Wires the five deterministic individual nodes into an executable StateGraph:
  START -> load_spec -> select_operation -> prepare_request -> execute_request -> evaluate_response -> END
With explicit conditional branching terminating any failure at END.
"""

from typing import Dict
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.app.agent.nodes import (
    evaluate_response_node,
    execute_request_node,
    load_spec_node,
    prepare_request_node,
    select_operation_node,
)
from backend.app.agent.state import AgentState


# ---------------------------------------------------------------------------
# Explicit Conditional Routing Functions
# ---------------------------------------------------------------------------

def route_after_load_spec(state: AgentState) -> str:
    """
    Routes after load_spec_node:
    - If status == 'spec_loaded', proceed to 'select_operation'.
    - On any error or unexpected status, terminate immediately at END.
    """
    if state.get("status") == "spec_loaded":
        return "select_operation"
    return END


def route_after_select_operation(state: AgentState) -> str:
    """
    Routes after select_operation_node:
    - If status == 'operation_selected', proceed to 'prepare_request'.
    - On any error or missing operation, terminate immediately at END.
    """
    if state.get("status") == "operation_selected":
        return "prepare_request"
    return END


def route_after_prepare_request(state: AgentState) -> str:
    """
    Routes after prepare_request_node:
    - If status == 'request_ready', proceed to 'execute_request'.
    - On any error or missing configuration, terminate immediately at END.
    """
    if state.get("status") == "request_ready":
        return "execute_request"
    return END


def route_after_execute_request(state: AgentState) -> str:
    """
    Routes after execute_request_node:
    - If status == 'error', terminate at END.
    - If execution timed out (is_timeout), suffered network error, or did not complete,
      terminate immediately at END without evaluating assertions.
    - If execution completed a valid HTTP exchange, proceed to 'evaluate_response'.
    """
    if state.get("status") == "error":
        return END

    exec_result = state.get("execution_result")
    if not exec_result:
        return END

    # Timeouts, network errors, or incomplete exchanges terminate without evaluating
    if exec_result.is_timeout or exec_result.is_network_error or not exec_result.is_success:
        return END

    if state.get("status") == "executed":
        return "evaluate_response"

    return END


def route_after_evaluate_response(state: AgentState) -> str:
    """
    Routes after evaluate_response_node:
    - Concludes workflow at END, preserving assertion reports (both passed and failed).
    """
    return END


# ---------------------------------------------------------------------------
# StateGraph Assembly & Public Interfaces
# ---------------------------------------------------------------------------

def build_agent_graph() -> StateGraph:
    """
    Constructs and wires the deterministic API testing StateGraph.
    
    Graph Topology:
      START
        │
      load_spec ───────────────(error)───────────────┐
        │ (spec_loaded)                              │
      select_operation ────────(error)───────────────┤
        │ (operation_selected)                       │
      prepare_request ─────────(error)───────────────┤
        │ (request_ready)                            │
      execute_request ──(timeout / error / network)──┤
        │ (executed && is_success)                   │
      evaluate_response ─────────────────────────────┤
        │ (evaluated / error)                        │
       END <─────────────────────────────────────────┘
    """
    workflow = StateGraph(AgentState)

    # 1. Register existing individual nodes
    workflow.add_node("load_spec", load_spec_node)
    workflow.add_node("select_operation", select_operation_node)
    workflow.add_node("prepare_request", prepare_request_node)
    workflow.add_node("execute_request", execute_request_node)
    workflow.add_node("evaluate_response", evaluate_response_node)

    # 2. Wire entry point
    workflow.add_edge(START, "load_spec")

    # 3. Wire explicit conditional edges
    workflow.add_conditional_edges(
        "load_spec",
        route_after_load_spec,
        {
            "select_operation": "select_operation",
            END: END,
        },
    )

    workflow.add_conditional_edges(
        "select_operation",
        route_after_select_operation,
        {
            "prepare_request": "prepare_request",
            END: END,
        },
    )

    workflow.add_conditional_edges(
        "prepare_request",
        route_after_prepare_request,
        {
            "execute_request": "execute_request",
            END: END,
        },
    )

    workflow.add_conditional_edges(
        "execute_request",
        route_after_execute_request,
        {
            "evaluate_response": "evaluate_response",
            END: END,
        },
    )

    workflow.add_conditional_edges(
        "evaluate_response",
        route_after_evaluate_response,
        {
            END: END,
        },
    )

    return workflow


def compile_agent_graph() -> CompiledStateGraph:
    """Compiles the StateGraph into an executable LangGraph instance."""
    graph = build_agent_graph()
    return graph.compile()


# Ready-to-use compiled graph instance
agent_graph: CompiledStateGraph = compile_agent_graph()

