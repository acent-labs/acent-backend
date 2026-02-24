"""
Assist API 테스트

FDK Custom App Assist API 엔드포인트 테스트
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.assist import Proposal
from app.services.assist_service import get_assist_service, ProposalStore


# =============================================================================
# Fixtures
# =============================================================================

class MockAssistService:
    """테스트용 Mock AssistService"""

    def __init__(self):
        self.proposal_store = ProposalStore()
        self._created_proposals = {}

    async def analyze(self, tenant_id, request, freshdesk_context=None):
        """Mock 분석 - 동기 방식"""
        proposal_data = {
            "ticket_id": request.ticket_id,
            "draft_response": "테스트 응답입니다.",
            "field_updates": {"priority": {"old": 2, "new": 3}},
            "confidence": "high",
            "mode": "synthesis",
            "similar_cases": [],
            "kb_references": [],
            "analysis_time_ms": 150,
        }

        # 저장
        proposal = await self.proposal_store.create(tenant_id, proposal_data)
        self._created_proposals[proposal["id"]] = proposal

        return Proposal(**proposal)

    async def analyze_with_streaming(self, tenant_id, request, freshdesk_context=None):
        """Mock 스트리밍 분석 - 사용하지 않음"""
        # non-streaming으로 전환
        proposal = await self.analyze(tenant_id, request, freshdesk_context)
        yield {
            "type": "resolution_complete",
            "proposalId": proposal.id,
            "confidence": proposal.confidence,
            "mode": proposal.mode,
            "proposal": proposal.model_dump(),
        }

    async def approve(self, tenant_id, request):
        """Mock 승인"""
        proposal = await self.proposal_store.get(request.proposal_id)
        if not proposal:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"제안을 찾을 수 없습니다: {request.proposal_id}",
            )

        if request.action == "approve":
            await self.proposal_store.update(request.proposal_id, {"status": "approved"})
            return {
                "status": "approved",
                "field_updates": proposal.get("field_updates"),
                "final_response": request.final_response or proposal.get("draft_response"),
            }
        elif request.action == "reject":
            await self.proposal_store.update(request.proposal_id, {"status": "rejected"})
            return {
                "status": "rejected",
                "reason": request.rejection_reason,
            }

    async def refine(self, tenant_id, request):
        """Mock 수정"""
        proposal = await self.proposal_store.get(request.proposal_id)
        if not proposal:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"제안을 찾을 수 없습니다: {request.proposal_id}",
            )

        new_proposal = await self.proposal_store.create_version(
            request.proposal_id,
            {
                "draft_response": f"{proposal['draft_response']}\n\n[수정됨]",
                "field_updates": proposal.get("field_updates"),
                "confidence": proposal.get("confidence"),
                "mode": proposal.get("mode"),
            }
        )

        return {
            "proposal": Proposal(**new_proposal),
            "version": new_proposal["proposal_version"],
        }

    async def get_proposal(self, tenant_id, proposal_id):
        """Mock 조회"""
        proposal = await self.proposal_store.get(proposal_id)
        if proposal:
            return Proposal(**proposal)
        return None


@pytest.fixture
def mock_assist_service():
    """Mock AssistService 주입"""
    mock = MockAssistService()
    app.dependency_overrides[get_assist_service] = lambda: mock
    yield mock
    app.dependency_overrides.pop(get_assist_service, None)


@pytest.fixture
def assist_client(mock_assist_service) -> TestClient:
    """테스트 클라이언트"""
    with TestClient(app) as client:
        yield client


# =============================================================================
# Analyze Endpoint Tests
# =============================================================================

class TestAnalyzeEndpoint:
    """POST /api/assist/analyze 테스트"""

    def test_analyze_success(self, assist_client):
        """정상 분석 요청"""
        response = assist_client.post(
            "/api/assist/analyze",
            json={
                "ticket_id": "12345",
                "subject": "로그인 오류",
                "description": "로그인 시 500 에러가 발생합니다.",
                "priority": 2,
                "status": 2,
                "stream_progress": False,  # 스트리밍 비활성화
            },
            headers={
                "X-Tenant-ID": "test-tenant",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "proposal" in data
        # Pydantic alias로 인해 ticketId로 직렬화됨
        assert data["proposal"]["ticketId"] == "12345"
        assert data["proposal"]["confidence"] == "high"

    def test_analyze_missing_tenant_id(self, assist_client):
        """테넌트 ID 누락 시 에러"""
        response = assist_client.post(
            "/api/assist/analyze",
            json={
                "ticket_id": "12345",
            },
        )

        assert response.status_code == 422  # Validation error

    def test_analyze_missing_ticket_id(self, assist_client):
        """티켓 ID 누락 시 에러"""
        response = assist_client.post(
            "/api/assist/analyze",
            json={
                "subject": "테스트",
            },
            headers={
                "X-Tenant-ID": "test-tenant",
            },
        )

        assert response.status_code == 422

    def test_analyze_with_freshdesk_headers(self, assist_client):
        """Freshdesk 헤더 포함 요청"""
        response = assist_client.post(
            "/api/assist/analyze",
            json={
                "ticket_id": "12345",
                "subject": "테스트",
                "stream_progress": False,
            },
            headers={
                "X-Tenant-ID": "test-tenant",
                "X-Freshdesk-Domain": "company.freshdesk.com",
                "X-Freshdesk-API-Key": "test-api-key",
            },
        )

        assert response.status_code == 200


# =============================================================================
# Approve Endpoint Tests
# =============================================================================

class TestApproveEndpoint:
    """POST /api/assist/approve 테스트"""

    def test_approve_success(self, assist_client, mock_assist_service):
        """정상 승인 요청"""
        # 먼저 제안 생성
        analyze_response = assist_client.post(
            "/api/assist/analyze",
            json={"ticket_id": "12345", "stream_progress": False},
            headers={"X-Tenant-ID": "test-tenant"},
        )
        proposal_id = analyze_response.json()["proposal"]["id"]

        # 승인 요청
        response = assist_client.post(
            "/api/assist/approve",
            json={
                "ticket_id": "12345",
                "proposal_id": proposal_id,
                "action": "approve",
                "agent_email": "agent@test.com",
            },
            headers={"X-Tenant-ID": "test-tenant"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "approved"

    def test_reject_with_reason(self, assist_client, mock_assist_service):
        """거절 요청 (사유 포함)"""
        # 제안 생성
        analyze_response = assist_client.post(
            "/api/assist/analyze",
            json={"ticket_id": "12345", "stream_progress": False},
            headers={"X-Tenant-ID": "test-tenant"},
        )
        proposal_id = analyze_response.json()["proposal"]["id"]

        # 거절 요청
        response = assist_client.post(
            "/api/assist/approve",
            json={
                "ticket_id": "12345",
                "proposal_id": proposal_id,
                "action": "reject",
                "rejection_reason": "응답이 적절하지 않음",
            },
            headers={"X-Tenant-ID": "test-tenant"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "rejected"
        assert data["reason"] == "응답이 적절하지 않음"

    def test_approve_not_found(self, assist_client):
        """존재하지 않는 제안 승인 시도"""
        response = assist_client.post(
            "/api/assist/approve",
            json={
                "ticket_id": "12345",
                "proposal_id": "non-existent-id",
                "action": "approve",
                "agent_email": "test@test.com",
            },
            headers={"X-Tenant-ID": "test-tenant"},
        )

        assert response.status_code == 404


# =============================================================================
# Refine Endpoint Tests
# =============================================================================

class TestRefineEndpoint:
    """POST /api/assist/refine 테스트"""

    def test_refine_success(self, assist_client, mock_assist_service):
        """정상 수정 요청"""
        # 제안 생성
        analyze_response = assist_client.post(
            "/api/assist/analyze",
            json={"ticket_id": "12345", "stream_progress": False},
            headers={"X-Tenant-ID": "test-tenant"},
        )
        proposal_id = analyze_response.json()["proposal"]["id"]

        # 수정 요청
        response = assist_client.post(
            "/api/assist/refine",
            json={
                "ticket_id": "12345",
                "proposal_id": proposal_id,
                "refinement_request": "좀 더 친근한 톤으로 수정해주세요",
            },
            headers={"X-Tenant-ID": "test-tenant"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "proposal" in data
        assert data["version"] == 2  # 새 버전


# =============================================================================
# Status Endpoint Tests
# =============================================================================

class TestStatusEndpoint:
    """GET /api/assist/status/{proposal_id} 테스트"""

    def test_get_status_success(self, assist_client, mock_assist_service):
        """제안 상태 조회"""
        # 제안 생성
        analyze_response = assist_client.post(
            "/api/assist/analyze",
            json={"ticket_id": "12345", "stream_progress": False},
            headers={"X-Tenant-ID": "test-tenant"},
        )
        proposal_id = analyze_response.json()["proposal"]["id"]

        # 상태 조회
        response = assist_client.get(
            f"/api/assist/status/{proposal_id}",
            headers={"X-Tenant-ID": "test-tenant"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == proposal_id

    def test_get_status_not_found(self, assist_client):
        """존재하지 않는 제안 조회"""
        response = assist_client.get(
            "/api/assist/status/non-existent-id",
            headers={"X-Tenant-ID": "test-tenant"},
        )

        assert response.status_code == 404
