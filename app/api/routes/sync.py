"""
Sync API Routes

Freshdesk data synchronization endpoints.
Triggers data collection from Freshdesk and uploads to Gemini RAG store.

Requires tenant authentication (X-Tenant-ID, X-Platform, X-API-Key, X-Domain headers).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.middleware.tenant_auth import TenantContext, get_tenant_context
from app.services.freshdesk_client import FreshdeskClient
from app.services.sync_service import SyncService, SyncOptions, SyncProgress
from app.services.transformer import GeminiDocument
from app.services.gemini_file_search import upload_document_to_store
from app.services.ticket_metadata_service import TicketMetadataService
from app.core.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sync"])


# ============================================================================
# Request/Response Models
# ============================================================================

class SyncTriggerRequest(BaseModel):
    """동기화 트리거 요청"""
    include_tickets: bool = Field(default=True, description="티켓 동기화 포함")
    include_articles: bool = Field(default=True, description="아티클 동기화 포함")
    incremental: bool = Field(default=False, description="증분 동기화 (마지막 동기화 이후 변경분만)")
    ticket_since: Optional[datetime] = Field(default=None, description="이 시각 이후 업데이트된 티켓만")
    article_since: Optional[datetime] = Field(default=None, description="이 시각 이후 업데이트된 아티클만")
    max_concurrency: int = Field(default=5, ge=1, le=20, description="병렬 처리 수")
    batch_size: int = Field(default=10, ge=1, le=100, description="배치 크기")


class SyncTriggerResponse(BaseModel):
    """동기화 트리거 응답"""
    job_id: str
    status: str
    message: str
    tenant_id: str
    domain: str


class SyncStatusResponse(BaseModel):
    """동기화 상태 응답"""
    job_id: str
    status: str
    phase: str
    tickets_processed: int
    tickets_total: int
    articles_processed: int
    articles_total: int
    documents_uploaded: int
    documents_total: int
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    error: Optional[str]


class SyncResultResponse(BaseModel):
    """동기화 결과 응답"""
    synced_at: str
    tickets_count: int
    articles_count: int
    documents_count: int
    errors: list[str]
    rag_store_name: Optional[str]


# ============================================================================
# In-memory Job Store (for demo - use Redis/DB in production)
# ============================================================================

class SyncJobStore:
    """동기화 작업 상태 저장소 (메모리)"""
    
    _jobs: dict[str, dict[str, Any]] = {}
    _services: dict[str, SyncService] = {}
    
    @classmethod
    def create_job(cls, job_id: str, tenant_id: str) -> None:
        cls._jobs[job_id] = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "status": "pending",
            "phase": "idle",
            "result": None,
            "error": None,
        }
    
    @classmethod
    def update_job(cls, job_id: str, **kwargs: Any) -> None:
        if job_id in cls._jobs:
            cls._jobs[job_id].update(kwargs)
    
    @classmethod
    def get_job(cls, job_id: str) -> dict[str, Any] | None:
        return cls._jobs.get(job_id)
    
    @classmethod
    def set_service(cls, job_id: str, service: SyncService) -> None:
        cls._services[job_id] = service
    
    @classmethod
    def get_service(cls, job_id: str) -> SyncService | None:
        return cls._services.get(job_id)
    
    @classmethod
    def cleanup_job(cls, job_id: str) -> None:
        cls._jobs.pop(job_id, None)
        cls._services.pop(job_id, None)


# ============================================================================
# Endpoints
# ============================================================================

@router.post("/trigger", response_model=SyncTriggerResponse)
async def trigger_sync(
    request_body: SyncTriggerRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_tenant_context),
) -> SyncTriggerResponse:
    """
    동기화 트리거
    
    Freshdesk 데이터를 수집하여 Gemini RAG 스토어에 업로드합니다.
    백그라운드 작업으로 실행되며, /api/sync/status/{job_id}로 진행 상황을 확인할 수 있습니다.
    
    Requires authentication headers:
    - X-Tenant-ID: 테넌트 식별자
    - X-Platform: 플랫폼 (freshdesk)
    - X-API-Key: Freshdesk API 키
    - X-Domain: Freshdesk 도메인
    """
    # Get API key from request header
    api_key = request.headers.get("X-API-Key", "").strip()
    
    if tenant.platform != "freshdesk":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported platform: {tenant.platform}. Only 'freshdesk' is supported.",
        )
    
    if not tenant.domain or not api_key:
        raise HTTPException(
            status_code=400,
            detail="Missing X-Domain or X-API-Key header",
        )
    
    # Generate job ID
    job_id = f"sync_{tenant.tenant_id}_{int(datetime.now().timestamp())}"
    
    # Create job record
    SyncJobStore.create_job(job_id, tenant.tenant_id)
    
    # Create metadata service for Supabase
    settings = get_settings()
    metadata_service = None
    if settings.supabase_common_url and settings.supabase_common_service_role_key:
        metadata_service = TicketMetadataService(
            supabase_url=settings.supabase_common_url,
            supabase_key=settings.supabase_common_service_role_key,
            tenant_slug=tenant.tenant_id,
            platform=tenant.platform,
        )
        logger.info(f"Metadata service enabled for tenant {tenant.tenant_id}")
    
    # Create SyncService
    client = FreshdeskClient(tenant.domain, api_key)
    sync_service = SyncService(
        client,
        tenant_id=tenant.tenant_id,
        platform=tenant.platform,
        batch_size=request_body.batch_size,
        max_concurrency=request_body.max_concurrency,
        metadata_service=metadata_service,
    )
    SyncJobStore.set_service(job_id, sync_service)
    
    # Create options
    options = SyncOptions(
        include_tickets=request_body.include_tickets,
        include_articles=request_body.include_articles,
        incremental=request_body.incremental,
        ticket_since=request_body.ticket_since,
        article_since=request_body.article_since,
        batch_size=request_body.batch_size,
        max_concurrency=request_body.max_concurrency,
    )
    
    # Start background task
    background_tasks.add_task(run_sync_job, job_id, sync_service, options)
    
    logger.info(f"Sync job triggered: {job_id} for tenant {tenant.tenant_id}")
    
    return SyncTriggerResponse(
        job_id=job_id,
        status="started",
        message="Sync job started. Check /api/sync/status/{job_id} for progress.",
        tenant_id=tenant.tenant_id,
        domain=tenant.domain,
    )


async def run_sync_job(
    job_id: str,
    sync_service: SyncService,
    options: SyncOptions,
    tickets_store: Optional[str] = None,
    articles_store: Optional[str] = None,
) -> None:
    """백그라운드 동기화 작업 실행"""
    settings = get_settings()
    
    # 스토어 이름 결정 (환경변수 또는 파라미터)
    tickets_store_name = tickets_store or getattr(settings, "gemini_store_tickets", None)
    articles_store_name = articles_store or getattr(settings, "gemini_store_articles", None)
    
    try:
        SyncJobStore.update_job(job_id, status="running")
        
        # Upload callback - 문서를 적절한 Gemini 스토어에 업로드
        async def upload_to_gemini(documents: list[GeminiDocument]) -> None:
            """Gemini File Search 스토어에 문서 업로드 (병렬 처리)"""
            
            # 동시 업로드 제한 (Rate Limit 방지) - 20개로 증량
            sem = asyncio.Semaphore(20)

            async def upload_single(doc: GeminiDocument):
                async with sem:
                    # 문서 타입에 따라 스토어 선택
                    doc_type = doc.type  # "ticket" or "article"
                    
                    if doc_type == "article":
                        store_name = articles_store_name
                    else:
                        store_name = tickets_store_name
                    
                    if not store_name:
                        logger.warning(f"No store configured for type '{doc_type}', skipping document: {doc.title}")
                        return
                    
                    # 메타데이터를 Gemini 형식으로 변환
                    metadata = []
                    if doc.metadata:
                        for key, value in doc.metadata.items():
                            if value is not None:
                                # custom_fields는 개별 필드로 풀어서 저장 (Flatten)
                                if key == "custom_fields" and isinstance(value, dict):
                                    for cf_key, cf_value in value.items():
                                        if cf_value is not None:
                                            str_cf_value = str(cf_value)
                                            # Gemini 메타데이터 값 길이 제한 (안전하게 100자)
                                            if len(str_cf_value) > 100:
                                                str_cf_value = str_cf_value[:97] + "..."
                                            metadata.append({"key": cf_key, "value": str_cf_value})
                                    continue

                                # 일반 메타데이터 처리
                                # Gemini 메타데이터 값 길이 제한 (256자 -> 안전하게 100자로 제한)
                                str_value = str(value)
                                if len(str_value) > 100:
                                    str_value = str_value[:97] + "..."
                                
                                metadata.append({"key": key, "value": str_value})
                    
                    try:
                        await upload_document_to_store(
                            store_name=store_name,
                            file_name=doc.title,
                            file_content=doc.content.encode("utf-8"),
                            metadata=metadata,
                        )
                        logger.debug(f"Uploaded document: {doc.title} to {store_name}")
                    except Exception as e:
                        logger.error(f"Failed to upload document {doc.title}: {e}")
                        # 개별 문서 실패는 무시하고 계속 진행 (전체 중단 방지)

            # Run all uploads in parallel
            await asyncio.gather(*[upload_single(doc) for doc in documents])
        
        # Run sync with upload callback
        result = await sync_service.sync(
            options=options,
            upload_callback=upload_to_gemini,
        )
        
        # 사용된 스토어 정보 추가
        if hasattr(result, "__dataclass_fields__"):
            result_dict = asdict(result)
        else:
            result_dict = result
        
        result_dict["rag_store_tickets"] = tickets_store_name
        result_dict["rag_store_articles"] = articles_store_name
        
        SyncJobStore.update_job(
            job_id,
            status="completed",
            phase="completed",
            result=result_dict,
        )
        
        logger.info(f"Sync job completed: {job_id}")
        
    except Exception as e:
        logger.error(f"Sync job failed: {job_id} - {e}")
        SyncJobStore.update_job(
            job_id,
            status="failed",
            error=str(e),
        )
    finally:
        # Cleanup client
        try:
            await sync_service.close()
        except Exception:
            pass


@router.get("/status/{job_id}", response_model=SyncStatusResponse)
async def get_sync_status(
    job_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> SyncStatusResponse:
    """
    동기화 상태 조회
    
    백그라운드로 실행 중인 동기화 작업의 진행 상황을 확인합니다.
    """
    job = SyncJobStore.get_job(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    
    if job["tenant_id"] != tenant.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied to this job")
    
    # Get progress from service if running
    service = SyncJobStore.get_service(job_id)
    progress = service.progress if service else SyncProgress()
    
    return SyncStatusResponse(
        job_id=job_id,
        status=job.get("status", "unknown"),
        phase=progress.phase,
        tickets_processed=progress.tickets_processed,
        tickets_total=progress.tickets_total,
        articles_processed=progress.articles_processed,
        articles_total=progress.articles_total,
        documents_uploaded=progress.documents_uploaded,
        documents_total=progress.documents_total,
        started_at=progress.started_at,
        completed_at=progress.completed_at,
        error=job.get("error") or progress.error,
    )


@router.get("/result/{job_id}", response_model=SyncResultResponse)
async def get_sync_result(
    job_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> SyncResultResponse:
    """
    동기화 결과 조회
    
    완료된 동기화 작업의 결과를 확인합니다.
    """
    job = SyncJobStore.get_job(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    
    if job["tenant_id"] != tenant.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied to this job")
    
    if job["status"] != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Job not completed. Current status: {job['status']}",
        )
    
    result = job.get("result", {})
    
    return SyncResultResponse(
        synced_at=result.get("synced_at", ""),
        tickets_count=result.get("tickets_count", 0),
        articles_count=result.get("articles_count", 0),
        documents_count=result.get("documents_count", 0),
        errors=result.get("errors", []),
        rag_store_name=result.get("rag_store_name"),
    )


@router.delete("/cancel/{job_id}")
async def cancel_sync(
    job_id: str,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict[str, str]:
    """
    동기화 작업 취소
    
    실행 중인 동기화 작업을 취소합니다.
    """
    job = SyncJobStore.get_job(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    
    if job["tenant_id"] != tenant.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied to this job")
    
    if job["status"] not in ("pending", "running"):
        return {"message": f"Job already {job['status']}"}
    
    # Mark as cancelled
    SyncJobStore.update_job(job_id, status="cancelled")
    
    # Cleanup
    service = SyncJobStore.get_service(job_id)
    if service:
        try:
            await service.close()
        except Exception:
            pass
    
    logger.info(f"Sync job cancelled: {job_id}")
    
    return {"message": "Job cancelled", "job_id": job_id}


@router.get("/health")
async def sync_health(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict[str, Any]:
    """
    동기화 서비스 상태 확인
    
    Freshdesk API 연결 상태를 확인합니다.
    """
    # Get API key from request header
    api_key = request.headers.get("X-API-Key", "").strip()
    
    if not tenant.domain or not api_key:
        return {
            "status": "error",
            "message": "Missing domain or API key",
        }
    
    try:
        client = FreshdeskClient(tenant.domain, api_key)
        is_healthy = await client.health_check()
        await client.close()
        
        return {
            "status": "healthy" if is_healthy else "unhealthy",
            "tenant_id": tenant.tenant_id,
            "domain": tenant.domain,
            "freshdesk_connected": is_healthy,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "tenant_id": tenant.tenant_id,
            "domain": tenant.domain,
        }
