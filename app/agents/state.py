"""
LangGraph State Schema for AI Contact Center OS

This module defines the state schema for LangGraph workflow orchestration.
Provides both TypedDict (for LangGraph) and Pydantic (for validation) versions
with conversion functions.
"""
from typing import TypedDict, Optional, List, Dict, Any
from typing_extensions import NotRequired



# ============================================================================
# TypedDict Definitions (for LangGraph)
# ============================================================================

class SearchResults(TypedDict):
    """Search results container"""
    similar_cases: List[Dict[str, Any]]
    kb_procedures: List[Dict[str, Any]]
    total_results: int


class AgentState(TypedDict):
    """
    LangGraph workflow state.

    This TypedDict is used by LangGraph for state management across nodes.
    All fields are optional (NotRequired) to allow partial state updates.
    """
    # Input Context
    ticket_context: NotRequired[Optional[Dict[str, Any]]]
    tenant_config: NotRequired[Optional[Dict[str, Any]]]
    
    # Search Results
    search_results: NotRequired[Optional[SearchResults]]
    
    # Analysis Results
    analysis_result: NotRequired[Optional[Dict[str, Any]]]  # intent, sentiment, summary
    
    # Proposed Actions
    proposed_action: NotRequired[Optional[Dict[str, Any]]]  # draft_response, field_updates
    
    # Approval Status
    approval_status: NotRequired[Optional[str]]  # approved, modified, rejected
    
    # Metadata & Errors
    errors: NotRequired[List[str]]
    metadata: NotRequired[Dict[str, Any]]
    next_node: NotRequired[str]
