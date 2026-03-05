from typing import Any, Dict, List, Optional

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.middleware.tenant_auth import TenantContext, get_tenant_context, get_optional_tenant_context
from app.models.session import ChatRequest, ChatResponse
from app.services.chat_usecase import ChatUsecase, get_chat_usecase
from app.services.multitenant_chat_handler import MultitenantChatHandler, get_multitenant_chat_handler


router = APIRouter(prefix="/web/v1", tags=["channel:web"])

_WEB_AUTH_ERROR_EXAMPLES: Dict[str, Any] = {
    "missing_tenant_id": {
        "summary": "X-Tenant-ID 누락",
        "value": {"detail": "X-Tenant-ID header is required"},
    },
    "missing_platform": {
        "summary": "X-Platform 누락",
        "value": {"detail": "X-Platform header is required"},
    },
    "missing_api_key": {
        "summary": "X-API-Key 누락",
        "value": {"detail": "X-API-Key header is required"},
    },
    "invalid_api_key": {
        "summary": "API Key 검증 실패",
        "value": {"detail": "Invalid API key for the specified platform and tenant"},
    },
    "unsupported_platform": {
        "summary": "지원하지 않는 platform",
        "value": {"detail": "Unsupported platform: unknown. Supported: freshdesk, zendesk, web"},
    },
}

_WEB_ERROR_RESPONSES: Dict[int, Any] = {
    400: {
        "description": "요청 형식 오류(예: platform 값)",
        "content": {"application/json": {"examples": {"unsupported_platform": _WEB_AUTH_ERROR_EXAMPLES["unsupported_platform"]}}},
    },
    401: {
        "description": "인증 헤더 누락",
        "content": {
            "application/json": {
                "examples": {
                    "missing_tenant_id": _WEB_AUTH_ERROR_EXAMPLES["missing_tenant_id"],
                    "missing_platform": _WEB_AUTH_ERROR_EXAMPLES["missing_platform"],
                    "missing_api_key": _WEB_AUTH_ERROR_EXAMPLES["missing_api_key"],
                }
            }
        },
    },
    403: {
        "description": "API Key 검증 실패",
        "content": {"application/json": {"examples": {"invalid_api_key": _WEB_AUTH_ERROR_EXAMPLES["invalid_api_key"]}}},
    },
    503: {
        "description": "채팅 서비스 미사용/미설정",
        "content": {
            "application/json": {
                "examples": {
                    "chat_service_unavailable": {
                        "summary": "Chat service not available",
                        "value": {"detail": "Chat service not available. Check Gemini API configuration."},
                    }
                }
            }
        },
    },
}


def _format_sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat", response_model=ChatResponse, responses=_WEB_ERROR_RESPONSES)
async def web_chat(
    request: ChatRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    handler: Optional[MultitenantChatHandler] = Depends(get_multitenant_chat_handler),
    usecase: ChatUsecase = Depends(get_chat_usecase),
) -> ChatResponse:
    """
    WEB 채널 BFF 엔드포인트.
    - 멀티테넌트 인증 헤더 필요
    - 현재는 multitenant chat 동작과 동일(하위호환/점진 전환 목적)
    """
    if not handler:
        raise HTTPException(status_code=503, detail="Chat service not available. Check Gemini API configuration.")
    return await usecase.handle_multitenant_chat(request, tenant=tenant)


@router.get("/chat/stream", responses=_WEB_ERROR_RESPONSES)
async def web_chat_stream(
    session_id: str = Query(..., alias="sessionId"),
    query: str = Query(...),
    sources: Optional[List[str]] = Query(None),
    product: Optional[str] = Query(None),
    tenant: TenantContext = Depends(get_tenant_context),
    usecase: ChatUsecase = Depends(get_chat_usecase),
) -> StreamingResponse:
    request = ChatRequest(
        sessionId=session_id,
        query=query,
        sources=sources,
        commonProduct=product,
    )

    async def event_stream():
        async for event in usecase.stream_multitenant_chat(request, tenant=tenant):
            yield _format_sse(event["event"], event["data"])

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/tenant/info")
async def web_tenant_info(
    tenant: TenantContext = Depends(get_tenant_context),
) -> Dict[str, Any]:
    """
    PR5에서 `/api/web/v1`로 노출되던 멀티테넌트 유틸 엔드포인트 호환 유지.
    """
    return {
        "tenant_id": tenant.tenant_id,
        "platform": tenant.platform,
        "domain": tenant.domain,
        "verified": tenant.verified,
        "mandatory_filter_count": len(tenant.mandatory_filters),
    }


@router.get("/health")
async def web_health_check(
    tenant: Optional[TenantContext] = Depends(get_optional_tenant_context),
) -> Dict[str, Any]:
    """
    PR5에서 `/api/web/v1`로 노출되던 헬스체크 호환 유지.
    """
    return {
        "status": "healthy",
        "authenticated": tenant is not None,
        "tenant_id": tenant.tenant_id if tenant else None,
    }
