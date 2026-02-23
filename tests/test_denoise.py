"""Tests for NoCut Denoise Engine."""
import pytest
from app.services.denoise import denoise_conversations, DenoiseResult


class TestDenoiseConversations:
    """Test denoise_conversations function."""

    def test_empty_input(self):
        result = denoise_conversations([])
        assert result.conversation == []
        assert result.kept_original_indices == []

    def test_preserves_normal_messages(self):
        conversations = [
            {"body_text": "로그인이 안됩니다.", "incoming": True},
            {"body_text": "어떤 브라우저를 사용하시나요?", "incoming": False},
        ]
        result = denoise_conversations(conversations)
        assert len(result.conversation) == 2
        assert result.kept_original_indices == [0, 1]

    def test_removes_empty_messages(self):
        conversations = [
            {"body_text": "정상 메시지", "incoming": True},
            {"body_text": "", "incoming": True},
            {"body_text": "   ", "incoming": False},
        ]
        result = denoise_conversations(conversations)
        assert len(result.conversation) == 1
        assert result.conversation[0]["body_text"] == "정상 메시지"

    def test_removes_auto_reply(self):
        conversations = [
            {"body_text": "This is an automated message. Do not reply.", "incoming": False},
            {"body_text": "실제 상담원 응답입니다.", "incoming": False},
        ]
        result = denoise_conversations(conversations)
        assert len(result.conversation) == 1
        assert "실제 상담원" in result.conversation[0]["body_text"]

    def test_removes_duplicate_messages(self):
        conversations = [
            {"body_text": "같은 내용의 메시지", "incoming": True},
            {"body_text": "같은 내용의 메시지", "incoming": True},
            {"body_text": "다른 내용", "incoming": True},
        ]
        result = denoise_conversations(conversations)
        assert len(result.conversation) == 2

    def test_preserves_keep_signal_in_auto_reply(self):
        """Messages with keep signals should be preserved even if they match auto-reply."""
        conversations = [
            {"body_text": "자동응답: 해결 방법을 안내드립니다.", "incoming": False},
        ]
        result = denoise_conversations(conversations)
        assert len(result.conversation) == 1

    def test_strips_email_signatures(self):
        conversations = [
            {
                "body_text": "문제가 계속됩니다.\n--\nJohn Doe\nSenior Engineer\nCompany Inc.",
                "incoming": True,
            },
        ]
        result = denoise_conversations(conversations)
        assert len(result.conversation) == 1
        assert "John Doe" not in result.conversation[0]["body_text"]

    def test_strips_quoted_blocks(self):
        conversations = [
            {
                "body_text": "네 확인했습니다.\n> 이전 메시지 내용\n> 또 다른 인용",
                "incoming": True,
            },
        ]
        result = denoise_conversations(conversations)
        assert len(result.conversation) == 1
        text = result.conversation[0]["body_text"]
        assert "확인했습니다" in text
        assert "이전 메시지 내용" not in text

    def test_supports_nexus_format(self):
        """Test with nexus-ai style messages (text field)."""
        conversations = [
            {"text": "로그인 문제입니다.", "author_role": "customer", "channel": "email"},
            {"text": "캐시를 삭제해보세요.", "author_role": "agent", "channel": "note"},
        ]
        result = denoise_conversations(conversations)
        assert len(result.conversation) == 2

    def test_removes_freshdesk_footer(self):
        conversations = [
            {"body_text": "감사합니다.\nPowered by Freshdesk", "incoming": True},
        ]
        result = denoise_conversations(conversations)
        assert len(result.conversation) == 1
        assert "freshdesk" not in result.conversation[0]["body_text"].lower()

    def test_returns_correct_indices(self):
        conversations = [
            {"body_text": "첫 번째 메시지", "incoming": True},
            {"body_text": "", "incoming": True},  # removed (empty)
            {"body_text": "세 번째 메시지", "incoming": False},
            {"body_text": "This is an automated message", "incoming": False},  # removed
            {"body_text": "다섯 번째 메시지", "incoming": True},
        ]
        result = denoise_conversations(conversations)
        assert result.kept_original_indices == [0, 2, 4]
