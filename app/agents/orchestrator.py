"""
LangGraph Orchestrator
Defines the workflow graph with parallel execution support

Architecture:
    START -> fan_out -> [retrieve, analyze] (parallel) -> synthesize -> END

    retrieve: KB/Similar case search (Gemini)
    analyze: Intent/sentiment/field proposals (LLM)
    synthesize: Combine results and generate final proposal
"""
import asyncio
import logging
from langgraph.graph import StateGraph, END

from app.agents.state import AgentState
from app.agents.retriever import retrieve_context
from app.agents.analyzer import analyze_ticket
from app.agents.resolver import propose_solution
from app.agents.synthesizer import synthesize_results

logger = logging.getLogger(__name__)


# =============================================================================
# Parallel Execution Nodes
# =============================================================================

async def parallel_retrieve_analyze(state: AgentState) -> AgentState:
    """
    Execute retrieve and analyze in parallel using asyncio.gather.
    This is a workaround since LangGraph doesn't natively support fan-out/fan-in.
    """
    logger.info("Starting parallel execution: retrieve + analyze")

    # Create copies of state for parallel execution
    retrieve_state = state.copy()
    analyze_state = state.copy()

    # Execute in parallel
    retrieve_result, analyze_result = await asyncio.gather(
        retrieve_context(retrieve_state),
        analyze_ticket(analyze_state),
        return_exceptions=True
    )

    # Merge results back into state
    if isinstance(retrieve_result, Exception):
        logger.error(f"Retrieve failed: {retrieve_result}")
        state.setdefault("errors", []).append(f"Retrieve error: {str(retrieve_result)}")
    else:
        state["search_results"] = retrieve_result.get("search_results")
        state["metadata"] = retrieve_result.get("metadata", {})

    if isinstance(analyze_result, Exception):
        logger.error(f"Analyze failed: {analyze_result}")
        state.setdefault("errors", []).append(f"Analyze error: {str(analyze_result)}")
    else:
        state["analysis_result"] = analyze_result.get("analysis_result")

    logger.info("Parallel execution complete")
    return state


# =============================================================================
# Graph Builders
# =============================================================================

def build_graph() -> StateGraph:
    """
    Build the LangGraph workflow with parallel execution:
    START -> parallel_retrieve_analyze -> synthesize -> END
    """
    graph = StateGraph(AgentState)

    # Add Nodes
    graph.add_node("parallel", parallel_retrieve_analyze)
    graph.add_node("synthesize", synthesize_results)

    # Define Edges
    graph.set_entry_point("parallel")
    graph.add_edge("parallel", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()


def build_sequential_graph() -> StateGraph:
    """
    Legacy sequential workflow for comparison/fallback:
    START -> Retrieve -> Analyze -> Resolve -> END
    """
    graph = StateGraph(AgentState)

    # Add Nodes
    graph.add_node("retrieve", retrieve_context)
    graph.add_node("analyze", analyze_ticket)
    graph.add_node("resolve", propose_solution)

    # Define Edges
    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "analyze")
    graph.add_edge("analyze", "resolve")
    graph.add_edge("resolve", END)

    return graph.compile()
