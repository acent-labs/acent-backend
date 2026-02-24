"""
AI Assistant API 라우트

FDK Custom App에서 호출하는 티켓 분석, 승인, 수정 엔드포인트
SSE 스트리밍을 통한 실시간 진행 상황 업데이트 지원

Streaming Architecture:
- /analyze/stream: SSE endpoint for real-time progressive rendering
- Events: started, searching, analyzing, field_proposal, draft_response, complete, error
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict, Optional, List

from fastapi import APIRouter, Depends, Header, HTTPException, status, BackgroundTasks, Query
from fastapi.responses import StreamingResponse

from app.models.assist import (
    AnalyzeRequest,
    AnalyzeResponse,
    ApproveRequest,
    ApproveResponse,
    RefineRequest,
    RefineResponse,
    Proposal,
)
from app.models.feedback import (
    FeedbackSubmitRequest,
    FeedbackSubmitResponse,
    FeedbackEditRequest,
    FeedbackEditResponse,
    TrainingExportRequest,
    TrainingExportResponse,
    TrainingSampleExport,
)
from app.services.feedback_repository import get_feedback_repository
from app.agents.orchestrator import build_graph
from app.agents.retriever import retrieve_context
from app.agents.analyzer import analyze_ticket as analyze_ticket_agent
from app.agents.synthesizer import synthesize_results
from app.repositories.proposal_repository import ProposalRepository, get_proposal_repository
from app.services.tenant_ticket_fields_cache import TenantTicketFieldsCache, get_tenant_ticket_fields_cache
from app.services.admin_service import AdminService, get_admin_service
from app.services.freshdesk_client import FreshdeskClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assist", tags=["assist"])


# =============================================================================
# Ticket Conversations Enrichment (Freshdesk per_page=30 pagination)
# =============================================================================

_CONVERSATIONS_CACHE: Dict[tuple[str, str], tuple[float, List[Dict[str, Any]]]] = {}
_CONVERSATIONS_CACHE_TTL_S = 60.0


def _normalize_conversation_for_llm(conv: Dict[str, Any]) -> Dict[str, Any]:
    """LLM 입력용으로 대화 데이터를 축약/정규화."""
    body = (conv.get("body_text") or "").strip()
    # 너무 길면 잘라냄 (토큰 폭증 방지)
    if len(body) > 2000:
        body = body[:2000] + "…"

    return {
        "body_text": body,
        "incoming": conv.get("incoming"),
        "private": conv.get("private"),
        "created_at": conv.get("created_at"),
        "user_id": conv.get("user_id"),
    }


async def _enrich_ticket_context_with_conversations(
    ticket_context: Dict[str, Any],
    *,
    freshdesk_domain: Optional[str],
    freshdesk_api_key: Optional[str],
    tenant_id: str,
) -> Dict[str, Any]:
    """ticket_context에 conversations를 추가.

    - Freshdesk conversations는 페이지당 30개 제한이 있으므로 서버에서 전체를 수집한다.
    - 동일 티켓 반복 분석 시를 위해 짧은 TTL 캐시를 둔다.
    """
    if not freshdesk_domain or not freshdesk_api_key:
        return ticket_context

    ticket_id_raw = ticket_context.get("ticketId") or ticket_context.get("ticket_id")
    if not ticket_id_raw:
        return ticket_context

    try:
        ticket_id_int = int(str(ticket_id_raw))
    except ValueError:
        return ticket_context

    cache_key = (tenant_id, str(ticket_id_int))
    now = time.time()
    t0 = time.perf_counter()
    cache_hit = False

    cached = _CONVERSATIONS_CACHE.get(cache_key)
    if cached and cached[0] > now:
        conversations = cached[1]
        cache_hit = True
    else:
        client = FreshdeskClient(domain=freshdesk_domain, api_key=freshdesk_api_key)
        try:
            conversations = await client.get_all_conversations(ticket_id_int)
        finally:
            await client.close()
        _CONVERSATIONS_CACHE[cache_key] = (now + _CONVERSATIONS_CACHE_TTL_S, conversations)

    normalized = [_normalize_conversation_for_llm(c) for c in conversations]
    out = dict(ticket_context)
    out["conversations"] = normalized

    # NOTE: This happens before SSE 'started' event in /assist/analyze/stream.
    # Keep this log compact to make perf bottlenecks obvious.
    logger.info(
        "Conversations enriched tenant=%s ticket=%s count=%s cache_hit=%s ms=%s",
        tenant_id,
        ticket_id_int,
        len(normalized),
        cache_hit,
        int((time.perf_counter() - t0) * 1000),
    )
    return out


@router.get("/ticket-fields")
async def get_freshdesk_ticket_fields(
    force_refresh: bool = Query(False, alias="forceRefresh"),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_freshdesk_domain: str = Header(..., alias="X-Freshdesk-Domain"),
    x_freshdesk_api_key: str = Header(..., alias="X-Freshdesk-API-Key"),
    cache: TenantTicketFieldsCache = Depends(get_tenant_ticket_fields_cache),
):
    """Freshdesk 티켓 필드 정의(/ticket_fields)를 캐시 기반으로 반환.

    - Supabase 캐시(hit): 빠르게 반환
    - 캐시 miss/만료 또는 forceRefresh=true: Freshdesk에서 재수집 후 Supabase에 upsert

    반환 형식은 프론트에서 바로 쓰기 쉽도록 *배열(list)* 그대로 반환합니다.
    """
    if not x_freshdesk_domain or not x_freshdesk_api_key:
        raise HTTPException(status_code=400, detail="X-Freshdesk-Domain/X-Freshdesk-API-Key headers are required")

    result = await cache.get_or_refresh_freshdesk_ticket_fields(
        tenant_slug=x_tenant_id,
        freshdesk_domain=x_freshdesk_domain,
        freshdesk_api_key=x_freshdesk_api_key,
        platform="freshdesk",
        force_refresh=force_refresh,
    )

    return result.ticket_fields

# Initialize Graph
agent_graph = build_graph()

# =============================================================================
# SSE Streaming Helper
# =============================================================================

async def sse_generator(
    events: AsyncGenerator[Dict[str, Any], None]
) -> AsyncGenerator[str, None]:
    """
    이벤트 스트림을 SSE 포맷으로 변환
    """
    last_heartbeat = time.time()

    try:
        async for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            if time.time() - last_heartbeat > 30:
                heartbeat = {"type": "heartbeat", "timestamp": time.time()}
                yield f"data: {json.dumps(heartbeat)}\n\n"
                last_heartbeat = time.time()

    except Exception as e:
        logger.error(f"SSE 스트리밍 오류: {e}")
        error_event = {
            "type": "error",
            "message": str(e),
            "recoverable": False
        }
        yield f"data: {json.dumps(error_event)}\n\n"


async def process_analysis_background(
    initial_state: Dict[str, Any],
    repo: ProposalRepository,
    tenant_id: str,
    ticket_id: str,
    proposal_id: str
):
    """Background task for analysis"""
    try:
        logger.info(f"Starting background analysis for proposal {proposal_id}")
        final_state = await agent_graph.ainvoke(initial_state)
        
        # Extract results
        proposal_data = final_state.get("proposed_action", {})
        
        # Update proposal with results
        proposal_data["id"] = proposal_id
        proposal_data["tenantId"] = tenant_id
        proposal_data["ticketId"] = ticket_id
        proposal_data["status"] = "draft" # Completed
        
        # Merge analysis/search into proposal if needed or just save as is
        # The Proposal model has fields for these? No, but we can store them in the repo
        # Actually, Proposal model has similar_cases etc.
        
        # Ensure we have a valid Proposal object structure
        # We need to map the dict to the model fields if they differ, but they should match mostly
        
        # Save completed proposal
        await repo.save_proposal(proposal_id, proposal_data)
        logger.info(f"Background analysis completed for proposal {proposal_id}")
        
    except Exception as e:
        logger.error(f"Background analysis failed: {e}")
        # Update status to error
        error_proposal = {
            "id": proposal_id,
            "tenantId": tenant_id,
            "ticketId": ticket_id,
            "status": "error",
            "rejectionReason": str(e)
        }
        await repo.save_proposal(proposal_id, error_proposal)


# =============================================================================
# Routes
# =============================================================================

@router.post("/analyze")
async def analyze_ticket(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_freshdesk_domain: Optional[str] = Header(None, alias="X-Freshdesk-Domain"),
    x_freshdesk_api_key: Optional[str] = Header(None, alias="X-Freshdesk-API-Key"),
    repo: ProposalRepository = Depends(get_proposal_repository),
    admin_service: AdminService = Depends(get_admin_service)
):
    """
    티켓 분석 및 AI 제안 생성 (LangGraph 기반)
    """
    logger.info(f"Analyze request for ticket {request.ticket_id} (Tenant: {x_tenant_id})")

    # Fetch Tenant Config
    tenant_config = await admin_service.get_tenant(x_tenant_id)
    response_tone = tenant_config.response_tone if tenant_config else "formal"
    selected_fields = tenant_config.selected_fields if tenant_config else []

    # Ticket context enrichment (include full conversations)
    ticket_context = request.model_dump(by_alias=True)

    # fieldsOnly 모드에서는 대화 전체 수집(페이지네이션)이 병목이 될 수 있어 기본적으로 생략한다.
    if not bool(ticket_context.get("fieldsOnly")):
        ticket_context = await _enrich_ticket_context_with_conversations(
            ticket_context,
            freshdesk_domain=x_freshdesk_domain,
            freshdesk_api_key=x_freshdesk_api_key,
            tenant_id=x_tenant_id,
        )

    # Initial State
    initial_state = {
        "ticket_context": ticket_context,
        "response_tone": response_tone,
        "selected_fields": selected_fields,
        "tenant_config": {
            "tenant_id": x_tenant_id,
            "domain": x_freshdesk_domain,
            "api_key": x_freshdesk_api_key
        }
    }

    if request.async_mode:
        # Create a placeholder proposal
        proposal_id = str(uuid.uuid4())
        initial_proposal = Proposal(
            id=proposal_id,
            tenantId=x_tenant_id,
            ticketId=request.ticket_id,
            status="processing",
            draftResponse="" # Optional now
        )
        await repo.save_proposal(proposal_id, initial_proposal.model_dump(by_alias=True))
        
        # Start background task
        background_tasks.add_task(
            process_analysis_background,
            initial_state,
            repo,
            x_tenant_id,
            request.ticket_id,
            proposal_id
        )
        
        return AnalyzeResponse(proposal=initial_proposal)

    if request.stream_progress:
        return StreamingResponse(
            sse_generator(run_agent_stream(initial_state, repo, x_tenant_id, request.ticket_id)),
            media_type="text/event-stream"
        )
    else:
        # Sync execution
        final_state = await agent_graph.ainvoke(initial_state)
        response = await build_analyze_response(final_state, repo, x_tenant_id, request.ticket_id)
        return response

async def run_agent_stream(
    initial_state: Dict[str, Any],
    repo: ProposalRepository,
    tenant_id: str,
    ticket_id: str
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run LangGraph and yield SSE events"""
    try:
        final_state = {}
        async for event in agent_graph.astream(initial_state):
            for node_name, state_update in event.items():
                final_state.update(state_update) # Keep track of state

                yield {
                    "type": "progress",
                    "data": {
                        "stage": node_name,
                        "message": f"Completed {node_name}..."
                    }
                }

                if node_name == "retrieve" and "search_results" in state_update:
                    yield {
                        "type": "search_results",
                        "data": state_update["search_results"]
                    }
                elif node_name == "analyze" and "analysis_result" in state_update:
                    yield {
                        "type": "analysis_result",
                        "data": state_update["analysis_result"]
                    }
                elif node_name == "resolve" and "proposed_action" in state_update:
                    # Save proposal before yielding
                    proposal_data = state_update["proposed_action"]
                    proposal_id = str(uuid.uuid4())
                    proposal_data["id"] = proposal_id
                    proposal_data["tenantId"] = tenant_id
                    proposal_data["ticketId"] = ticket_id

                    await repo.save_proposal(proposal_id, proposal_data)

                    yield {
                        "type": "proposal",
                        "data": proposal_data
                    }
                    
                    # Keep proposal data for complete event
                    final_state["proposed_action"] = proposal_data

        # Ensure we have the final proposal data
        final_proposal = final_state.get("proposed_action", {})
        if not final_proposal and "proposed_action" in final_state:
             final_proposal = final_state["proposed_action"]
             
        yield {"type": "complete", "data": final_proposal if final_proposal else {"status": "done"}}

    except Exception as e:
        logger.error(f"Agent execution failed: {e}")
        yield {"type": "error", "message": str(e)}


# =============================================================================
# Progressive SSE Streaming (New Implementation)
# =============================================================================

async def run_progressive_stream(
    initial_state: Dict[str, Any],
    repo: ProposalRepository,
    tenant_id: str,
    ticket_id: str
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Progressive SSE streaming with individual field proposals.

    Event flow:
    1. started (proposal_id)
    2. searching (progress)
    3. search_result (if available)
    4. analyzing (progress)
    5. field_proposal (per field, streamed individually)
    6. draft_response (chunked or full)
    7. complete (full proposal)
    """
    proposal_id = str(uuid.uuid4())

    t0 = time.perf_counter()
    t_retrieve_done: Optional[float] = None
    t_analyze_done: Optional[float] = None
    t_synthesize_done: Optional[float] = None

    try:
        # 1. Started event
        yield {
            "type": "started",
            "data": {"proposalId": proposal_id, "ticketId": ticket_id}
        }

        state = initial_state.copy()

        # Frontend can request fields-only mode to skip expensive synthesis.
        ticket_ctx = state.get("ticket_context") or {}
        fields_only = bool(ticket_ctx.get("fieldsOnly") or state.get("fields_only"))

        # 2. Execution
        # - fieldsOnly: 필드 제안만 필요하므로 검색(retrieve) 자체를 생략한다.
        # - normal: retrieve + analyze 병렬 실행

        analyze_task = asyncio.create_task(analyze_ticket_agent(state.copy()))

        if not fields_only:
            retrieve_task = asyncio.create_task(retrieve_context(state.copy()))

            # Yield searching status
            yield {
                "type": "searching",
                "data": {"message": "관련 문서 검색 중..."}
            }

            # Wait for retrieve to complete first
            retrieve_result = await retrieve_task
            t_retrieve_done = time.perf_counter()

            # Yield search results
            search_results = retrieve_result.get("search_results", {})
            state["search_results"] = search_results
            state["metadata"] = retrieve_result.get("metadata", {})

            yield {
                "type": "search_result",
                "data": {
                    "similarCases": search_results.get("similar_cases", []),
                    "kbArticles": search_results.get("kb_procedures", []),
                    "totalResults": search_results.get("total_results", 0)
                }
            }
        else:
            # Emit an empty search result for shape compatibility
            search_results = {}
            state["search_results"] = search_results
            t_retrieve_done = t0

        # Yield analyzing status
        yield {
            "type": "analyzing",
            "data": {"message": "티켓 분석 중..."}
        }

        # Wait for analyze to complete
        analyze_result = await analyze_task
        t_analyze_done = time.perf_counter()
        analysis = analyze_result.get("analysis_result", {})
        state["analysis_result"] = analysis

        # 3. Stream field proposals individually
        field_proposals = analysis.get("field_proposals", [])
        for proposal in field_proposals:
            yield {
                "type": "field_proposal",
                "data": {
                    "fieldName": proposal.get("field_name"),
                    "fieldLabel": proposal.get("field_label"),
                    "proposedValue": proposal.get("proposed_value"),
                    "reason": proposal.get("reason")
                }
            }
            # Small delay for visual effect (optional)
            await asyncio.sleep(0.1)

        # 4. Synthesize final proposal (optional)
        yield {
            "type": "synthesizing",
            "data": {"message": "최종 정리 중..." if fields_only else "응답 생성 중..."}
        }

        proposal_data: Dict[str, Any]
        if fields_only:
            # Fast path: skip LLM synthesis stage. Keep shape compatible.
            proposal_data = {
                "draft_response": "",
                "field_updates": {},
                "field_proposals": field_proposals,
                "confidence": "medium" if analysis else "low",
                "mode": "fields_only",
                "summary": analysis.get("summary"),
                "intent": analysis.get("intent"),
                "sentiment": analysis.get("sentiment"),
                "key_entities": analysis.get("key_entities"),
                "reasoning": "fieldsOnly=true (synthesis skipped)"
            }
        else:
            synthesized = await synthesize_results(state)
            t_synthesize_done = time.perf_counter()
            proposal_data = synthesized.get("proposed_action", {})

            # 5. Stream draft response
            draft_response = proposal_data.get("draft_response", "")
            if draft_response:
                yield {
                    "type": "draft_response",
                    "data": {"text": draft_response}
                }

        # 6. Finalize and save proposal
        proposal_data["id"] = proposal_id
        proposal_data["tenantId"] = tenant_id
        proposal_data["ticketId"] = ticket_id
        proposal_data["status"] = "draft"

        # Attach timing info
        now = time.perf_counter()
        timing_ms = {
            "total": int((now - t0) * 1000),
            "retrieve": int(((t_retrieve_done or now) - t0) * 1000) if t_retrieve_done else None,
            "analyze": int(((t_analyze_done or now) - (t_retrieve_done or t0)) * 1000) if t_analyze_done else None,
            "synthesize": int(((t_synthesize_done or now) - (t_analyze_done or t0)) * 1000) if (not fields_only and t_synthesize_done) else 0,
        }
        proposal_data["analysisTimeMs"] = timing_ms.get("total")

        await repo.save_proposal(proposal_id, proposal_data)

        # 7. Complete event with full proposal
        yield {
            "type": "complete",
            "data": {
                "proposal": proposal_data,
                "analysis": analysis,
                "search": search_results,
                "timingMs": timing_ms
            }
        }

        logger.info(
            "Progressive stream done (fields_only=%s) ticket=%s timing_ms=%s",
            fields_only,
            ticket_id,
            timing_ms,
        )

    except Exception as e:
        logger.error(f"Progressive stream failed: {e}", exc_info=True)
        yield {
            "type": "error",
            "data": {
                "message": str(e),
                "proposalId": proposal_id,
                "recoverable": False
            }
        }


@router.post("/analyze/stream")
async def analyze_ticket_stream(
    request: AnalyzeRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_freshdesk_domain: Optional[str] = Header(None, alias="X-Freshdesk-Domain"),
    x_freshdesk_api_key: Optional[str] = Header(None, alias="X-Freshdesk-API-Key"),
    repo: ProposalRepository = Depends(get_proposal_repository),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    티켓 분석 SSE 스트리밍 엔드포인트

    실시간으로 분석 진행 상황과 필드 제안을 스트리밍합니다.
    각 필드 제안이 도착하는 대로 프론트엔드에서 즉시 렌더링할 수 있습니다.
    """
    logger.info(f"Stream analyze request for ticket {request.ticket_id} (Tenant: {x_tenant_id})")

    ticket_context = request.model_dump(by_alias=True)

    # fieldsOnly 모드에서는 대화 전체 수집(페이지네이션)이 병목이 될 수 있어 기본적으로 생략한다.
    if not bool(ticket_context.get("fieldsOnly")):
        ticket_context = await _enrich_ticket_context_with_conversations(
            ticket_context,
            freshdesk_domain=x_freshdesk_domain,
            freshdesk_api_key=x_freshdesk_api_key,
            tenant_id=x_tenant_id,
        )

    initial_state = {
        "ticket_context": ticket_context,
        "tenant_config": {
            "tenant_id": x_tenant_id,
            "domain": x_freshdesk_domain,
            "api_key": x_freshdesk_api_key
        },
    }

    # 테넌트 설정(관리자에서 지정한 selected_fields/response_tone)을 주입
    tenant = await admin_service.get_tenant(x_tenant_id)
    if tenant:
        initial_state["response_tone"] = tenant.response_tone or "formal"
        initial_state["selected_fields"] = tenant.selected_fields or []

    return StreamingResponse(
        sse_generator(run_progressive_stream(initial_state, repo, x_tenant_id, request.ticket_id)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable nginx buffering
        }
    )


@router.post("/field-proposals")
async def propose_fields_only(
    request: AnalyzeRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """필드 제안만 생성하는 경량 엔드포인트.

    - LLM 1회(필드 제안 전용)만 호출
    - conversations 수집/검색/합성 단계 생략
    """
    t0 = time.perf_counter()
    ticket_context = request.model_dump(by_alias=True)
    ticket_context["fieldsOnly"] = True

    # Directly run analyzer (no graph)
    state: Dict[str, Any] = {"ticket_context": ticket_context, "fields_only": True}

    tenant = await admin_service.get_tenant(x_tenant_id)
    if tenant:
        state["response_tone"] = tenant.response_tone or "formal"
        state["selected_fields"] = tenant.selected_fields or []

    analyzed = await analyze_ticket_agent(state)
    analysis = analyzed.get("analysis_result", {})

    field_proposals = analysis.get("field_proposals", []) if isinstance(analysis, dict) else []

    return {
        "fieldProposals": field_proposals,
        "analysis": analysis,
        "timingMs": {
            "total": int((time.perf_counter() - t0) * 1000),
        },
    }


@router.post("/field-proposals/stream")
async def propose_fields_only_stream(
    request: AnalyzeRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_freshdesk_domain: Optional[str] = Header(None, alias="X-Freshdesk-Domain"),
    x_freshdesk_api_key: Optional[str] = Header(None, alias="X-Freshdesk-API-Key"),
    repo: ProposalRepository = Depends(get_proposal_repository),
    admin_service: AdminService = Depends(get_admin_service),
):
    """필드 제안만 스트리밍하는 전용 SSE 엔드포인트.

    - fieldsOnly=true 강제
    - conversations 수집 기본 생략(병목 방지)
    - retrieve/synthesize 생략(run_progressive_stream 내부 최적화 적용)
    """
    logger.info(f"Stream field-proposals request for ticket {request.ticket_id} (Tenant: {x_tenant_id})")

    ticket_context = request.model_dump(by_alias=True)
    ticket_context["fieldsOnly"] = True

    initial_state = {
        "ticket_context": ticket_context,
        "tenant_config": {
            "tenant_id": x_tenant_id,
            "domain": x_freshdesk_domain,
            "api_key": x_freshdesk_api_key,
        },
        "fields_only": True,
    }

    tenant = await admin_service.get_tenant(x_tenant_id)
    if tenant:
        initial_state["response_tone"] = tenant.response_tone or "formal"
        initial_state["selected_fields"] = tenant.selected_fields or []

    return StreamingResponse(
        sse_generator(run_progressive_stream(initial_state, repo, x_tenant_id, request.ticket_id)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

async def build_analyze_response(
    state: Dict[str, Any],
    repo: ProposalRepository,
    tenant_id: str,
    ticket_id: str
) -> Dict[str, Any]:
    """Build JSON response from final state"""
    proposal = state.get("proposed_action", {})
    analysis = state.get("analysis_result", {})
    search = state.get("search_results", {})
    
    if proposal:
        proposal_id = str(uuid.uuid4())
        proposal["id"] = proposal_id
        proposal["tenantId"] = tenant_id
        proposal["ticketId"] = ticket_id
        await repo.save_proposal(proposal_id, proposal)
    
    return {
        "proposal": proposal,
        "analysis": analysis,
        "search": search
    }

@router.post("/approve", response_model=ApproveResponse)
async def approve_proposal(
    request: ApproveRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    repo: ProposalRepository = Depends(get_proposal_repository),
):
    """
    제안 승인 또는 거절
    """
    try:
        proposal = await repo.get_proposal(request.proposal_id)
        if not proposal:
            raise HTTPException(status_code=404, detail="Proposal not found")
            
        # Logic to handle approval (e.g., update Freshdesk, log to Supabase)
        # For MVP, we just return success
        
        status_str = "approved" if request.action == "approve" else "rejected"
        
        return ApproveResponse(
            status=status_str,
            field_updates=proposal.get("field_updates"),
            final_response=request.final_response or proposal.get("draft_response"),
            reason=request.rejection_reason
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"승인 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )

@router.post("/refine", response_model=RefineResponse)
async def refine_proposal(
    request: RefineRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    repo: ProposalRepository = Depends(get_proposal_repository),
):
    """
    제안 수정 요청
    """
    try:
        proposal = await repo.get_proposal(request.proposal_id)
        if not proposal:
            raise HTTPException(status_code=404, detail="Proposal not found")
            
        # Logic to refine proposal using LLM (not implemented in this MVP step)
        # We would call the Resolver agent again with feedback
        
        # Create a new proposal object based on the old one but with incremented version
        new_version = proposal.get("proposal_version", 1) + 1
        
        refined_proposal = Proposal(
            id=request.proposal_id,
            tenantId=x_tenant_id,
            ticketId=request.ticket_id,
            proposalVersion=new_version,
            draftResponse=proposal.get("draft_response") or "", # In real logic, this would be refined
            fieldUpdates=proposal.get("field_updates"),
            reasoning="Refinement placeholder"
        )
        
        return RefineResponse(
            proposal=refined_proposal,
            version=new_version
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"수정 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )

@router.get("/status/{proposal_id}")
async def get_proposal_status(
    proposal_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    repo: ProposalRepository = Depends(get_proposal_repository),
):
    """
    제안 상태 조회
    """
    try:
        proposal = await repo.get_proposal(proposal_id)
        if not proposal:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"제안을 찾을 수 없습니다: {proposal_id}",
            )
        return proposal

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"상태 조회 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


# =============================================================================
# HITL Feedback Endpoints (ported from nexus-ai)
# =============================================================================


@router.post("/feedback/submit", response_model=FeedbackSubmitResponse)
async def submit_feedback(
    request: FeedbackSubmitRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """
    Submit feedback on an analysis (helpful / not_helpful).
    Optionally includes rating (1-5) and text feedback.
    """
    try:
        repo = get_feedback_repository()
        event_id = await repo.submit_feedback(
            analysis_id=request.analysis_id,
            event_type=request.event_type,
            rating=request.rating,
            feedback_text=request.feedback_text,
            agent_id=request.agent_id,
            tenant_id=x_tenant_id,
        )

        if not event_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to save feedback (DB may not be configured)",
            )

        return FeedbackSubmitResponse(
            success=True,
            event_id=event_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Feedback submit error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.post("/feedback/edit", response_model=FeedbackEditResponse)
async def edit_feedback(
    request: FeedbackEditRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """
    Submit an agent-edited/approved response for training.
    The approved_response is stored for future fine-tuning.
    """
    try:
        repo = get_feedback_repository()
        sample_id = await repo.update_approved_response(
            analysis_id=request.analysis_id,
            approved_response=request.approved_response,
            agent_id=request.agent_id,
            tenant_id=x_tenant_id,
        )

        if not sample_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to save edited response (DB may not be configured)",
            )

        return FeedbackEditResponse(
            success=True,
            sample_id=sample_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Feedback edit error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.post("/training/export", response_model=TrainingExportResponse)
async def export_training_data(
    request: TrainingExportRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """
    Export training samples with approved responses for fine-tuning.
    Only returns samples where an agent has provided an approved_response.
    """
    try:
        repo = get_feedback_repository()
        samples = await repo.get_exportable_samples(
            tenant_id=x_tenant_id,
            limit=request.limit,
            min_rating=request.min_rating,
            mark_exported=request.mark_exported,
        )

        export_samples = [
            TrainingSampleExport(
                id=s["id"],
                tenant_id=s["tenant_id"],
                ticket_id=s["ticket_id"],
                analysis_id=s.get("analysis_id", ""),
                original_response=s.get("original_response", {}),
                approved_response=s.get("approved_response", {}),
                rating=s.get("rating"),
                prompt_version=s.get("prompt_version"),
                model=s.get("model"),
                created_at=s.get("created_at"),
            )
            for s in samples
        ]

        return TrainingExportResponse(
            success=True,
            count=len(export_samples),
            samples=export_samples,
        )

    except Exception as e:
        logger.error(f"Training export error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
