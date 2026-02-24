"""
Admin 서비스

테넌트 관리, 동기화, 스케줄러 비즈니스 로직
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.models.admin import (
    TenantConfig,
    TenantConfigCreate,
    TenantConfigUpdate,
    TenantStats,
    SyncRequest,
    SyncStatus,
    SyncJobStatus,
    StoreInfo,
    SchedulerJobInfo,
    SchedulerJobCreate,
    SchedulerStatus,
)
from app.services.scheduler_service import get_scheduler_service, SchedulerService

logger = logging.getLogger(__name__)


# =============================================================================
# In-Memory Stores (추후 Supabase로 교체 가능)
# =============================================================================

class TenantStore:
    """테넌트 저장소 (In-memory)"""

    def __init__(self):
        self._tenants: Dict[str, Dict[str, Any]] = {}

    def list_all(self) -> List[TenantConfig]:
        """모든 테넌트 조회"""
        return [TenantConfig(**t) for t in self._tenants.values()]

    def get(self, tenant_id: str) -> Optional[TenantConfig]:
        """테넌트 조회"""
        data = self._tenants.get(tenant_id)
        if data:
            return TenantConfig(**data)
        return None

    def create(self, config: TenantConfigCreate) -> TenantConfig:
        """테넌트 생성"""
        if config.tenant_id in self._tenants:
            raise ValueError(f"이미 존재하는 테넌트: {config.tenant_id}")

        now = datetime.utcnow()
        data = {
            **config.model_dump(),
            "created_at": now,
            "updated_at": now,
        }
        # API 키 마스킹
        if data.get("freshdesk_api_key"):
            data["freshdesk_api_key"] = self._mask_api_key(data["freshdesk_api_key"])

        self._tenants[config.tenant_id] = data
        return TenantConfig(**data)

    def update(self, tenant_id: str, updates: TenantConfigUpdate) -> TenantConfig:
        """테넌트 업데이트"""
        if tenant_id not in self._tenants:
            raise ValueError(f"테넌트를 찾을 수 없습니다: {tenant_id}")

        update_data = {k: v for k, v in updates.model_dump().items() if v is not None}
        if "freshdesk_api_key" in update_data:
            update_data["freshdesk_api_key"] = self._mask_api_key(update_data["freshdesk_api_key"])

        update_data["updated_at"] = datetime.utcnow()
        self._tenants[tenant_id].update(update_data)
        return TenantConfig(**self._tenants[tenant_id])

    def delete(self, tenant_id: str) -> None:
        """테넌트 삭제"""
        if tenant_id not in self._tenants:
            raise ValueError(f"테넌트를 찾을 수 없습니다: {tenant_id}")
        del self._tenants[tenant_id]

    @staticmethod
    def _mask_api_key(key: str) -> str:
        """API 키 마스킹"""
        if len(key) <= 8:
            return "****"
        return f"{key[:4]}****{key[-4:]}"


class SyncJobStore:
    """동기화 작업 저장소 (In-memory)"""

    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._current_jobs: Dict[str, str] = {}  # tenant_id -> job_id

    def create(self, tenant_id: str, job_id: str, sync_type: str) -> SyncJobStatus:
        """작업 생성"""
        data = {
            "job_id": job_id,
            "tenant_id": tenant_id,
            "status": "running",
            "sync_type": sync_type,
            "started_at": datetime.utcnow().isoformat(),
            "completed_at": None,
            "items_synced": 0,
            "errors": [],
            "progress_percent": 0,
        }
        self._jobs[job_id] = data
        self._current_jobs[tenant_id] = job_id
        return SyncJobStatus(**data)

    def get(self, job_id: str) -> Optional[SyncJobStatus]:
        """작업 조회"""
        data = self._jobs.get(job_id)
        if data:
            return SyncJobStatus(**data)
        return None

    def update(self, job_id: str, updates: Dict[str, Any]) -> Optional[SyncJobStatus]:
        """작업 업데이트"""
        if job_id not in self._jobs:
            return None
        self._jobs[job_id].update(updates)
        return SyncJobStatus(**self._jobs[job_id])

    def complete(self, job_id: str, success: bool, items_synced: int, errors: List[str]) -> None:
        """작업 완료"""
        if job_id in self._jobs:
            tenant_id = self._jobs[job_id]["tenant_id"]
            self._jobs[job_id].update({
                "status": "completed" if success else "failed",
                "completed_at": datetime.utcnow().isoformat(),
                "items_synced": items_synced,
                "errors": errors,
                "progress_percent": 100,
            })
            if tenant_id in self._current_jobs:
                del self._current_jobs[tenant_id]

    def get_current_job(self, tenant_id: str) -> Optional[str]:
        """현재 진행 중인 작업 ID 조회"""
        return self._current_jobs.get(tenant_id)


class SchedulerJobStore:
    """스케줄러 작업 저장소 (레거시 - SchedulerService로 대체됨)"""
    # NOTE: 이 클래스는 SchedulerService로 대체되었습니다.
    # 하위 호환성을 위해 유지하지만, 실제로는 사용되지 않습니다.
    pass


# 전역 저장소 인스턴스
_tenant_store = TenantStore()
_sync_job_store = SyncJobStore()


# =============================================================================
# AdminService
# =============================================================================

class AdminService:
    """Admin 서비스"""

    def __init__(
        self,
        tenant_store: TenantStore,
        sync_job_store: SyncJobStore,
        scheduler_service: SchedulerService,
        admin_api_key: Optional[str],
    ):
        self.tenant_store = tenant_store
        self.sync_job_store = sync_job_store
        self.scheduler_service = scheduler_service
        self.admin_api_key = admin_api_key

    async def verify_admin_key(self, provided_key: str) -> None:
        """관리자 API 키 검증"""
        if not self.admin_api_key:
            # API 키가 설정되지 않으면 모든 요청 허용 (개발 모드)
            logger.warning("Admin API 키가 설정되지 않음 - 인증 건너뜀")
            return

        if provided_key != self.admin_api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 관리자 API 키",
            )

    # =========================================================================
    # 테넌트 관리
    # =========================================================================

    async def list_tenants(self) -> List[TenantConfig]:
        """모든 테넌트 조회"""
        return self.tenant_store.list_all()

    async def get_tenant(self, tenant_id: str) -> Optional[TenantConfig]:
        """테넌트 조회"""
        return self.tenant_store.get(tenant_id)

    async def create_tenant(self, config: TenantConfigCreate) -> TenantConfig:
        """테넌트 생성"""
        return self.tenant_store.create(config)

    async def update_tenant(self, tenant_id: str, updates: TenantConfigUpdate) -> TenantConfig:
        """테넌트 업데이트"""
        return self.tenant_store.update(tenant_id, updates)

    async def delete_tenant(self, tenant_id: str) -> None:
        """테넌트 삭제"""
        self.tenant_store.delete(tenant_id)

    async def get_tenant_stats(self, tenant_id: str, period_days: int) -> TenantStats:
        """테넌트 통계 조회"""
        # TODO: 실제 통계 계산 구현
        period_start = (datetime.utcnow() - timedelta(days=period_days)).isoformat()
        period_end = datetime.utcnow().isoformat()

        return TenantStats(
            tenant_id=tenant_id,
            total_proposals=0,
            approved_proposals=0,
            rejected_proposals=0,
            modified_proposals=0,
            approval_rate=0.0,
            average_analysis_time_ms=0.0,
            period_start=period_start,
            period_end=period_end,
        )

    # =========================================================================
    # 동기화 관리
    # =========================================================================

    async def get_sync_status(self, tenant_id: str) -> SyncStatus:
        """동기화 상태 조회"""
        current_job_id = self.sync_job_store.get_current_job(tenant_id)

        # TODO: 실제 동기화 상태 조회 구현 (Gemini File Search API)
        return SyncStatus(
            tenant_id=tenant_id,
            last_ticket_sync=None,
            last_article_sync=None,
            total_tickets=0,
            total_articles=0,
            sync_in_progress=current_job_id is not None,
            current_job_id=current_job_id,
            next_scheduled_sync=None,
        )

    async def run_sync(self, tenant_id: str, request: SyncRequest, job_id: str) -> None:
        """동기화 실행 (백그라운드)"""
        logger.info(f"동기화 시작: tenant={tenant_id}, type={request.sync_type}, job={job_id}")

        self.sync_job_store.create(tenant_id, job_id, request.sync_type)
        errors = []
        items_synced = 0

        try:
            # TODO: 실제 동기화 로직 구현
            # 1. Freshdesk에서 티켓/KB 조회
            # 2. Gemini File Search에 업로드
            # 3. 진행 상황 업데이트

            # 진행 상황 시뮬레이션
            for progress in [25, 50, 75, 100]:
                self.sync_job_store.update(job_id, {"progress_percent": progress})
                await self._async_sleep(1)  # 실제 작업 대신 대기

            items_synced = 10  # 시뮬레이션

        except Exception as e:
            logger.error(f"동기화 오류: {e}", exc_info=True)
            errors.append(str(e))

        finally:
            self.sync_job_store.complete(
                job_id,
                success=len(errors) == 0,
                items_synced=items_synced,
                errors=errors,
            )
            logger.info(f"동기화 완료: job={job_id}, synced={items_synced}, errors={len(errors)}")

    async def get_sync_job_status(self, tenant_id: str, job_id: str) -> Optional[SyncJobStatus]:
        """동기화 작업 상태 조회"""
        job = self.sync_job_store.get(job_id)
        if job and job.tenant_id == tenant_id:
            return job
        return None

    # =========================================================================
    # 스토어 관리
    # =========================================================================

    async def list_stores(self, tenant_id: str) -> List[StoreInfo]:
        """테넌트의 스토어 목록 조회"""
        tenant = self.tenant_store.get(tenant_id)
        if not tenant:
            return []

        stores = []
        for store_attr in ["gemini_store_tickets", "gemini_store_articles", "gemini_store_common"]:
            store_name = getattr(tenant, store_attr, None)
            if store_name:
                # TODO: 실제 Gemini File Search API에서 스토어 정보 조회
                stores.append(StoreInfo(
                    store_name=store_name,
                    display_name=store_attr.replace("gemini_store_", "").title(),
                    document_count=0,
                    last_updated=None,
                    size_bytes=None,
                ))

        return stores

    async def get_store_info(self, tenant_id: str, store_name: str) -> Optional[StoreInfo]:
        """스토어 정보 조회"""
        stores = await self.list_stores(tenant_id)
        for store in stores:
            if store.store_name == store_name:
                return store
        return None

    # =========================================================================
    # 스케줄러 관리
    # =========================================================================

    async def get_scheduler_status(self) -> SchedulerStatus:
        """스케줄러 상태 조회"""
        jobs = self.scheduler_service.list_jobs()
        return SchedulerStatus(
            running=self.scheduler_service.is_running(),
            total_jobs=len(jobs),
            jobs=jobs,
        )

    async def create_scheduler_job(self, job: SchedulerJobCreate) -> SchedulerJobInfo:
        """스케줄러 작업 생성"""
        # 테넌트 존재 확인
        tenant = self.tenant_store.get(job.tenant_id)
        if not tenant:
            raise ValueError(f"테넌트를 찾을 수 없습니다: {job.tenant_id}")

        return self.scheduler_service.add_job(
            tenant_id=job.tenant_id,
            job_type=job.job_type,
            cron_expression=job.cron_expression,
            handler=self._create_sync_handler(job.tenant_id, job.job_type),
            enabled=job.enabled,
        )

    async def delete_scheduler_job(self, job_id: str) -> None:
        """스케줄러 작업 삭제"""
        self.scheduler_service.remove_job(job_id)

    async def toggle_scheduler_job(self, job_id: str, enabled: bool) -> SchedulerJobInfo:
        """스케줄러 작업 활성화/비활성화"""
        return self.scheduler_service.toggle_job(job_id, enabled)

    def _create_sync_handler(self, tenant_id: str, job_type: str):
        """동기화 핸들러 생성"""
        async def sync_handler():
            """스케줄러에서 호출하는 동기화 핸들러"""
            logger.info(f"스케줄 동기화 시작: tenant={tenant_id}, type={job_type}")

            job_id = str(uuid.uuid4())

            # SyncRequest 생성
            request = SyncRequest(sync_type=job_type)

            # 동기화 실행
            await self.run_sync(tenant_id, request, job_id)

            logger.info(f"스케줄 동기화 완료: tenant={tenant_id}, job={job_id}")

        return sync_handler

    # =========================================================================
    # 캐시 관리
    # =========================================================================

    async def clear_all_cache(self) -> None:
        """모든 캐시 클리어"""
        # TODO: Redis 캐시 클리어 등 실제 캐시 클리어 구현
        logger.info("캐시 클리어 완료")

    # =========================================================================
    # Helper Methods
    # =========================================================================

    @staticmethod
    async def _async_sleep(seconds: float) -> None:
        """비동기 대기"""
        import asyncio
        await asyncio.sleep(seconds)


# =============================================================================
# Dependency Injection
# =============================================================================

def get_admin_service() -> AdminService:
    """AdminService 의존성 주입"""
    settings = get_settings()

    # Admin API 키는 환경변수에서 가져옴
    admin_api_key = getattr(settings, 'admin_api_key', None)

    return AdminService(
        tenant_store=_tenant_store,
        sync_job_store=_sync_job_store,
        scheduler_service=get_scheduler_service(),
        admin_api_key=admin_api_key,
    )
