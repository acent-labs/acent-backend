from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import List, Optional, Tuple

from functools import lru_cache

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.models.analyzer import AnalyzerResult
from app.models.metadata import MetadataFilter
from app.models.session import ChatRequest
from app.services.gemini_client import GeminiClient, GeminiClientError
from app.services.query_filter_analyzer import QueryFilterAnalyzer, get_query_filter_analyzer
from app.services.freshdesk_search_service import (
    FreshdeskSearchService,
    get_freshdesk_search_service,
)


class TicketChatHandler:
    def __init__(
        self,
        *,
        gemini_client: GeminiClient,
        analyzer: QueryFilterAnalyzer,
        ticket_store_names: List[str],
        search_service: Optional[FreshdeskSearchService] = None,
    ) -> None:
        self.gemini_client = gemini_client
        self.analyzer = analyzer
        self.ticket_store_names = ticket_store_names
        self.search_service = search_service

    def can_handle(self, request: ChatRequest) -> bool:
        if not self.ticket_store_names or not self.analyzer.llm_client:
            return False
        sources = request.sources or []
        if not sources:
            return False
        return all(source in self.ticket_store_names for source in sources)

    async def handle(
        self,
        request: ChatRequest,
        *,
        history: List[str],
        clarification_state: Optional[dict] = None,
    ) -> Tuple[dict, AnalyzerResult]:
        analyzer_result = await self.analyzer.analyze(
            request.query,
            clarification_option=request.clarification_option,
            clarification_state=clarification_state,
        )
        freshdesk_tickets = []
        search_plan = None
        if self.search_service:
            search_result = await self.search_service.search_with_filters(analyzer_result)
            freshdesk_tickets = search_result.tickets
            search_plan = search_result.plan
            if search_result.ticket_ids:
                ticket_filter = MetadataFilter(
                    key="sourceId",
                    operator="IN",
                    value=",".join(str(tid) for tid in search_result.ticket_ids[:20]),
                )
                analyzer_result.filters.append(ticket_filter)
                analyzer_result.summaries.append(f"티켓ID={len(search_result.ticket_ids)}개")
        store_names = request.sources or self.ticket_store_names
        try:
            gemini_response = await asyncio.to_thread(
                self.gemini_client.search,
                query=request.query,
                store_names=store_names,
                metadata_filters=analyzer_result.filters,
                conversation_history=history,
            )
        except GeminiClientError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        payload = {
            "text": gemini_response.get("text", "검색 결과를 가져오지 못했습니다."),
            "groundingChunks": gemini_response.get("grounding_chunks", []),
            "ragStoreName": store_names[0] if store_names else None,
            "sources": gemini_response.get("store_names", store_names),
            "filters": analyzer_result.summaries,
            "filterConfidence": analyzer_result.confidence,
            "clarificationNeeded": analyzer_result.clarification_needed,
            "clarification": asdict(analyzer_result.clarification) if analyzer_result.clarification else None,
            "freshdeskTickets": freshdesk_tickets,
            "freshdeskSearchPlan": search_plan,
        }
        return payload, analyzer_result


@lru_cache
def get_ticket_chat_handler() -> Optional[TicketChatHandler]:
    settings = get_settings()
    analyzer = get_query_filter_analyzer()
    if not analyzer or not analyzer.llm_client:
        return None
    store_names = [settings.gemini_store_tickets] if settings.gemini_store_tickets else []
    if not store_names:
        return None
    if not settings.gemini_api_key:
        return None
    search_service = get_freshdesk_search_service()
    client = GeminiClient(
        api_key=settings.gemini_api_key,
        primary_model=settings.gemini_primary_model,
        fallback_model=settings.gemini_fallback_model,
    )
    return TicketChatHandler(
        gemini_client=client,
        analyzer=analyzer,
        ticket_store_names=store_names,
        search_service=search_service,
    )
