"""
AI Assistant 서비스

FDK Custom App의 티켓 분석, 승인, 수정 비즈니스 로직
Gemini File Search와 Freshdesk API 연동
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.models.assist import (
    AnalyzeRequest,
    ApproveRequest,
    RefineRequest,
    Proposal,
)
from app.services.gemini_file_search_client import GeminiFileSearchClient, GeminiClientError

logger = logging.getLogger(__name__)


# =============================================================================
# In-Memory Proposal Store (추후 Supabase로 교체 가능)
# =============================================================================

class ProposalStore:
    """제안 저장소 (In-memory)"""

    def __init__(self):
        self._proposals: Dict[str, Dict[str, Any]] = {}

    async def create(self, tenant_id: str, proposal_data: Dict[str, Any]) -> Dict[str, Any]:
        """제안 생성"""
        proposal_id = str(uuid.uuid4())
        proposal = {
            "id": proposal_id,
            "tenant_id": tenant_id,
            "proposal_version": 1,
            "status": "draft",
            "created_at": time.time(),
            **proposal_data,
        }
        self._proposals[proposal_id] = proposal
        return proposal

    async def get(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """제안 조회"""
        return self._proposals.get(proposal_id)

    async def update(self, proposal_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """제안 업데이트"""
        if proposal_id not in self._proposals:
            return None
        self._proposals[proposal_id].update(updates)
        return self._proposals[proposal_id]

    async def create_version(
        self, original_id: str, refined_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """새 버전 생성"""
        original = self._proposals.get(original_id)
        if not original:
            return None

        # 원본을 superseded로 변경
        original["status"] = "superseded"

        # 새 버전 생성
        new_id = str(uuid.uuid4())
        new_proposal = {
            "id": new_id,
            "tenant_id": original["tenant_id"],
            "ticket_id": original["ticket_id"],
            "proposal_version": original["proposal_version"] + 1,
            "status": "draft",
            "created_at": time.time(),
            **refined_data,
        }
        self._proposals[new_id] = new_proposal
        return new_proposal


# 전역 저장소 인스턴스
_proposal_store = ProposalStore()


# =============================================================================
# AssistService
# =============================================================================

class AssistService:
    """AI Assistant 서비스"""

    def __init__(
        self,
        gemini_client: GeminiFileSearchClient,
        proposal_store: ProposalStore,
    ):
        self.gemini_client = gemini_client
        self.proposal_store = proposal_store

    async def analyze(
        self,
        tenant_id: str,
        request: AnalyzeRequest,
        freshdesk_context: Optional[Dict[str, str]] = None,
    ) -> Proposal:
        """
        티켓 분석 (non-streaming)

        Args:
            tenant_id: 테넌트 ID
            request: 분석 요청
            freshdesk_context: Freshdesk 자격 증명 (domain, api_key)

        Returns:
            생성된 제안
        """
        start_time = time.time()

        # 티켓 컨텍스트 구성
        ticket_context = self._build_ticket_context(request, freshdesk_context)

        # Gemini를 통한 분석
        store_names = self._get_store_names(tenant_id)

        try:
            # RAG 검색
            search_query = f"{ticket_context.get('subject', '')} {ticket_context.get('description', '')}"
            search_result = await self.gemini_client.search(
                query=search_query,
                store_names=store_names,
            )

            # 응답 초안 생성
            draft_response = search_result.get("text", "죄송합니다. 관련 정보를 찾지 못했습니다.")
            grounding_chunks = search_result.get("grounding_chunks", [])

            # 필드 업데이트 추천 (간단한 휴리스틱)
            field_updates = self._suggest_field_updates(ticket_context, grounding_chunks)

            # 유사 사례 및 KB 참조 추출
            similar_cases = self._extract_similar_cases(grounding_chunks)
            kb_references = self._extract_kb_references(grounding_chunks)

            # 신뢰도 계산
            confidence = self._calculate_confidence(grounding_chunks)
            mode = "synthesis" if grounding_chunks else "direct"

        except GeminiClientError as e:
            logger.warning(f"Gemini 검색 실패, direct 모드로 전환: {e}")
            draft_response = self._generate_fallback_response(ticket_context)
            field_updates = {}
            similar_cases = []
            kb_references = []
            confidence = "low"
            mode = "fallback"

        analysis_time_ms = int((time.time() - start_time) * 1000)

        # 제안 저장
        proposal_data = {
            "ticket_id": request.ticket_id,
            "draft_response": draft_response,
            "field_updates": field_updates,
            "confidence": confidence,
            "mode": mode,
            "similar_cases": similar_cases,
            "kb_references": kb_references,
            "analysis_time_ms": analysis_time_ms,
            "reasoning": f"Analyzed ticket {request.ticket_id} with {len(grounding_chunks) if 'grounding_chunks' in dir() else 0} sources",
        }

        proposal = await self.proposal_store.create(tenant_id, proposal_data)
        return Proposal(**proposal)

    async def analyze_with_streaming(
        self,
        tenant_id: str,
        request: AnalyzeRequest,
        freshdesk_context: Optional[Dict[str, str]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        티켓 분석 (SSE 스트리밍)

        Args:
            tenant_id: 테넌트 ID
            request: 분석 요청
            freshdesk_context: Freshdesk 자격 증명

        Yields:
            SSE 이벤트
        """
        start_time = time.time()

        try:
            # 티켓 컨텍스트 구성
            ticket_context = self._build_ticket_context(request, freshdesk_context)
            store_names = self._get_store_names(tenant_id)

            # Event 1: 라우터 결정
            embedding_enabled = bool(store_names)
            yield {
                "type": "router_decision",
                "decision": "retrieve_cases" if embedding_enabled else "propose_solution_direct",
                "reasoning": "RAG 스토어 활성화됨" if embedding_enabled else "RAG 스토어 미설정",
                "embeddingEnabled": embedding_enabled,
            }

            grounding_chunks = []

            if embedding_enabled:
                # Event 2: 검색 시작
                yield {"type": "retriever_start", "mode": "embedding"}

                # RAG 검색 실행
                search_query = f"{ticket_context.get('subject', '')} {ticket_context.get('description', '')}"

                try:
                    search_result = await self.gemini_client.search(
                        query=search_query,
                        store_names=store_names,
                    )
                    grounding_chunks = search_result.get("grounding_chunks", [])
                    draft_response = search_result.get("text", "")

                    # Event 3: 검색 결과
                    yield {
                        "type": "retriever_results",
                        "similarCasesCount": len([c for c in grounding_chunks if "ticket" in str(c).lower()]),
                        "kbArticlesCount": len([c for c in grounding_chunks if "article" in str(c).lower()]),
                        "totalResults": len(grounding_chunks),
                    }

                except GeminiClientError as e:
                    logger.warning(f"검색 실패: {e}")
                    yield {
                        "type": "retriever_fallback",
                        "reason": str(e),
                    }
                    draft_response = ""

            # Event 4: 솔루션 생성 시작
            yield {"type": "resolution_start"}

            # 응답이 없으면 폴백 생성
            if not draft_response:
                draft_response = self._generate_fallback_response(ticket_context)

            # 필드 업데이트 추천
            field_updates = self._suggest_field_updates(ticket_context, grounding_chunks)

            # 유사 사례 및 KB 참조 추출
            similar_cases = self._extract_similar_cases(grounding_chunks)
            kb_references = self._extract_kb_references(grounding_chunks)

            # 신뢰도 계산
            confidence = self._calculate_confidence(grounding_chunks)
            mode = "synthesis" if grounding_chunks else "direct"

            analysis_time_ms = int((time.time() - start_time) * 1000)

            # 제안 저장
            proposal_data = {
                "ticket_id": request.ticket_id,
                "draft_response": draft_response,
                "field_updates": field_updates,
                "confidence": confidence,
                "mode": mode,
                "similar_cases": similar_cases,
                "kb_references": kb_references,
                "analysis_time_ms": analysis_time_ms,
            }

            proposal = await self.proposal_store.create(tenant_id, proposal_data)

            # Event 5: 솔루션 완료
            yield {
                "type": "resolution_complete",
                "proposalId": proposal["id"],
                "confidence": confidence,
                "mode": mode,
                "analysisTimeMs": analysis_time_ms,
                "proposal": proposal,
            }

        except Exception as e:
            logger.error(f"스트리밍 분석 오류: {e}", exc_info=True)
            yield {
                "type": "error",
                "message": str(e),
                "recoverable": False,
            }

    async def approve(
        self,
        tenant_id: str,
        request: ApproveRequest,
    ) -> Dict[str, Any]:
        """
        제안 승인 또는 거절

        Args:
            tenant_id: 테넌트 ID
            request: 승인 요청

        Returns:
            승인 결과
        """
        proposal = await self.proposal_store.get(request.proposal_id)
        if not proposal:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"제안을 찾을 수 없습니다: {request.proposal_id}",
            )

        if proposal["tenant_id"] != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="이 테넌트의 제안이 아닙니다",
            )

        if request.action == "approve":
            await self.proposal_store.update(
                request.proposal_id,
                {
                    "status": "approved",
                    "approved_by": request.agent_email,
                    "approved_at": time.time(),
                },
            )

            return {
                "status": "approved",
                "field_updates": proposal.get("field_updates"),
                "final_response": request.final_response or proposal.get("draft_response"),
            }

        elif request.action == "reject":
            await self.proposal_store.update(
                request.proposal_id,
                {
                    "status": "rejected",
                    "rejection_reason": request.rejection_reason,
                },
            )

            return {
                "status": "rejected",
                "reason": request.rejection_reason,
            }

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"잘못된 액션: {request.action}",
            )

    async def refine(
        self,
        tenant_id: str,
        request: RefineRequest,
    ) -> Dict[str, Any]:
        """
        제안 수정

        Args:
            tenant_id: 테넌트 ID
            request: 수정 요청

        Returns:
            새 버전의 제안
        """
        original = await self.proposal_store.get(request.proposal_id)
        if not original:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"제안을 찾을 수 없습니다: {request.proposal_id}",
            )

        if original["tenant_id"] != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="이 테넌트의 제안이 아닙니다",
            )

        # TODO: LLM을 통한 실제 수정 구현
        # 현재는 간단히 수정 요청을 첨부
        refined_response = f"{original['draft_response']}\n\n[수정 반영: {request.refinement_request}]"

        refined_data = {
            "draft_response": refined_response,
            "field_updates": original.get("field_updates"),
            "confidence": original.get("confidence"),
            "mode": original.get("mode"),
            "reasoning": f"Refined from version {original['proposal_version']}: {request.refinement_request}",
        }

        new_proposal = await self.proposal_store.create_version(
            request.proposal_id,
            refined_data,
        )

        return {
            "proposal": Proposal(**new_proposal),
            "version": new_proposal["proposal_version"],
        }

    async def get_proposal(
        self,
        tenant_id: str,
        proposal_id: str,
    ) -> Optional[Proposal]:
        """제안 조회"""
        proposal = await self.proposal_store.get(proposal_id)
        if not proposal:
            return None

        if proposal["tenant_id"] != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="이 테넌트의 제안이 아닙니다",
            )

        return Proposal(**proposal)

    # =========================================================================
    # Private Helper Methods
    # =========================================================================

    def _build_ticket_context(
        self,
        request: AnalyzeRequest,
        freshdesk_context: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        """티켓 컨텍스트 구성"""
        context = {
            "id": request.ticket_id,
            "subject": request.subject or "",
            "description": request.description or "",
            "priority": request.priority,
            "status": request.status,
            "tags": request.tags or [],
        }

        # TODO: freshdesk_context가 있으면 Freshdesk API에서 추가 정보 가져오기
        if freshdesk_context:
            logger.info(f"Freshdesk context available for domain: {freshdesk_context.get('domain')}")

        return context

    def _get_store_names(self, tenant_id: str) -> List[str]:
        """테넌트의 RAG 스토어 목록 반환"""
        settings = get_settings()
        stores = []

        if settings.gemini_store_tickets:
            stores.append(settings.gemini_store_tickets)
        if settings.gemini_store_articles:
            stores.append(settings.gemini_store_articles)
        if settings.gemini_store_common:
            stores.append(settings.gemini_store_common)

        # TODO: 테넌트별 스토어 설정 조회
        return stores

    def _suggest_field_updates(
        self,
        ticket_context: Dict[str, Any],
        grounding_chunks: List[Any],
    ) -> Dict[str, Any]:
        """필드 업데이트 추천"""
        updates = {}

        # 간단한 휴리스틱 예시
        description = str(ticket_context.get("description", "")).lower()

        if "urgent" in description or "긴급" in description:
            updates["priority"] = {"old": ticket_context.get("priority"), "new": 4}

        if "error" in description or "오류" in description or "버그" in description:
            updates["tags"] = {"action": "add", "value": "bug"}

        return updates

    def _extract_similar_cases(self, grounding_chunks: List[Any]) -> List[Dict[str, Any]]:
        """유사 사례 추출"""
        # TODO: grounding_chunks에서 티켓 관련 청크 추출
        return []

    def _extract_kb_references(self, grounding_chunks: List[Any]) -> List[Dict[str, Any]]:
        """KB 참조 추출"""
        # TODO: grounding_chunks에서 KB 문서 관련 청크 추출
        return []

    def _calculate_confidence(self, grounding_chunks: List[Any]) -> str:
        """신뢰도 계산"""
        if not grounding_chunks:
            return "low"
        elif len(grounding_chunks) >= 3:
            return "high"
        else:
            return "medium"

    def _generate_fallback_response(self, ticket_context: Dict[str, Any]) -> str:
        """폴백 응답 생성"""
        subject = ticket_context.get("subject", "문의")
        return (
            f"안녕하세요, '{subject}'에 대해 문의해 주셔서 감사합니다.\n\n"
            f"담당자가 확인 후 빠른 시일 내에 답변 드리겠습니다.\n\n"
            f"추가 정보가 필요하시면 말씀해 주세요."
        )


# =============================================================================
# Dependency Injection
# =============================================================================

def get_assist_service() -> AssistService:
    """AssistService 의존성 주입"""
    settings = get_settings()

    if not settings.gemini_api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Gemini API 키가 설정되지 않았습니다",
        )

    gemini_client = GeminiFileSearchClient(
        api_key=settings.gemini_api_key,
        primary_model=settings.gemini_primary_model,
        fallback_model=settings.gemini_fallback_model,
    )

    return AssistService(
        gemini_client=gemini_client,
        proposal_store=_proposal_store,
    )
