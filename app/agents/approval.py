"""
LangGraph Approval Agent
Handles approval logic and logging
"""
import logging
from typing import Literal
from app.agents.state import AgentState

logger = logging.getLogger(__name__)

async def approval_node(state: AgentState) -> AgentState:
    """
    Handle approval status.
    In a real scenario, this would pause the graph or check external status.
    For now, we simulate auto-approval or check if status is already set.
    """
    logger.info("Checking approval status")
    
    # If status is not set, default to 'pending' or 'approved' for MVP
    if not state.get("approval_status"):
        # MVP: Auto-approve for now to test flow
        state["approval_status"] = "approved"
        logger.info("Auto-approved (MVP)")
        
    return state

def approval_condition(state: AgentState) -> Literal["end", "resolve"]:
    """
    Determine next step based on approval status.
    """
    status = state.get("approval_status")
    
    if status == "modified":
        return "resolve" # Go back to resolve/refine
    elif status == "rejected":
        return "end"
    else: # approved
        return "end"
