"""
LangGraph Synthesizer Agent
Combines retrieval and analysis results into final proposal
"""
import logging
from app.agents.state import AgentState
from app.services.llm_adapter import LLMAdapter

logger = logging.getLogger(__name__)


async def synthesize_results(state: AgentState) -> AgentState:
    """
    Synthesize retrieval and analysis results into a final proposal.

    This node runs after parallel execution of retrieve and analyze.
    It combines their outputs and generates the final draft response.
    """
    try:
        logger.info("Synthesizing results")

        ticket_context = state.get("ticket_context", {})
        search_results = state.get("search_results", {})
        analysis_result = state.get("analysis_result", {})
        metadata = state.get("metadata", {})

        # If we have Gemini's synthesized response, include it
        gemini_response = metadata.get("gemini_response")
        if gemini_response:
            analysis_result["gemini_suggestion"] = gemini_response

        # Generate final proposal using LLM
        llm_adapter = LLMAdapter()
        proposal = await llm_adapter.propose_solution(
            ticket_context,
            search_results,
            analysis_result
        )

        # Preserve field_proposals from analysis if not in proposal
        if "field_proposals" not in proposal and "field_proposals" in analysis_result:
            proposal["field_proposals"] = analysis_result["field_proposals"]

        # Add analysis metadata to proposal
        proposal["summary"] = analysis_result.get("summary")
        proposal["intent"] = analysis_result.get("intent")
        proposal["sentiment"] = analysis_result.get("sentiment")
        proposal["key_entities"] = analysis_result.get("key_entities")

        # Set confidence level based on available data
        # - 기존에는 검색 결과(total_results)가 없으면 'medium'으로 내려갔는데,
        #   현재 운영/테스트 환경에서 검색을 생략하거나(성능) 결과가 0일 수 있어도
        #   분석(intent 등)이 성공하면 충분히 신뢰 가능한 제안으로 취급한다.
        has_analysis = bool(analysis_result and analysis_result.get("intent"))
        has_search = bool(search_results and search_results.get("total_results", 0) > 0)

        if has_analysis:
            proposal["confidence"] = "high"
            proposal["mode"] = "synthesis" if has_search else "direct"
        else:
            proposal["confidence"] = "low"
            proposal["mode"] = "fallback"

        state["proposed_action"] = proposal
        logger.info(f"Synthesis complete (confidence: {proposal['confidence']})")

        return state

    except Exception as e:
        logger.error(f"Synthesis failed: {e}")
        if "errors" not in state:
            state["errors"] = []
        state["errors"].append(f"Synthesis error: {str(e)}")

        # Create fallback proposal
        state["proposed_action"] = {
            "cause": "",
            "solution": "",
            "field_updates": {},
            "field_proposals": state.get("analysis_result", {}).get("field_proposals", []),
            "confidence": "low",
            "mode": "fallback",
            "reasoning": f"Synthesis failed: {str(e)}"
        }

        return state
