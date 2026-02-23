"""Tests for HITL Feedback API endpoints."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from fastapi.testclient import TestClient

HEADERS = {"X-Tenant-ID": "test-tenant-123"}


@pytest.fixture
def client():
    """Create test client."""
    from app.main import app
    return TestClient(app)


@pytest.fixture
def mock_feedback_repo():
    """Mock feedback repository to avoid DB calls."""
    repo = MagicMock()
    repo.submit_feedback = AsyncMock(return_value="event-uuid-123")
    repo.update_approved_response = AsyncMock(return_value="sample-uuid-456")
    repo.get_exportable_samples = AsyncMock(return_value=[])
    return repo


class TestFeedbackSubmit:
    """Test POST /api/assist/feedback/submit."""

    def test_submit_helpful(self, client, mock_feedback_repo):
        with patch(
            "app.api.routes.assist.get_feedback_repository",
            return_value=mock_feedback_repo,
        ):
            response = client.post(
                "/api/assist/feedback/submit",
                json={
                    "analysis_id": "analysis-uuid-789",
                    "event_type": "helpful",
                    "rating": 5,
                },
                headers=HEADERS,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["event_id"] == "event-uuid-123"

    def test_submit_not_helpful(self, client, mock_feedback_repo):
        with patch(
            "app.api.routes.assist.get_feedback_repository",
            return_value=mock_feedback_repo,
        ):
            response = client.post(
                "/api/assist/feedback/submit",
                json={
                    "analysis_id": "analysis-uuid-789",
                    "event_type": "not_helpful",
                    "feedback_text": "분석이 부정확합니다",
                },
                headers=HEADERS,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_submit_invalid_event_type(self, client):
        response = client.post(
            "/api/assist/feedback/submit",
            json={
                "analysis_id": "analysis-uuid-789",
                "event_type": "invalid_type",
            },
            headers=HEADERS,
        )
        assert response.status_code == 422  # Validation error


class TestFeedbackEdit:
    """Test POST /api/assist/feedback/edit."""

    def test_edit_response(self, client, mock_feedback_repo):
        with patch(
            "app.api.routes.assist.get_feedback_repository",
            return_value=mock_feedback_repo,
        ):
            response = client.post(
                "/api/assist/feedback/edit",
                json={
                    "analysis_id": "analysis-uuid-789",
                    "approved_response": {
                        "narrative": {"summary": "수정된 요약"},
                        "root_cause": "수정된 원인",
                        "confidence": 0.9,
                    },
                    "agent_id": "agent-123",
                },
                headers=HEADERS,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["sample_id"] == "sample-uuid-456"


class TestTrainingExport:
    """Test POST /api/assist/training/export."""

    def test_export_empty(self, client, mock_feedback_repo):
        with patch(
            "app.api.routes.assist.get_feedback_repository",
            return_value=mock_feedback_repo,
        ):
            response = client.post(
                "/api/assist/training/export",
                json={"limit": 10},
                headers=HEADERS,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["count"] == 0
        assert data["samples"] == []

    def test_export_with_samples(self, client, mock_feedback_repo):
        mock_feedback_repo.get_exportable_samples = AsyncMock(
            return_value=[
                {
                    "id": "sample-1",
                    "tenant_id": "test-tenant-123",
                    "ticket_id": "ticket-456",
                    "analysis_id": "analysis-789",
                    "original_response": {"summary": "AI 응답"},
                    "approved_response": {"summary": "수정된 응답"},
                    "rating": 4,
                    "prompt_version": "v1",
                    "model": "gemini-2.5-flash",
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ]
        )

        with patch(
            "app.api.routes.assist.get_feedback_repository",
            return_value=mock_feedback_repo,
        ):
            response = client.post(
                "/api/assist/training/export",
                json={"limit": 10, "mark_exported": True},
                headers=HEADERS,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["samples"][0]["ticket_id"] == "ticket-456"
