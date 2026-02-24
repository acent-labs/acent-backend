"""
Scheduler Service

APScheduler 기반 테넌트별 자동 동기화 스케줄러
크론 표현식 지원, 작업 상태 추적
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.job import Job
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, JobExecutionEvent

from app.models.admin import SchedulerJobInfo, SchedulerStatus

logger = logging.getLogger(__name__)


class SchedulerService:
    """
    APScheduler 기반 스케줄러 서비스

    테넌트별 동기화 작업을 크론 스케줄로 관리
    """

    _instance: Optional["SchedulerService"] = None

    def __init__(self):
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._job_metadata: Dict[str, Dict[str, Any]] = {}  # job_id -> metadata
        self._job_history: Dict[str, List[Dict[str, Any]]] = {}  # job_id -> execution history
        self._sync_handler: Optional[Callable] = None

    @classmethod
    def get_instance(cls) -> "SchedulerService":
        """싱글턴 인스턴스 반환"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_sync_handler(self, handler: Callable) -> None:
        """
        동기화 핸들러 설정

        Args:
            handler: async def handler(tenant_id: str, job_type: str) -> None
        """
        self._sync_handler = handler

    def start(self) -> None:
        """스케줄러 시작"""
        if self._scheduler is not None and self._scheduler.running:
            logger.warning("스케줄러가 이미 실행 중입니다")
            return

        self._scheduler = AsyncIOScheduler(
            timezone="Asia/Seoul",
            job_defaults={
                "coalesce": True,  # 놓친 작업 합치기
                "max_instances": 1,  # 동시 실행 방지
                "misfire_grace_time": 60 * 5,  # 5분 유예
            },
        )

        # 이벤트 리스너 등록
        self._scheduler.add_listener(
            self._on_job_executed,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
        )

        self._scheduler.start()
        logger.info("스케줄러 시작됨")

    def shutdown(self, wait: bool = True) -> None:
        """스케줄러 종료"""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=wait)
            self._scheduler = None
            logger.info("스케줄러 종료됨")

    @property
    def is_running(self) -> bool:
        """스케줄러 실행 상태"""
        return self._scheduler is not None and self._scheduler.running

    # =========================================================================
    # Job Management
    # =========================================================================

    def add_job(
        self,
        tenant_id: str,
        job_type: str,
        cron_expression: str,
        enabled: bool = True,
    ) -> SchedulerJobInfo:
        """
        동기화 작업 추가

        Args:
            tenant_id: 테넌트 ID
            job_type: 작업 유형 (sync_tickets, sync_articles, sync_all)
            cron_expression: 크론 표현식 (예: "0 */6 * * *")
            enabled: 활성화 여부

        Returns:
            생성된 작업 정보
        """
        if self._scheduler is None:
            raise RuntimeError("스케줄러가 시작되지 않았습니다")

        job_id = f"{tenant_id}_{job_type}_{uuid.uuid4().hex[:8]}"

        # 크론 트리거 파싱
        try:
            trigger = CronTrigger.from_crontab(cron_expression)
        except ValueError as e:
            raise ValueError(f"잘못된 크론 표현식: {cron_expression} - {e}")

        # 메타데이터 저장
        self._job_metadata[job_id] = {
            "tenant_id": tenant_id,
            "job_type": job_type,
            "cron_expression": cron_expression,
            "enabled": enabled,
            "created_at": datetime.utcnow().isoformat(),
        }

        # 활성화된 경우에만 스케줄러에 등록
        if enabled:
            self._scheduler.add_job(
                func=self._execute_sync_job,
                trigger=trigger,
                id=job_id,
                args=[job_id, tenant_id, job_type],
                name=f"Sync {job_type} for {tenant_id}",
            )

        logger.info(f"작업 추가됨: {job_id} (enabled={enabled})")

        return self._build_job_info(job_id)

    def remove_job(self, job_id: str) -> None:
        """
        작업 제거

        Args:
            job_id: 작업 ID
        """
        if job_id not in self._job_metadata:
            raise ValueError(f"작업을 찾을 수 없습니다: {job_id}")

        # 스케줄러에서 제거
        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass  # 이미 없는 경우 무시

        # 메타데이터 제거
        del self._job_metadata[job_id]

        # 히스토리 유지 (옵션)
        if job_id in self._job_history:
            del self._job_history[job_id]

        logger.info(f"작업 제거됨: {job_id}")

    def toggle_job(self, job_id: str, enabled: bool) -> SchedulerJobInfo:
        """
        작업 활성화/비활성화

        Args:
            job_id: 작업 ID
            enabled: 활성화 여부

        Returns:
            업데이트된 작업 정보
        """
        if job_id not in self._job_metadata:
            raise ValueError(f"작업을 찾을 수 없습니다: {job_id}")

        metadata = self._job_metadata[job_id]
        old_enabled = metadata.get("enabled", True)

        if old_enabled == enabled:
            return self._build_job_info(job_id)

        metadata["enabled"] = enabled

        if self._scheduler is not None:
            if enabled and not old_enabled:
                # 활성화: 스케줄러에 추가
                trigger = CronTrigger.from_crontab(metadata["cron_expression"])
                self._scheduler.add_job(
                    func=self._execute_sync_job,
                    trigger=trigger,
                    id=job_id,
                    args=[job_id, metadata["tenant_id"], metadata["job_type"]],
                    name=f"Sync {metadata['job_type']} for {metadata['tenant_id']}",
                )
            elif not enabled and old_enabled:
                # 비활성화: 스케줄러에서 제거
                try:
                    self._scheduler.remove_job(job_id)
                except Exception:
                    pass

        logger.info(f"작업 토글됨: {job_id} (enabled={enabled})")

        return self._build_job_info(job_id)

    def pause_job(self, job_id: str) -> None:
        """작업 일시 정지"""
        if self._scheduler is not None:
            self._scheduler.pause_job(job_id)
            logger.info(f"작업 일시 정지됨: {job_id}")

    def resume_job(self, job_id: str) -> None:
        """작업 재개"""
        if self._scheduler is not None:
            self._scheduler.resume_job(job_id)
            logger.info(f"작업 재개됨: {job_id}")

    def trigger_job_now(self, job_id: str) -> None:
        """작업 즉시 실행"""
        if job_id not in self._job_metadata:
            raise ValueError(f"작업을 찾을 수 없습니다: {job_id}")

        metadata = self._job_metadata[job_id]

        if self._scheduler is not None:
            # 일회성 작업으로 즉시 실행
            self._scheduler.add_job(
                func=self._execute_sync_job,
                trigger="date",  # 즉시 실행
                id=f"{job_id}_manual_{uuid.uuid4().hex[:4]}",
                args=[job_id, metadata["tenant_id"], metadata["job_type"]],
                name=f"Manual sync for {metadata['tenant_id']}",
            )
            logger.info(f"작업 즉시 실행 트리거됨: {job_id}")

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_job(self, job_id: str) -> Optional[SchedulerJobInfo]:
        """작업 정보 조회"""
        if job_id not in self._job_metadata:
            return None
        return self._build_job_info(job_id)

    def list_jobs(self, tenant_id: Optional[str] = None) -> List[SchedulerJobInfo]:
        """
        작업 목록 조회

        Args:
            tenant_id: 특정 테넌트 필터 (None이면 전체)

        Returns:
            작업 목록
        """
        jobs = []
        for job_id in self._job_metadata:
            if tenant_id is None or self._job_metadata[job_id]["tenant_id"] == tenant_id:
                jobs.append(self._build_job_info(job_id))
        return jobs

    def get_status(self) -> SchedulerStatus:
        """스케줄러 상태 조회"""
        jobs = self.list_jobs()
        return SchedulerStatus(
            running=self.is_running,
            total_jobs=len(jobs),
            jobs=jobs,
        )

    def get_job_history(self, job_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """작업 실행 히스토리 조회"""
        history = self._job_history.get(job_id, [])
        return history[-limit:]

    # =========================================================================
    # Internal Methods
    # =========================================================================

    async def _execute_sync_job(self, job_id: str, tenant_id: str, job_type: str) -> None:
        """
        동기화 작업 실행

        실제 동기화 로직은 sync_handler에 위임
        """
        start_time = datetime.utcnow()
        logger.info(f"동기화 작업 시작: job={job_id}, tenant={tenant_id}, type={job_type}")

        try:
            if self._sync_handler is not None:
                await self._sync_handler(tenant_id, job_type)

            # 성공 기록
            self._record_execution(job_id, {
                "status": "success",
                "started_at": start_time.isoformat(),
                "completed_at": datetime.utcnow().isoformat(),
            })

            logger.info(f"동기화 작업 완료: job={job_id}")

        except Exception as e:
            # 실패 기록
            self._record_execution(job_id, {
                "status": "failed",
                "started_at": start_time.isoformat(),
                "completed_at": datetime.utcnow().isoformat(),
                "error": str(e),
            })

            logger.error(f"동기화 작업 실패: job={job_id}, error={e}")
            raise

    def _on_job_executed(self, event: JobExecutionEvent) -> None:
        """작업 실행 이벤트 핸들러"""
        job_id = event.job_id

        # manual 작업인 경우 원본 job_id 추출
        if "_manual_" in job_id:
            job_id = job_id.rsplit("_manual_", 1)[0]

        if job_id in self._job_metadata:
            self._job_metadata[job_id]["last_run_time"] = datetime.utcnow().isoformat()

    def _record_execution(self, job_id: str, record: Dict[str, Any]) -> None:
        """실행 기록 저장"""
        if job_id not in self._job_history:
            self._job_history[job_id] = []

        self._job_history[job_id].append(record)

        # 최대 100개까지만 유지
        if len(self._job_history[job_id]) > 100:
            self._job_history[job_id] = self._job_history[job_id][-100:]

    def _build_job_info(self, job_id: str) -> SchedulerJobInfo:
        """작업 정보 빌드"""
        metadata = self._job_metadata[job_id]

        # 다음 실행 시간 계산
        next_run_time = None
        if self._scheduler is not None and metadata.get("enabled", True):
            job: Optional[Job] = self._scheduler.get_job(job_id)
            if job is not None and job.next_run_time is not None:
                next_run_time = job.next_run_time.isoformat()

        return SchedulerJobInfo(
            job_id=job_id,
            tenant_id=metadata["tenant_id"],
            job_type=metadata["job_type"],
            cron_expression=metadata["cron_expression"],
            next_run_time=next_run_time,
            last_run_time=metadata.get("last_run_time"),
            enabled=metadata.get("enabled", True),
        )


# =============================================================================
# Dependency Injection
# =============================================================================

def get_scheduler_service() -> SchedulerService:
    """SchedulerService 의존성 주입"""
    return SchedulerService.get_instance()
