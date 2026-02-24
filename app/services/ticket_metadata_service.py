"""
Ticket Metadata Service - Supabase 메타데이터 저장

TypeScript google-file-saerch-tool/src/lib/supabase/ticket-metadata-service.ts 포팅

기능:
- 티켓/아티클 메타데이터 Supabase 저장 (upsert)
- 날짜 범위 필터링 (Gemini 검색 전 사전 필터링)
- 담당자/요청자 목록 조회 (자동완성용)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from supabase import create_client, Client

logger = logging.getLogger(__name__)


@dataclass
class TicketMetadataRecord:
    """티켓 메타데이터 레코드"""
    platform: str
    ticket_id: int
    external_id: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    source: Optional[str] = None
    requester: Optional[str] = None
    requester_id: Optional[int] = None
    responder: Optional[str] = None
    responder_id: Optional[int] = None
    group_name: Optional[str] = None
    group_id: Optional[int] = None
    tags: Optional[List[str]] = None
    ticket_created_at: Optional[str] = None
    ticket_updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Supabase upsert용 딕셔너리 변환"""
        return {
            "platform": self.platform,
            "ticket_id": self.ticket_id,
            "external_id": self.external_id,
            "status": self.status,
            "priority": self.priority,
            "source": self.source,
            "requester": self.requester,
            "requester_id": self.requester_id,
            "responder": self.responder,
            "responder_id": self.responder_id,
            "group_name": self.group_name,
            "group_id": self.group_id,
            "tags": self.tags,
            "ticket_created_at": self.ticket_created_at,
            "ticket_updated_at": self.ticket_updated_at,
        }


@dataclass
class ArticleMetadataRecord:
    """아티클 메타데이터 레코드"""
    platform: str
    article_id: int
    external_id: Optional[str] = None
    title: Optional[str] = None
    folder_id: Optional[int] = None
    folder_name: Optional[str] = None
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    status: Optional[str] = None
    article_created_at: Optional[str] = None
    article_updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Supabase upsert용 딕셔너리 변환"""
        return {
            "platform": self.platform,
            "article_id": self.article_id,
            "external_id": self.external_id,
            "title": self.title,
            "folder_id": self.folder_id,
            "folder_name": self.folder_name,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "status": self.status,
            "article_created_at": self.article_created_at,
            "article_updated_at": self.article_updated_at,
        }


@dataclass
class DateFilterOptions:
    """날짜 필터 옵션"""
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    limit: int = 1000


@dataclass
class UpsertResult:
    """Upsert 결과"""
    success: int = 0
    failed: int = 0


class TicketMetadataService:
    """
    Supabase 티켓/아티클 메타데이터 서비스
    
    사용:
        service = TicketMetadataService(
            supabase_url="https://xxx.supabase.co",
            supabase_key="service_role_key",
            tenant_slug="wedosoft",
            platform="freshdesk",
        )
        
        # 티켓 메타데이터 저장
        await service.upsert_tickets(ticket_records)
        
        # 날짜 범위로 티켓 ID 조회
        ticket_ids = await service.get_ticket_ids_by_date_range(
            DateFilterOptions(start_date=datetime(2024, 1, 1))
        )
    """
    
    BATCH_SIZE = 100
    
    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        tenant_slug: str,
        platform: str = "freshdesk",
    ):
        self.client: Client = create_client(supabase_url, supabase_key)
        self.tenant_slug = tenant_slug
        self.platform = platform
        self._tenant_uuid: Optional[str] = None
    
    async def _get_tenant_uuid(self) -> Optional[str]:
        """
        테넌트 UUID 조회 (캐시됨)
        없으면 자동 생성
        """
        if self._tenant_uuid:
            return self._tenant_uuid
        
        try:
            # 1. 기존 테넌트 조회
            result = self.client.table("tenants").select("id").eq("slug", self.tenant_slug).execute()
            
            if result.data and len(result.data) > 0:
                self._tenant_uuid = result.data[0]["id"]
                logger.info(f"Found tenant '{self.tenant_slug}' with UUID '{self._tenant_uuid}'")
                return self._tenant_uuid
            
            # 2. 없으면 자동 생성
            logger.info(f"Tenant '{self.tenant_slug}' not found, auto-creating...")
            
            insert_result = self.client.table("tenants").insert({
                "slug": self.tenant_slug,
                "name": self.tenant_slug,
                "plan": "basic",
            }).execute()
            
            if insert_result.data and len(insert_result.data) > 0:
                self._tenant_uuid = insert_result.data[0]["id"]
                logger.info(f"Auto-created tenant '{self.tenant_slug}' with UUID '{self._tenant_uuid}'")
                return self._tenant_uuid
            
            logger.error(f"Failed to create tenant: {self.tenant_slug}")
            return None
            
        except Exception as e:
            logger.error(f"Exception getting tenant UUID: {e}")
            return None
    
    async def upsert_tickets(self, tickets: List[TicketMetadataRecord]) -> UpsertResult:
        """
        티켓 메타데이터 배치 upsert
        """
        if not tickets:
            return UpsertResult()
        
        tenant_uuid = await self._get_tenant_uuid()
        if not tenant_uuid:
            logger.error("Cannot upsert tickets: tenant UUID not found")
            return UpsertResult(failed=len(tickets))
        
        result = UpsertResult()
        
        # tenant_id 추가
        records = []
        for t in tickets:
            record = t.to_dict()
            record["tenant_id"] = tenant_uuid
            records.append(record)
        
        # 배치 처리
        for i in range(0, len(records), self.BATCH_SIZE):
            batch = records[i:i + self.BATCH_SIZE]
            
            try:
                self.client.table("ticket_metadata").upsert(
                    batch,
                    on_conflict="tenant_id,platform,ticket_id",
                ).execute()
                
                result.success += len(batch)
            except Exception as e:
                logger.error(f"Failed to upsert ticket batch {i}: {e}")
                result.failed += len(batch)
        
        logger.info(f"Upserted ticket metadata: {result.success} success, {result.failed} failed")
        return result
    
    async def upsert_articles(self, articles: List[ArticleMetadataRecord]) -> UpsertResult:
        """
        아티클 메타데이터 배치 upsert
        """
        if not articles:
            return UpsertResult()
        
        tenant_uuid = await self._get_tenant_uuid()
        if not tenant_uuid:
            logger.error("Cannot upsert articles: tenant UUID not found")
            return UpsertResult(failed=len(articles))
        
        result = UpsertResult()
        
        # tenant_id 추가
        records = []
        for a in articles:
            record = a.to_dict()
            record["tenant_id"] = tenant_uuid
            records.append(record)
        
        # 배치 처리
        for i in range(0, len(records), self.BATCH_SIZE):
            batch = records[i:i + self.BATCH_SIZE]
            
            try:
                self.client.table("article_metadata").upsert(
                    batch,
                    on_conflict="tenant_id,platform,article_id",
                ).execute()
                
                result.success += len(batch)
            except Exception as e:
                logger.error(f"Failed to upsert article batch {i}: {e}")
                result.failed += len(batch)
        
        logger.info(f"Upserted article metadata: {result.success} success, {result.failed} failed")
        return result
    
    async def get_ticket_ids_by_date_range(
        self,
        options: Optional[DateFilterOptions] = None,
    ) -> List[int]:
        """
        날짜 범위로 티켓 ID 조회
        Gemini 검색 전 사전 필터링용
        """
        tenant_uuid = await self._get_tenant_uuid()
        if not tenant_uuid:
            logger.error("Cannot get ticket IDs: tenant UUID not found")
            return []
        
        opts = options or DateFilterOptions()
        
        try:
            query = (
                self.client.table("ticket_metadata")
                .select("ticket_id")
                .eq("tenant_id", tenant_uuid)
                .eq("platform", self.platform)
                .order("ticket_updated_at", desc=True)
                .limit(opts.limit)
            )
            
            if opts.start_date:
                query = query.gte("ticket_created_at", opts.start_date.isoformat())
            if opts.end_date:
                query = query.lte("ticket_created_at", opts.end_date.isoformat())
            if opts.status:
                query = query.eq("status", opts.status)
            if opts.priority:
                query = query.eq("priority", opts.priority)
            
            result = query.execute()
            
            return [row["ticket_id"] for row in (result.data or [])]
            
        except Exception as e:
            logger.error(f"Failed to get ticket IDs by date range: {e}")
            return []
    
    async def get_article_ids_by_date_range(
        self,
        options: Optional[DateFilterOptions] = None,
    ) -> List[int]:
        """
        날짜 범위로 아티클 ID 조회
        """
        tenant_uuid = await self._get_tenant_uuid()
        if not tenant_uuid:
            logger.error("Cannot get article IDs: tenant UUID not found")
            return []
        
        opts = options or DateFilterOptions(limit=500)
        
        try:
            query = (
                self.client.table("article_metadata")
                .select("article_id")
                .eq("tenant_id", tenant_uuid)
                .eq("platform", self.platform)
                .order("article_updated_at", desc=True)
                .limit(opts.limit)
            )
            
            if opts.start_date:
                query = query.gte("article_created_at", opts.start_date.isoformat())
            if opts.end_date:
                query = query.lte("article_created_at", opts.end_date.isoformat())
            
            result = query.execute()
            
            return [row["article_id"] for row in (result.data or [])]
            
        except Exception as e:
            logger.error(f"Failed to get article IDs by date range: {e}")
            return []
    
    async def get_ticket_count(self) -> int:
        """테넌트의 티켓 수 조회"""
        tenant_uuid = await self._get_tenant_uuid()
        if not tenant_uuid:
            return 0
        
        try:
            result = (
                self.client.table("ticket_metadata")
                .select("*", count="exact", head=True)
                .eq("tenant_id", tenant_uuid)
                .eq("platform", self.platform)
                .execute()
            )
            
            return result.count or 0
            
        except Exception as e:
            logger.error(f"Failed to get ticket count: {e}")
            return 0
    
    async def get_distinct_requesters(self) -> List[str]:
        """고유 요청자 목록 조회 (자동완성용)"""
        tenant_uuid = await self._get_tenant_uuid()
        if not tenant_uuid:
            return []
        
        try:
            result = (
                self.client.table("ticket_metadata")
                .select("requester")
                .eq("tenant_id", tenant_uuid)
                .eq("platform", self.platform)
                .not_.is_("requester", "null")
                .execute()
            )
            
            # 중복 제거
            unique = list(set(
                row["requester"]
                for row in (result.data or [])
                if row.get("requester")
            ))
            
            logger.info(f"Loaded {len(unique)} distinct requesters")
            return unique
            
        except Exception as e:
            logger.error(f"Failed to get distinct requesters: {e}")
            return []
    
    async def get_distinct_responders(self) -> List[str]:
        """고유 담당자 목록 조회 (자동완성용)"""
        tenant_uuid = await self._get_tenant_uuid()
        if not tenant_uuid:
            return []
        
        try:
            result = (
                self.client.table("ticket_metadata")
                .select("responder")
                .eq("tenant_id", tenant_uuid)
                .eq("platform", self.platform)
                .not_.is_("responder", "null")
                .execute()
            )
            
            # 중복 제거
            unique = list(set(
                row["responder"]
                for row in (result.data or [])
                if row.get("responder")
            ))
            
            logger.info(f"Loaded {len(unique)} distinct responders")
            return unique
            
        except Exception as e:
            logger.error(f"Failed to get distinct responders: {e}")
            return []
    
    async def clear_ticket_metadata(self) -> None:
        """티켓 메타데이터 전체 삭제 (재동기화용)"""
        tenant_uuid = await self._get_tenant_uuid()
        if not tenant_uuid:
            raise ValueError("Tenant UUID not found")
        
        try:
            self.client.table("ticket_metadata").delete().eq(
                "tenant_id", tenant_uuid
            ).eq("platform", self.platform).execute()
            
            logger.info("Cleared ticket metadata for tenant")
            
        except Exception as e:
            logger.error(f"Failed to clear ticket metadata: {e}")
            raise
    
    async def clear_article_metadata(self) -> None:
        """아티클 메타데이터 전체 삭제 (재동기화용)"""
        tenant_uuid = await self._get_tenant_uuid()
        if not tenant_uuid:
            raise ValueError("Tenant UUID not found")
        
        try:
            self.client.table("article_metadata").delete().eq(
                "tenant_id", tenant_uuid
            ).eq("platform", self.platform).execute()
            
            logger.info("Cleared article metadata for tenant")
            
        except Exception as e:
            logger.error(f"Failed to clear article metadata: {e}")
            raise


def create_ticket_metadata_service(
    supabase_url: str,
    supabase_key: str,
    tenant_slug: str,
    platform: str = "freshdesk",
) -> TicketMetadataService:
    """TicketMetadataService 팩토리 함수"""
    return TicketMetadataService(
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        tenant_slug=tenant_slug,
        platform=platform,
    )
