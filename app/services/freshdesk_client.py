"""
Freshdesk API Client - 고성능 병렬 수집 지원

TypeScript google-file-saerch-tool/src/lib/freshdesk/client.ts 완전 포팅
- aiohttp 커넥션 풀링
- Rate Limit 자동 처리 (Retry-After 파싱)
- 페이지네이션 지원
- 병렬 수집 메서드
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Rate Limit 설정 (TypeScript와 동일)
RATE_LIMIT_FALLBACK_SECONDS = 60
RATE_LIMIT_MIN_SECONDS = 5
RATE_LIMIT_MAX_SECONDS = 3600


class FreshdeskClientError(RuntimeError):
    """Freshdesk API 일반 에러"""
    pass


class RateLimitError(FreshdeskClientError):
    """Rate Limit 에러 (429)"""
    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


class FreshdeskClient:
    """
    고성능 Freshdesk API 클라이언트
    
    Features:
    - httpx 커넥션 풀링 (세션 재사용)
    - Rate Limit 자동 대기
    - 페이지네이션 자동 처리
    - 대화 전체 수집 (30개 제한 우회)
    """
    
    def __init__(
        self, 
        domain: str, 
        api_key: str, 
        *, 
        timeout: float = 30.0,
        rate_limit_delay_ms: int = 100
    ) -> None:
        if not domain or not api_key:
            raise FreshdeskClientError("Freshdesk domain/API key required")
        
        # 도메인 정규화
        normalized = domain.replace("https://", "").replace("http://", "").rstrip("/")
        if not normalized.endswith(".freshdesk.com"):
            normalized = f"{normalized}.freshdesk.com"
        
        self.base_url = f"https://{normalized}/api/v2"
        self.api_key = api_key
        self.timeout = timeout
        self.rate_limit_delay_ms = rate_limit_delay_ms
        
        # Rate Limit 상태
        self._rate_limit_reset_at: Optional[float] = None
        
        # 커넥션 풀 (세션 재사용)
        self._client: Optional[httpx.AsyncClient] = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        """커넥션 풀 클라이언트 반환 (lazy init)"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                auth=(self.api_key, "X"),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client
    
    async def close(self) -> None:
        """클라이언트 종료"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    # =========================================================================
    # 티켓 API
    # =========================================================================
    
    async def get_tickets(
        self,
        *,
        page: int = 1,
        per_page: int = 100,
        updated_since: Optional[datetime] = None,
        include_fields: Optional[List[str]] = None
    ) -> List[dict[str, Any]]:
        """
        티켓 목록 조회 (단일 페이지)
        
        Args:
            page: 페이지 번호 (1부터 시작)
            per_page: 페이지당 개수 (최대 100)
            updated_since: 이 시각 이후 업데이트된 티켓만
            include_fields: 추가 필드 (description, requester, company, stats)
        """
        params = {
            "page": page,
            "per_page": min(per_page, 100),
        }
        
        if updated_since:
            params["updated_since"] = updated_since.isoformat()
        
        if include_fields:
            allowed = {"description", "requester", "company", "stats"}
            valid = [f for f in include_fields if f in allowed]
            if valid:
                params["include"] = ",".join(valid)
        
        return await self._request("GET", "/tickets", params=params)
    
    async def get_all_tickets(
        self,
        *,
        updated_since: Optional[datetime] = None,
        include_fields: Optional[List[str]] = None,
        per_page: int = 100
    ) -> List[dict[str, Any]]:
        """
        전체 티켓 조회 (자동 페이지네이션)
        
        Args:
            updated_since: 증분 동기화 시작점
            include_fields: 추가 필드
            per_page: 페이지당 개수
        """
        all_tickets: List[dict] = []
        page = 1
        
        logger.info(f"Fetching all tickets (updated_since={updated_since})")
        
        while True:
            tickets = await self.get_tickets(
                page=page,
                per_page=per_page,
                updated_since=updated_since,
                include_fields=include_fields,
            )
            
            if not tickets:
                break
            
            all_tickets.extend(tickets)
            logger.info(f"Fetched page {page}: {len(tickets)} tickets (total: {len(all_tickets)})")
            
            if len(tickets) < per_page:
                break
            
            page += 1
            await self._rate_limit_delay()
        
        logger.info(f"Fetched {len(all_tickets)} tickets total")
        return all_tickets
    
    async def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        """단일 티켓 조회"""
        return await self._request("GET", f"/tickets/{ticket_id}")
    
    # =========================================================================
    # 대화 API (Freshdesk는 페이지당 30개 제한)
    # =========================================================================
    
    async def get_conversations(
        self,
        ticket_id: int,
        *,
        page: int = 1,
        per_page: int = 30
    ) -> List[dict[str, Any]]:
        """
        티켓 대화 조회 (단일 페이지)
        
        Note: Freshdesk는 대화 API에서 페이지당 최대 30개만 반환
        """
        params = {
            "page": page,
            "per_page": min(per_page, 30),
        }
        return await self._request("GET", f"/tickets/{ticket_id}/conversations", params=params)
    
    async def get_all_conversations(self, ticket_id: int) -> List[dict[str, Any]]:
        """
        티켓의 전체 대화 조회 (자동 페이지네이션)
        
        50개 이상의 대화도 모두 가져옴 (30개 제한 우회)
        """
        all_conversations: List[dict] = []
        page = 1
        
        while True:
            conversations = await self.get_conversations(ticket_id, page=page, per_page=30)
            
            if not conversations:
                break
            
            all_conversations.extend(conversations)
            
            # 30개 미만이면 마지막 페이지
            if len(conversations) < 30:
                break
            
            page += 1
            await self._rate_limit_delay()
        
        return all_conversations
    
    # =========================================================================
    # 아티클 API
    # =========================================================================
    
    async def get_folder_articles(
        self,
        folder_id: int,
        *,
        page: int = 1,
        per_page: int = 100,
        updated_since: Optional[datetime] = None
    ) -> List[dict[str, Any]]:
        """폴더 내 아티클 조회 (단일 페이지)"""
        params = {
            "page": page,
            "per_page": min(per_page, 100),
        }
        
        if updated_since:
            params["updated_since"] = updated_since.isoformat()
        
        return await self._request("GET", f"/solutions/folders/{folder_id}/articles", params=params)
    
    async def get_all_articles(
        self,
        *,
        updated_since: Optional[datetime] = None
    ) -> List[dict[str, Any]]:
        """
        전체 아티클 조회 (카테고리 → 폴더 순회)
        
        모든 카테고리의 모든 폴더를 순회하여 아티클 수집
        """
        all_articles: List[dict] = []
        
        logger.info(f"Fetching all articles (updated_since={updated_since})")
        
        # 카테고리 조회
        categories = await self.get_categories()
        logger.debug(f"Found {len(categories)} categories")
        
        for category in categories:
            category_id = category["id"]
            
            # 카테고리의 폴더 조회
            try:
                folders = await self.get_folders(category_id)
            except FreshdeskClientError as e:
                logger.warning(f"Failed to fetch folders for category {category_id}: {e}")
                continue
            
            for folder in folders:
                folder_id = folder["id"]
                page = 1
                
                while True:
                    try:
                        articles = await self.get_folder_articles(
                            folder_id,
                            page=page,
                            per_page=100,
                            updated_since=updated_since,
                        )
                    except FreshdeskClientError as e:
                        logger.warning(f"Failed to fetch articles for folder {folder_id}: {e}")
                        break
                    
                    if not articles:
                        break
                    
                    # 폴더/카테고리 정보 추가
                    for article in articles:
                        article["folder_id"] = folder_id
                        article["folder_name"] = folder.get("name")
                        article["category_id"] = category_id
                        article["category_name"] = category.get("name")
                    
                    all_articles.extend(articles)
                    
                    if len(articles) < 100:
                        break
                    
                    page += 1
                    await self._rate_limit_delay()
        
        logger.info(f"Fetched {len(all_articles)} articles total")
        return all_articles
    
    async def get_article(self, article_id: int) -> dict[str, Any]:
        """단일 아티클 조회"""
        return await self._request("GET", f"/solutions/articles/{article_id}")
    
    # =========================================================================
    # 엔티티 API (에이전트, 그룹, 연락처, 회사 등)
    # =========================================================================
    
    async def get_agents(self) -> List[dict[str, Any]]:
        """전체 에이전트 조회 (단일 페이지)"""
        return await self._request("GET", "/agents")
    
    async def get_all_agents(self) -> List[dict[str, Any]]:
        """전체 에이전트 조회 (자동 페이지네이션)"""
        all_agents: List[dict] = []
        page = 1
        
        while True:
            params = {"page": page, "per_page": 100}
            agents = await self._request("GET", "/agents", params=params)
            
            if not agents:
                break
            
            all_agents.extend(agents)
            
            if len(agents) < 100:
                break
            
            page += 1
            await self._rate_limit_delay()
        
        return all_agents
    
    async def get_agent(self, agent_id: int) -> Optional[dict[str, Any]]:
        """단일 에이전트 조회"""
        try:
            return await self._request("GET", f"/agents/{agent_id}")
        except FreshdeskClientError as e:
            if "404" in str(e):
                return None
            raise
    
    async def get_groups(self) -> List[dict[str, Any]]:
        """전체 그룹 조회 (단일 페이지)"""
        return await self._request("GET", "/groups")
    
    async def get_all_groups(self) -> List[dict[str, Any]]:
        """전체 그룹 조회 (자동 페이지네이션)"""
        all_groups: List[dict] = []
        page = 1
        
        while True:
            params = {"page": page, "per_page": 100}
            groups = await self._request("GET", "/groups", params=params)
            
            if not groups:
                break
            
            all_groups.extend(groups)
            
            if len(groups) < 100:
                break
            
            page += 1
            await self._rate_limit_delay()
        
        return all_groups
    
    async def get_categories(self) -> List[dict[str, Any]]:
        """전체 솔루션 카테고리 조회 (단일 페이지)"""
        return await self._request("GET", "/solutions/categories")
    
    async def get_all_categories(self) -> List[dict[str, Any]]:
        """전체 솔루션 카테고리 조회 (자동 페이지네이션)"""
        all_categories: List[dict] = []
        page = 1
        
        while True:
            params = {"page": page, "per_page": 100}
            categories = await self._request("GET", "/solutions/categories", params=params)
            
            if not categories:
                break
            
            all_categories.extend(categories)
            
            if len(categories) < 100:
                break
            
            page += 1
            await self._rate_limit_delay()
        
        return all_categories
    
    async def get_folders(self, category_id: int) -> List[dict[str, Any]]:
        """카테고리 내 폴더 조회"""
        return await self._request("GET", f"/solutions/categories/{category_id}/folders")
    
    async def get_folders_for_category(self, category_id: int) -> List[dict[str, Any]]:
        """카테고리 내 폴더 조회 (alias)"""
        return await self.get_folders(category_id)
    
    async def get_ticket_fields(self) -> List[dict[str, Any]]:
        """티켓 필드 정의 조회 (status, priority, source choices 포함)"""
        return await self._request("GET", "/ticket_fields")
    
    async def get_contact(self, contact_id: int) -> Optional[dict[str, Any]]:
        """단일 연락처 조회"""
        try:
            return await self._request("GET", f"/contacts/{contact_id}")
        except FreshdeskClientError as e:
            if "404" in str(e):
                return None
            raise
    
    async def get_contacts(
        self,
        *,
        page: int = 1,
        per_page: int = 100
    ) -> List[dict[str, Any]]:
        """연락처 목록 조회"""
        params = {"page": page, "per_page": min(per_page, 100)}
        return await self._request("GET", "/contacts", params=params)
    
    async def get_company(self, company_id: int) -> Optional[dict[str, Any]]:
        """단일 회사 조회"""
        try:
            return await self._request("GET", f"/companies/{company_id}")
        except FreshdeskClientError as e:
            if "404" in str(e):
                return None
            raise
    
    async def get_products(self) -> List[dict[str, Any]]:
        """전체 제품 조회 (단일 페이지)"""
        return await self._request("GET", "/products")
    
    async def get_all_products(self) -> List[dict[str, Any]]:
        """전체 제품 조회 (자동 페이지네이션)"""
        all_products: List[dict] = []
        page = 1
        
        while True:
            params = {"page": page, "per_page": 100}
            products = await self._request("GET", "/products", params=params)
            
            if not products:
                break
            
            all_products.extend(products)
            
            if len(products) < 100:
                break
            
            page += 1
            await self._rate_limit_delay()
        
        return all_products
    
    # =========================================================================
    # 검색 API
    # =========================================================================
    
    async def search_tickets(self, query: str, page: int = 1) -> dict[str, Any]:
        """티켓 검색"""
        # 쿼리 이스케이프
        sanitized = query.strip().replace('"', '\\"')
        encoded_query = f'"{sanitized}"'
        params = {"query": encoded_query, "page": page}
        return await self._request("GET", "/search/tickets", params=params)
    
    async def search_contacts(self, query: str) -> dict[str, Any]:
        """연락처 검색"""
        return await self._request("GET", "/search/contacts", params={"query": query})
    
    async def search_agents(self, query: str) -> dict[str, Any]:
        """에이전트 검색"""
        return await self._request("GET", "/search/agents", params={"query": query})
    
    # =========================================================================
    # Health Check
    # =========================================================================
    
    async def health_check(self) -> bool:
        """API 연결 확인"""
        try:
            await self._request("GET", "/agents", params={"per_page": 1})
            return True
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False
    
    # =========================================================================
    # Internal Methods
    # =========================================================================
    
    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None
    ) -> Any:
        """
        HTTP 요청 실행 (Rate Limit 자동 처리)
        
        - 429 응답 시 Retry-After 파싱 후 대기
        - 자동 재시도 (최대 3회)
        """
        url = f"{self.base_url}{path}"
        max_attempts = 3
        
        for attempt in range(max_attempts):
            # Rate Limit 윈도우 대기
            await self._wait_for_rate_limit()
            
            try:
                client = await self._get_client()
                response = await client.request(method, url, params=params)
                
                # Rate Limit (429)
                if response.status_code == 429:
                    retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
                    self._schedule_rate_limit_wait(retry_after)
                    logger.warning(f"Rate limit hit, waiting {retry_after or RATE_LIMIT_FALLBACK_SECONDS}s...")
                    raise RateLimitError("Rate limit exceeded", retry_after)
                
                # 다른 에러
                if response.status_code >= 400:
                    # 502 Bad Gateway, 503 Service Unavailable 등 일시적인 서버 오류는 재시도
                    if response.status_code in [502, 503]:
                        logger.warning(f"Freshdesk {response.status_code} Server Error (attempt {attempt + 1}/{max_attempts}), retrying...")
                        if attempt < max_attempts - 1:
                            await asyncio.sleep(2.0 * (attempt + 1))
                            continue
                    
                    raise FreshdeskClientError(
                        f"Freshdesk API {method} {path} failed: {response.status_code} {response.text}"
                    )
                
                return response.json()
                
            except RateLimitError:
                # Rate Limit은 대기 후 재시도
                if attempt < max_attempts - 1:
                    continue
                raise
            
            except httpx.TimeoutException as e:
                logger.warning(f"Request timeout (attempt {attempt + 1}/{max_attempts}): {e}")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise FreshdeskClientError(f"Request timeout: {e}")
            
            except httpx.HTTPError as e:
                logger.error(f"HTTP error: {e}")
                raise FreshdeskClientError(f"HTTP error: {e}")
        
        raise FreshdeskClientError(f"Max retries exceeded for {method} {path}")
    
    async def _wait_for_rate_limit(self) -> None:
        """Rate Limit 윈도우 대기"""
        if self._rate_limit_reset_at is None:
            return
        
        import time
        remaining = self._rate_limit_reset_at - time.time()
        
        if remaining <= 0:
            self._rate_limit_reset_at = None
            return
        
        logger.warning(f"Waiting {remaining:.1f}s for rate limit window...")
        await asyncio.sleep(remaining)
        self._rate_limit_reset_at = None
    
    def _schedule_rate_limit_wait(self, retry_after: Optional[int]) -> None:
        """Rate Limit 대기 스케줄링"""
        import time
        
        seconds = retry_after or RATE_LIMIT_FALLBACK_SECONDS
        seconds = max(RATE_LIMIT_MIN_SECONDS, min(seconds, RATE_LIMIT_MAX_SECONDS))
        
        target = time.time() + seconds
        
        if self._rate_limit_reset_at is None or target > self._rate_limit_reset_at:
            self._rate_limit_reset_at = target
    
    def _parse_retry_after(self, header_value: Optional[str]) -> Optional[int]:
        """Retry-After 헤더 파싱"""
        if not header_value:
            return None
        
        # 숫자 (초)
        try:
            return int(header_value)
        except ValueError:
            pass
        
        # HTTP 날짜 형식
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(header_value)
            import time
            seconds = int(dt.timestamp() - time.time())
            return seconds if seconds > 0 else None
        except Exception:
            pass
        
        return None
    
    async def _rate_limit_delay(self) -> None:
        """API 호출 간 딜레이 (Rate Limit 방지)"""
        if self.rate_limit_delay_ms > 0:
            await asyncio.sleep(self.rate_limit_delay_ms / 1000.0)
