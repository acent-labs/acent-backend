from typing import Any, Dict, List, Optional

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.middleware.tenant_auth import TenantContext, get_optional_tenant_context
from app.models.session import ChatRequest, ChatResponse
from app.services.chat_usecase import ChatUsecase, get_chat_usecase


router = APIRouter(prefix="/fdk/v1", tags=["channel:fdk"])

_LOGICAL_SOURCE_KEYS: set[str] = {"tickets", "articles", "common"}

_FDK_400_EXAMPLES: Dict[str, Any] = {
    "missing_sources": {
        "summary": "sources 누락",
        "value": {"detail": "FDK 채널에서는 sources가 필수입니다."},
    },
    "missing_common_product": {
        "summary": "commonProduct(product) 누락",
        "value": {
            "detail": {
                "error": "MISSING_COMMON_PRODUCT",
                "message": "FDK 채널에서는 commonProduct(product)가 필수입니다.",
            }
        },
    },
    "invalid_sources": {
        "summary": "sources allowlist 위반",
        "value": {
            "detail": {
                "error": "INVALID_SOURCES",
                "message": "FDK 채널에서 허용되지 않는 sources가 포함되어 있습니다.",
                "invalid": ["invalid-source"],
                "allowedSourceKeys": ["tickets", "articles", "common"],
            }
        },
    },
    "invalid_sources_combination": {
        "summary": "sources 조합 규칙 위반",
        "value": {
            "detail": {
                "error": "INVALID_SOURCES_COMBINATION",
                "message": "common 단독 sources는 허용되지 않습니다. tickets 또는 articles를 함께 포함하세요.",
                "sources": ["common"],
            }
        },
    },
}

_FDK_COMMON_400_RESPONSES: Dict[int, Any] = {
    400: {
        "description": "FDK 채널 입력 계약 위반",
        "content": {
            "application/json": {
                "examples": _FDK_400_EXAMPLES,
            }
        },
    }
}


def _get_allowed_sources() -> set[str]:
    """
    FDK 채널에서 허용하는 sources:
    - 논리 키: tickets/articles/common (app/api/routes/health.py의 status 설명과 일치)
    - 설정된 실제 store name: settings.gemini_store_* (레거시 포함)
    """
    settings = get_settings()
    allowed: set[str] = {"tickets", "articles", "common"}
    for value in (
        settings.gemini_store_tickets,
        settings.gemini_store_articles,
        settings.gemini_store_common,
        getattr(settings, "gemini_common_store_name", None),  # legacy/test compatibility
    ):
        if value:
            allowed.add(value)
    return allowed


def _validate_sources(sources: Optional[List[str]]) -> List[str]:
    if not sources:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="FDK 채널에서는 sources가 필수입니다.",
        )

    normalized: List[str] = []
    seen: set[str] = set()
    for source in sources:
        normalized_source = (source or "").strip()
        if normalized_source in seen:
            continue
        normalized.append(normalized_source)
        seen.add(normalized_source)

    allowed = _get_allowed_sources()
    invalid = [s for s in normalized if s not in allowed]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "INVALID_SOURCES",
                "message": "FDK 채널에서 허용되지 않는 sources가 포함되어 있습니다.",
                "invalid": invalid,
                "allowedSourceKeys": ["tickets", "articles", "common"],
            },
        )

    normalized_set = set(normalized)

    # sources 조합 제한(안전한 기본 정책)
    # 1) 논리 키(tickets/articles/common)만 사용하는 조합 OR 2) store name 1개만 사용하는 조합
    store_names = normalized_set - _LOGICAL_SOURCE_KEYS
    if store_names and len(normalized) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "INVALID_SOURCES_COMBINATION",
                "message": "store name sources는 다른 sources와 함께 사용할 수 없습니다.",
                "sources": normalized,
            },
        )

    if not store_names:
        # common 단독 금지(최소 tickets/articles 중 하나는 포함)
        if normalized_set == {"common"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "INVALID_SOURCES_COMBINATION",
                    "message": "common 단독 sources는 허용되지 않습니다. tickets 또는 articles를 함께 포함하세요.",
                    "sources": normalized,
                },
            )

    return normalized


def _validate_common_product(common_product: Optional[str]) -> None:
    if not common_product or not common_product.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "MISSING_COMMON_PRODUCT",
                "message": "FDK 채널에서는 commonProduct(product)가 필수입니다.",
            },
        )


def _format_sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post(
    "/chat",
    response_model=ChatResponse,
    response_model_by_alias=True,
    responses=_FDK_COMMON_400_RESPONSES,
)
async def fdk_chat(
    request: ChatRequest,
    tenant: Optional[TenantContext] = Depends(get_optional_tenant_context),
    usecase: ChatUsecase = Depends(get_chat_usecase),
) -> ChatResponse:
    """
    FDK 채널 BFF 엔드포인트.
    - 현재는 레거시 chat 동작과 동일(하위호환/점진 전환 목적)
    - 향후 채널별 기본값/검증/권한을 이 레이어에서만 추가
    """
    request.sources = _validate_sources(request.sources)
    _validate_common_product(request.common_product)
    return await usecase.handle_legacy_chat(request, tenant=tenant)


@router.get(
    "/chat/stream",
    responses=_FDK_COMMON_400_RESPONSES,
)
async def fdk_chat_stream(
    session_id: str = Query(..., alias="sessionId"),
    query: str = Query(...),
    rag_store_name: Optional[str] = Query(None, alias="ragStoreName"),
    sources: Optional[List[str]] = Query(None, alias="sources"),
    product: Optional[str] = Query(None, alias="product"),
    legacy_common_product: Optional[str] = Query(None, alias="commonProduct"),
    clarification_option: Optional[str] = Query(None, alias="clarificationOption"),
    tenant: Optional[TenantContext] = Depends(get_optional_tenant_context),
    usecase: ChatUsecase = Depends(get_chat_usecase),
) -> StreamingResponse:
    normalized_sources = _validate_sources(sources)
    request = ChatRequest(
        sessionId=session_id,
        query=query,
        ragStoreName=rag_store_name,
        sources=normalized_sources,
        commonProduct=product or legacy_common_product,
        clarificationOption=clarification_option,
    )
    _validate_common_product(request.common_product)

    async def event_stream():
        async for event in usecase.stream_legacy_chat(request, tenant=tenant):
            yield _format_sse(event["event"], event["data"])

    return StreamingResponse(event_stream(), media_type="text/event-stream")
