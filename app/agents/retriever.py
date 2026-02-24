"""
LangGraph Retriever Agent
Retrieves similar cases and KB articles using Gemini File Search
"""
import logging
from typing import Any

from app.agents.state import AgentState
from app.services.gemini_file_search_client import GeminiFileSearchClient
from app.core.config import get_settings

logger = logging.getLogger(__name__)

async def retrieve_context(state: AgentState) -> AgentState:
    """
    Retrieve similar cases and KB articles using Gemini File Search.
    """
    try:
        logger.info("Retrieving context via Gemini")
        settings = get_settings()
        
        # Initialize Gemini Client (should be injected ideally)
        if not settings.gemini_api_key:
            raise ValueError("Gemini API Key is missing")
            
        gemini_client = GeminiFileSearchClient(
            api_key=settings.gemini_api_key,
            primary_model=settings.gemini_primary_model
        )
        
        ticket_context = state.get("ticket_context") or {}
        query = f"{ticket_context.get('subject', '')} {ticket_context.get('description', '')}"
        
        if not query.strip():
            logger.warning("Empty query, skipping retrieval")
            return state

        # Determine stores to search
        store_names = []
        if settings.gemini_store_tickets:
            store_names.append(settings.gemini_store_tickets)
        if settings.gemini_store_articles:
            store_names.append(settings.gemini_store_articles)
        if settings.gemini_store_common:
            store_names.append(settings.gemini_store_common)

        if not store_names:
            logger.warning("No Gemini stores configured")
            return state

        # --- TEMPORARY: Skip Gemini Search to avoid FDK Timeout ---
        logger.info("Skipping Gemini Search for performance testing")
        state["metadata"] = {"gemini_response": "This is a mock response to verify connectivity. The actual search takes too long for the FDK proxy timeout."}
        state["search_results"] = {"similar_cases": [], "kb_procedures": [], "total_results": 0}
        return state
        # ----------------------------------------------------------

        # Execute Search
        search_result = await gemini_client.search(
            query=query,
            store_names=store_names
        )
        
        # Parse Results (Gemini returns text + grounding chunks)
        # We map this to our SearchResults structure
        # Note: Gemini File Search returns synthesized text, not raw documents list directly in the same way as vector DB
        # We will use the grounding chunks as "similar cases/kb"
        
        # This is a simplification. In a real scenario, we might parse chunks or use metadata
        # For now, we store the full text result in metadata and chunks in search_results
        
        # Cast to Any to bypass TypedDict strict check for now, or define proper structure
        results_dict: Any = {
            "similar_cases": [], 
            "kb_procedures": [],
            "total_results": len(search_result.get("grounding_chunks", [])),
        }
        state["search_results"] = results_dict
        
        # Store Gemini synthesized text in metadata for Resolver to use
        if "metadata" not in state:
            state["metadata"] = {}
        state["metadata"]["gemini_response"] = search_result.get("text")
        
        logger.info("Context retrieval complete")
        return state

    except Exception as e:
        logger.error(f"Retrieval failed: {e}")
        if "errors" not in state:
            state["errors"] = []
        state["errors"].append(f"Retrieval error: {str(e)}")
        return state
