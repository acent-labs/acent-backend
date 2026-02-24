"""
Admin API 라우트

테넌트 관리, 동기화, 스케줄러 관련 관리자 엔드포인트
FDK iparams.html에서 호출
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status

from app.models.admin import (
    TenantConfig,
    TenantConfigCreate,
    TenantConfigUpdate,
    TenantListResponse,
    TenantStats,
    SyncRequest,
    SyncResult,
    SyncStatus,
    SyncJobStatus,
    StoreInfo,
    StoreListResponse,
    SchedulerJobCreate,
    SchedulerStatus,
)
from app.services.admin_service import AdminService, get_admin_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# =============================================================================
# 테넌트 관리 엔드포인트
# =============================================================================

@router.get("/tenants", response_model=TenantListResponse)
async def list_tenants(
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    모든 테넌트 목록 조회

    Args:
        x_admin_api_key: 관리자 API 키

    Returns:
        테넌트 목록
    """
    await admin_service.verify_admin_key(x_admin_api_key)
    tenants = await admin_service.list_tenants()
    return TenantListResponse(tenants=tenants, total=len(tenants))


@router.post("/tenants", status_code=status.HTTP_201_CREATED)
async def create_tenant(
    config: TenantConfigCreate,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    새 테넌트 생성

    Args:
        config: 테넌트 설정
        x_admin_api_key: 관리자 API 키

    Returns:
        생성된 테넌트 정보
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    try:
        tenant = await admin_service.create_tenant(config)
        return {"status": "created", "tenant": tenant}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.get("/tenants/{tenant_id}", response_model=TenantConfig)
async def get_tenant(
    tenant_id: str,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    테넌트 정보 조회

    Args:
        tenant_id: 테넌트 ID
        x_admin_api_key: 관리자 API 키

    Returns:
        테넌트 정보
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    tenant = await admin_service.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"테넌트를 찾을 수 없습니다: {tenant_id}",
        )
    return tenant


@router.put("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    updates: TenantConfigUpdate,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    테넌트 정보 업데이트

    Args:
        tenant_id: 테넌트 ID
        updates: 업데이트할 필드
        x_admin_api_key: 관리자 API 키

    Returns:
        업데이트된 테넌트 정보
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    try:
        tenant = await admin_service.update_tenant(tenant_id, updates)
        return {"status": "updated", "tenant": tenant}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: str,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    테넌트 삭제

    Args:
        tenant_id: 테넌트 ID
        x_admin_api_key: 관리자 API 키

    Returns:
        삭제 확인
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    try:
        await admin_service.delete_tenant(tenant_id)
        return {"status": "deleted", "tenant_id": tenant_id}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


# =============================================================================
# 테넌트 설정 (FDK 앱용)
# =============================================================================

@router.get("/config", response_model=TenantConfig)
async def get_tenant_config(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_freshdesk_domain: Optional[str] = Header(None, alias="X-Freshdesk-Domain"),
    x_freshdesk_api_key: Optional[str] = Header(None, alias="X-Freshdesk-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    현재 테넌트 설정 조회 (FDK 앱용)
    """
    tenant = await admin_service.get_tenant(x_tenant_id)
    if tenant:
        return tenant

    # Upsert: 테넌트가 없으면 FDK 헤더 기반으로 기본 설정을 생성한다.
    # (관리자 UI에서 선행 테넌트 생성 없이 설정 저장/조회 가능)
    if not x_freshdesk_domain:
        raise HTTPException(status_code=404, detail="Tenant not found")

    created = await admin_service.create_tenant(
        TenantConfigCreate(
            tenant_id=x_tenant_id,
            freshdesk_domain=x_freshdesk_domain,
            freshdesk_api_key=x_freshdesk_api_key or "",
        )
    )
    return created


@router.put("/config", response_model=TenantConfig)
async def update_tenant_config(
    config: TenantConfigUpdate,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_freshdesk_domain: Optional[str] = Header(None, alias="X-Freshdesk-Domain"),
    x_freshdesk_api_key: Optional[str] = Header(None, alias="X-Freshdesk-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    현재 테넌트 설정 업데이트 (FDK 앱용)
    """
    try:
        tenant = await admin_service.get_tenant(x_tenant_id)
        if not tenant:
            # Upsert: 없으면 생성 후 업데이트
            if not x_freshdesk_domain:
                raise HTTPException(status_code=404, detail="Tenant not found")
            await admin_service.create_tenant(
                TenantConfigCreate(
                    tenant_id=x_tenant_id,
                    freshdesk_domain=x_freshdesk_domain,
                    freshdesk_api_key=x_freshdesk_api_key or "",
                )
            )
        return await admin_service.update_tenant(x_tenant_id, config)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/tenants/{tenant_id}/stats", response_model=TenantStats)
async def get_tenant_stats(
    tenant_id: str,
    period_days: int = Query(30, ge=1, le=365, description="통계 기간 (일)"),
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    테넌트 통계 조회

    Args:
        tenant_id: 테넌트 ID
        period_days: 통계 기간
        x_admin_api_key: 관리자 API 키

    Returns:
        테넌트 통계
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    stats = await admin_service.get_tenant_stats(tenant_id, period_days)
    return stats


# =============================================================================
# 동기화 엔드포인트
# =============================================================================

@router.get("/tenants/{tenant_id}/sync/status", response_model=SyncStatus)
async def get_sync_status(
    tenant_id: str,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    동기화 상태 조회

    Args:
        tenant_id: 테넌트 ID
        x_admin_api_key: 관리자 API 키

    Returns:
        동기화 상태
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    sync_status = await admin_service.get_sync_status(tenant_id)
    return sync_status


@router.post("/tenants/{tenant_id}/sync/trigger", response_model=SyncResult)
async def trigger_sync(
    tenant_id: str,
    request: SyncRequest,
    background_tasks: BackgroundTasks,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    수동 동기화 트리거

    Args:
        tenant_id: 테넌트 ID
        request: 동기화 요청
        background_tasks: FastAPI 백그라운드 태스크
        x_admin_api_key: 관리자 API 키

    Returns:
        동기화 작업 정보
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    # 이미 진행 중인 동기화가 있는지 확인
    current_status = await admin_service.get_sync_status(tenant_id)
    if current_status.sync_in_progress:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"동기화가 이미 진행 중입니다: {current_status.current_job_id}",
        )

    # 백그라운드 작업 시작
    job_id = str(uuid.uuid4())
    background_tasks.add_task(
        admin_service.run_sync,
        tenant_id,
        request,
        job_id,
    )

    return SyncResult(
        success=True,
        items_synced=0,
        sync_type=request.sync_type,
        started_at=datetime.utcnow().isoformat(),
        job_id=job_id,
    )


@router.get("/tenants/{tenant_id}/sync/jobs/{job_id}", response_model=SyncJobStatus)
async def get_sync_job_status(
    tenant_id: str,
    job_id: str,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    동기화 작업 상태 조회

    Args:
        tenant_id: 테넌트 ID
        job_id: 작업 ID
        x_admin_api_key: 관리자 API 키

    Returns:
        작업 상태
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    job_status = await admin_service.get_sync_job_status(tenant_id, job_id)
    if not job_status:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"동기화 작업을 찾을 수 없습니다: {job_id}",
        )
    return job_status


# =============================================================================
# 스토어 관리 엔드포인트
# =============================================================================

@router.get("/tenants/{tenant_id}/stores", response_model=StoreListResponse)
async def list_stores(
    tenant_id: str,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    테넌트의 Gemini File Search 스토어 목록

    Args:
        tenant_id: 테넌트 ID
        x_admin_api_key: 관리자 API 키

    Returns:
        스토어 목록
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    stores = await admin_service.list_stores(tenant_id)
    return StoreListResponse(stores=stores, total=len(stores))


@router.get("/tenants/{tenant_id}/stores/{store_name}", response_model=StoreInfo)
async def get_store_info(
    tenant_id: str,
    store_name: str,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    스토어 상세 정보 조회

    Args:
        tenant_id: 테넌트 ID
        store_name: 스토어 이름
        x_admin_api_key: 관리자 API 키

    Returns:
        스토어 정보
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    store_info = await admin_service.get_store_info(tenant_id, store_name)
    if not store_info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"스토어를 찾을 수 없습니다: {store_name}",
        )
    return store_info


# =============================================================================
# 스케줄러 관리 엔드포인트
# =============================================================================

@router.get("/scheduler/status", response_model=SchedulerStatus)
async def get_scheduler_status(
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    스케줄러 상태 조회

    Args:
        x_admin_api_key: 관리자 API 키

    Returns:
        스케줄러 상태 및 등록된 작업 목록
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    scheduler_status = await admin_service.get_scheduler_status()
    return scheduler_status


@router.post("/scheduler/jobs", status_code=status.HTTP_201_CREATED)
async def create_scheduler_job(
    job: SchedulerJobCreate,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    스케줄러 작업 생성

    Args:
        job: 작업 설정
        x_admin_api_key: 관리자 API 키

    Returns:
        생성된 작업 정보
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    try:
        job_info = await admin_service.create_scheduler_job(job)
        return {"status": "created", "job": job_info}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.delete("/scheduler/jobs/{job_id}")
async def delete_scheduler_job(
    job_id: str,
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    스케줄러 작업 삭제

    Args:
        job_id: 작업 ID
        x_admin_api_key: 관리자 API 키

    Returns:
        삭제 확인
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    try:
        await admin_service.delete_scheduler_job(job_id)
        return {"status": "deleted", "job_id": job_id}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.put("/scheduler/jobs/{job_id}/toggle")
async def toggle_scheduler_job(
    job_id: str,
    enabled: bool = Query(..., description="활성화 여부"),
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    스케줄러 작업 활성화/비활성화

    Args:
        job_id: 작업 ID
        enabled: 활성화 여부
        x_admin_api_key: 관리자 API 키

    Returns:
        업데이트된 작업 정보
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    try:
        job_info = await admin_service.toggle_scheduler_job(job_id, enabled)
        return {"status": "updated", "job": job_info}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


# =============================================================================
# 캐시 관리 엔드포인트
# =============================================================================

@router.post("/cache/clear")
async def clear_cache(
    x_admin_api_key: str = Header(..., alias="X-Admin-API-Key"),
    admin_service: AdminService = Depends(get_admin_service),
):
    """
    모든 캐시 클리어

    Args:
        x_admin_api_key: 관리자 API 키

    Returns:
        클리어 확인
    """
    await admin_service.verify_admin_key(x_admin_api_key)

    await admin_service.clear_all_cache()
    return {"status": "cache_cleared", "timestamp": datetime.utcnow().isoformat()}
