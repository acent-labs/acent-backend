from __future__ import annotations

from dataclasses import asdict
from typing import AsyncIterator, Optional
import inspect
import logging

from fastapi import Depends, HTTPException, status

from app.middleware.tenant_auth import TenantContext
from app.models.session import ChatRequest, ChatResponse
from app.services.common_chat_handler import CommonChatHandler, get_common_chat_handler
from app.services.multitenant_chat_handler import MultitenantChatHandler, get_multitenant_chat_handler
from app.services.pipeline_client import PipelineClient, PipelineClientError, get_pipeline_client
from app.services.query_filter_analyzer import QueryFilterAnalyzer, get_query_filter_analyzer
from app.services.session_repository import SessionRepository, get_session_repository
from app.services.ticket_chat_handler import TicketChatHandler, get_ticket_chat_handler


LOGGER = logging.getLogger(__name__)


async def _maybe_await(value):
    """Await the value if needed to support sync test doubles."""
    if inspect.isawaitable(value):
        return await value
    return value


class ChatUsecase:
    def __init__(
        self,
        *,
        repository: SessionRepository,
        common_handler: Optional[CommonChatHandler],
        analyzer: Optional[QueryFilterAnalyzer],
        ticket_handler: Optional[TicketChatHandler],
        pipeline: PipelineClient,
        multitenant_handler: Optional[MultitenantChatHandler],
    ) -> None:
        self._repository = repository
        self._common_handler = common_handler
        self._analyzer = analyzer
        self._ticket_handler = ticket_handler
        self._pipeline = pipeline
        self._multitenant_handler = multitenant_handler

    async def handle_legacy_chat(self, request: ChatRequest, *, tenant: Optional[TenantContext]) -> ChatResponse:
        """
        레거시 chat 처리:
        - session이 없으면 생성(기존 동작 유지)
        - common/ticket/pipeline 순서 유지
        - tenant 헤더가 있으면 multitenant handler로 디스패치(하위호환)
        """
        session = await self._repository.get(request.session_id)
        if not session:
            session = {"sessionId": request.session_id, "conversationHistory": [], "questionHistory": []}
            await self._repository.save(session)

        conversation_history = session.get("conversationHistory", []) if isinstance(session, dict) else []

        if self._common_handler and self._common_handler.can_handle(request):
            LOGGER.info("🎯 CommonChatHandler handling request for sources: %s", request.sources)
            response = await _maybe_await(self._common_handler.handle(request, history=conversation_history))
            await self._repository.append_turn(request.session_id, request.query, response.text or "")
            return response

        if tenant is not None:
            return await self._handle_multitenant_chat(
                request,
                tenant=tenant,
                conversation_history=conversation_history,
                ensure_session_exists=False,  # legacy endpoint already created session above
            )

        history_texts: list[str] = []
        if isinstance(session, dict):
            question_history = session.get("questionHistory", [])
            if isinstance(question_history, list):
                history_texts = [str(entry) for entry in question_history if isinstance(entry, str)]

        clarification_state = session.get("clarificationState") if isinstance(session, dict) else None
        if request.clarification_option and clarification_state and isinstance(session, dict):
            session.pop("clarificationState", None)
            await self._repository.save(session)

        if self._ticket_handler and self._ticket_handler.can_handle(request):
            LOGGER.info("🎫 TicketChatHandler handling request")
            payload, ticket_result = await _maybe_await(
                self._ticket_handler.handle(
                    request,
                    history=history_texts,
                    clarification_state=clarification_state,
                )
            )
            if ticket_result:
                await self._repository.record_analyzer_result(request.session_id, ticket_result)
            await self._repository.append_question(request.session_id, request.query)
            return ChatResponse.model_validate(payload)

        analyzer_result = None
        if self._analyzer:
            try:
                analyzer_result = await _maybe_await(
                    self._analyzer.analyze(
                        request.query,
                        clarification_option=request.clarification_option,
                        clarification_state=clarification_state,
                    )
                )
            except Exception:
                analyzer_result = None

        if analyzer_result:
            await self._repository.record_analyzer_result(request.session_id, analyzer_result)

        payload = request.model_dump(by_alias=True, exclude_none=True)
        try:
            pipeline_result = await _maybe_await(self._pipeline.chat(payload))
        except PipelineClientError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.details)

        await self._repository.append_question(request.session_id, request.query)

        if isinstance(pipeline_result, dict):
            pipeline_result.setdefault("sources", request.sources)

            if analyzer_result:
                pipeline_result["filters"] = analyzer_result.summaries or pipeline_result.get("filters") or []
                pipeline_result["filterConfidence"] = analyzer_result.confidence
                pipeline_result["clarificationNeeded"] = analyzer_result.clarification_needed
                pipeline_result["clarification"] = (
                    asdict(analyzer_result.clarification) if analyzer_result.clarification else None
                )
                pipeline_result["knownContext"] = analyzer_result.known_context or {}

        return ChatResponse.model_validate(pipeline_result)

    async def handle_multitenant_chat(self, request: ChatRequest, *, tenant: TenantContext) -> ChatResponse:
        """
        멀티테넌트 chat 처리:
        - session이 없어도 생성하지 않음(기존 동작 유지)
        - multitenant handler만 사용
        """
        session = await self._repository.get(request.session_id)
        conversation_history = session.get("conversationHistory", []) if isinstance(session, dict) else []
        return await self._handle_multitenant_chat(
            request,
            tenant=tenant,
            conversation_history=conversation_history,
            ensure_session_exists=False,
        )

    async def stream_legacy_chat(self, request: ChatRequest, *, tenant: Optional[TenantContext]) -> AsyncIterator[dict]:
        """
        레거시 streaming 처리:
        - session이 없으면 생성(기존 동작 유지)
        - tenant 헤더가 있으면 multitenant stream 경로로 디스패치(하위호환)
        - 그 외에는 common handler stream 유지
        - SSE 포맷 자체는 라우트에서 유지(여기서는 event/data dict만 yield)
        """
        session = await self._repository.get(request.session_id)
        if not session:
            session = {"sessionId": request.session_id, "conversationHistory": [], "questionHistory": []}
            await self._repository.save(session)

        if tenant is not None:
            if not self._multitenant_handler:
                yield {"event": "error", "data": {"message": "Chat service not available"}}
                return

            raw_history = session.get("questionHistory", []) if isinstance(session, dict) else []
            history_texts = [str(entry) for entry in raw_history if isinstance(entry, str)][-4:]

            response_text = ""
            async for event in self._multitenant_handler.stream_handle(
                request,
                tenant,
                history=history_texts,
            ):
                yield event
                if event.get("event") == "result":
                    response_text = (event.get("data") or {}).get("text", "") or ""
                    await self._repository.append_turn(request.session_id, request.query, response_text)
            return

        if not self._common_handler or not self._common_handler.can_handle(request):
            yield {"event": "error", "data": {"message": f"지원하지 않는 검색 소스입니다: {request.sources}"}}
            return

        conversation_history = session.get("conversationHistory", []) if isinstance(session, dict) else []
        if len(conversation_history) > 4:
            conversation_history = conversation_history[-4:]

        terminal_event_sent = False
        response_text = ""
        async for event in self._common_handler.stream_handle(request, history=conversation_history):
            yield event
            if event.get("event") == "result":
                terminal_event_sent = True
                response_text = (event.get("data") or {}).get("text", "") or ""
                await self._repository.append_turn(request.session_id, request.query, response_text)
            if event.get("event") == "error":
                terminal_event_sent = True
                break
        if not terminal_event_sent:
            yield {"event": "error", "data": {"message": "잠시 후 다시 시도해 주세요."}}

    async def stream_multitenant_chat(self, request: ChatRequest, *, tenant: TenantContext) -> AsyncIterator[dict]:
        """
        멀티테넌트 streaming 처리:
        - session이 없어도 생성하지 않음(기존 동작 유지)
        - SSE 포맷 자체는 라우트에서 유지(event/data dict만 yield)
        """
        if not self._multitenant_handler:
            yield {"event": "error", "data": {"message": "Chat service not available"}}
            return

        session = await self._repository.get(request.session_id)
        history: list[str] = []
        if session and isinstance(session, dict):
            raw_history = session.get("questionHistory", [])
            history = [str(entry) for entry in raw_history if isinstance(entry, str)][-4:]

        response_text = ""
        async for event in self._multitenant_handler.stream_handle(
            request,
            tenant,
            history=history,
        ):
            yield event
            if event.get("event") == "result":
                response_text = (event.get("data") or {}).get("text", "") or ""
                await self._repository.append_turn(request.session_id, request.query, response_text)

    async def _handle_multitenant_chat(
        self,
        request: ChatRequest,
        *,
        tenant: TenantContext,
        conversation_history: list[dict],
        ensure_session_exists: bool,
    ) -> ChatResponse:
        if not self._multitenant_handler:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Chat service not available. Check Gemini API configuration.",
            )

        if ensure_session_exists:
            session = await self._repository.get(request.session_id)
            if not session:
                await self._repository.save(
                    {"sessionId": request.session_id, "conversationHistory": [], "questionHistory": []}
                )

        additional_filters = []
        analyzer_result = None
        if self._analyzer:
            analyzer_result = await _maybe_await(self._analyzer.analyze(request.query))
            if analyzer_result and getattr(analyzer_result, "filters", None):
                additional_filters = analyzer_result.filters

        response = await self._multitenant_handler.handle(
            request,
            tenant,
            history=conversation_history,
            additional_filters=additional_filters,
        )

        await self._repository.append_turn(request.session_id, request.query, response.text or "")

        if analyzer_result:
            if getattr(analyzer_result, "summaries", None) and not response.filters:
                response.filters = analyzer_result.summaries
            if getattr(analyzer_result, "confidence", None):
                response.filter_confidence = analyzer_result.confidence

        return response


async def get_chat_usecase(
    repository: SessionRepository = Depends(get_session_repository),
    common_handler: Optional[CommonChatHandler] = Depends(get_common_chat_handler),
    analyzer: Optional[QueryFilterAnalyzer] = Depends(get_query_filter_analyzer),
    ticket_handler: Optional[TicketChatHandler] = Depends(get_ticket_chat_handler),
    pipeline: PipelineClient = Depends(get_pipeline_client),
    multitenant_handler: Optional[MultitenantChatHandler] = Depends(get_multitenant_chat_handler),
) -> ChatUsecase:
    return ChatUsecase(
        repository=repository,
        common_handler=common_handler,
        analyzer=analyzer,
        ticket_handler=ticket_handler,
        pipeline=pipeline,
        multitenant_handler=multitenant_handler,
    )
