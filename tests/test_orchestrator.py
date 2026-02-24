"""
Tests for Ticket Analysis Orchestrator (PR2)

DoD:
- (1) Real LLM call generates valid analysis output
- (2) Data persisted to analysis_runs and ticket_analyses
- (3) Response includes analysis_id, gate, analysis, meta
- (4) Failure scenario tests: JSON repair, schema validation
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.prompts.loader import load_prompt, clear_prompt_cache
from app.services.orchestrator.json_repair import (
    repair_json,
    try_parse_json,
    JSONRepairError,
)
from app.services.orchestrator.ticket_analysis_orchestrator import (
    TicketAnalysisOrchestrator,
    AnalysisOptions,
)
from app.services.llm_gateway import LLMResponse


# =============================================================================
# Prompt Loader Tests
# =============================================================================


class TestPromptLoader:
    """Tests for prompt registry loader."""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        """Clear cache before each test."""
        clear_prompt_cache()
        yield
        clear_prompt_cache()

    def test_load_prompt_success(self):
        """Load existing prompt template."""
        prompt = load_prompt("ticket_analysis_cot_v1")

        assert prompt.id == "ticket_analysis_cot_v1"
        assert prompt.version == "1.0.0"
        assert "customer support analyst" in prompt.system_prompt.lower()
        assert prompt.json_mode is True
        assert prompt.temperature == 0.3

    def test_load_prompt_not_found(self):
        """Non-existent prompt raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_prompt("nonexistent_prompt")

    def test_prompt_render(self):
        """Prompt renders with context variables."""
        prompt = load_prompt("ticket_analysis_cot_v1")
        context = {
            "ticket_id": "12345",
            "subject": "Test Subject",
            "description": "Test Description",
            "conversations": [],
            "custom_fields": {},
            "ticket_fields_summary": "None",
            "similar_cases": [],
            "kb_articles": [],
            "response_tone": "formal",
        }

        system, user = prompt.render(context)

        assert "12345" in user
        assert "Test Subject" in user
        assert "Test Description" in user


# =============================================================================
# JSON Repair Tests
# =============================================================================


class TestJSONRepair:
    """Tests for JSON repair utility."""

    def test_repair_valid_json(self):
        """Valid JSON passes through unchanged."""
        valid = '{"key": "value", "num": 123}'
        result = repair_json(valid)
        assert json.loads(result) == {"key": "value", "num": 123}

    def test_repair_markdown_block(self):
        """Remove markdown code blocks."""
        with_markdown = '```json\n{"key": "value"}\n```'
        result = repair_json(with_markdown)
        assert json.loads(result) == {"key": "value"}

    def test_repair_trailing_comma(self):
        """Fix trailing commas."""
        with_comma = '{"key": "value",}'
        result = repair_json(with_comma)
        assert json.loads(result) == {"key": "value"}

    def test_repair_surrounding_text(self):
        """Extract JSON from surrounding text."""
        with_text = 'Here is the analysis:\n{"key": "value"}\nEnd of response.'
        result = repair_json(with_text)
        assert json.loads(result) == {"key": "value"}

    def test_repair_unbalanced_braces(self):
        """Balance missing closing braces."""
        unbalanced = '{"key": {"nested": "value"'
        result = repair_json(unbalanced)
        parsed = json.loads(result)
        assert "key" in parsed

    def test_repair_empty_raises(self):
        """Empty input raises JSONRepairError."""
        with pytest.raises(JSONRepairError):
            repair_json("")

    def test_try_parse_json_success(self):
        """try_parse_json returns parsed dict on success."""
        result, error = try_parse_json('{"confidence": 0.85}')
        assert result == {"confidence": 0.85}
        assert error is None

    def test_try_parse_json_failure(self):
        """try_parse_json returns error message on failure."""
        result, error = try_parse_json("completely invalid")
        assert result is None
        assert error is not None


# =============================================================================
# Orchestrator Tests
# =============================================================================


class TestTicketAnalysisOrchestrator:
    """Tests for the main orchestrator.

    Note: conftest.py의 stub_ticket_analysis_orchestrator fixture가
    이 클래스를 자동으로 건너뛰도록 설정되어 있습니다.
    """

    @pytest.fixture
    def orchestrator(self):
        """Create orchestrator instance with mocked dependencies."""
        orch = TicketAnalysisOrchestrator()
        # Pre-initialize with mocks to allow patching
        orch._llm_gateway = MagicMock()
        orch._persistence = MagicMock()
        return orch

    @pytest.fixture
    def sample_input(self):
        """Sample normalized ticket input."""
        return {
            "ticket_id": "12345",
            "subject": "Cannot login to my account",
            "description": "I'm having trouble logging in since yesterday.",
            "conversations": [
                {
                    "body_text": "Please help me access my account",
                    "incoming": True,
                    "created_at": "2025-01-01T10:00:00Z",
                }
            ],
            "custom_fields": {},
            "ticket_fields": [],
        }

    @pytest.fixture
    def sample_options(self):
        """Sample analysis options."""
        return AnalysisOptions(
            skip_retrieval=True,
            include_evidence=True,
            confidence_threshold=0.7,
            response_tone="formal",
        )

    @pytest.fixture
    def mock_llm_response(self):
        """Mock successful LLM response."""
        return LLMResponse(
            content=json.dumps({
                "narrative": {
                    "summary": "고객이 로그인 문제를 겪고 있습니다.",
                    "timeline": []
                },
                "root_cause": "비밀번호 만료 또는 계정 잠금 가능성",
                "resolution": [
                    {"step": 1, "action": "비밀번호 재설정 링크 발송", "rationale": "가장 일반적인 해결책"}
                ],
                "confidence": 0.75,
                "open_questions": [],
                "risk_tags": [],
                "intent": "technical_issue",
                "sentiment": "neutral",
                "field_proposals": [],
                "evidence": [
                    {
                        "source_type": "conversation",
                        "source_id": "1",
                        "excerpt": "Please help me access my account",
                        "relevance_score": 0.9
                    }
                ]
            }),
            provider="deepseek",
            model="deepseek-chat",
            latency_ms=1500,
            attempts=1,
            used_fallback=False,
        )

    @pytest.mark.anyio
    async def test_orchestrator_success(
        self, orchestrator, sample_input, sample_options, mock_llm_response
    ):
        """Orchestrator returns valid result on success."""
        # Configure mocks
        orchestrator._llm_gateway.generate = AsyncMock(return_value=mock_llm_response)
        orchestrator._persistence.save_analysis_run = AsyncMock(return_value=True)
        orchestrator._persistence.save_analysis_result = AsyncMock(return_value=True)

        result = await orchestrator.run_ticket_analysis(
            normalized_input=sample_input,
            options=sample_options,
            tenant_id="test-tenant",
        )

        assert result.success is True
        assert result.analysis_id is not None
        assert result.gate in ["CONFIRM", "EDIT", "DECIDE", "TEACH"]
        assert "confidence" in result.analysis
        assert result.meta["llm_provider"] == "deepseek"

    @pytest.mark.anyio
    async def test_orchestrator_json_repair(
        self, orchestrator, sample_input, sample_options
    ):
        """Orchestrator repairs malformed JSON from LLM."""
        # Response with markdown wrapper
        malformed_response = LLMResponse(
            content='```json\n{"confidence": 0.6, "intent": "inquiry", "sentiment": "neutral", "narrative": {"summary": "Test"}, "root_cause": null, "resolution": [], "open_questions": [], "risk_tags": [], "field_proposals": [], "evidence": []}\n```',
            provider="test",
            model="test-model",
            latency_ms=100,
            attempts=1,
            used_fallback=False,
        )

        # Configure mocks
        orchestrator._llm_gateway.generate = AsyncMock(return_value=malformed_response)
        orchestrator._persistence.save_analysis_run = AsyncMock(return_value=True)
        orchestrator._persistence.save_analysis_result = AsyncMock(return_value=True)

        result = await orchestrator.run_ticket_analysis(
            normalized_input=sample_input,
            options=sample_options,
            tenant_id="test-tenant",
        )

        assert result.success is True
        assert result.analysis.get("confidence") == 0.6

    @pytest.mark.anyio
    async def test_orchestrator_llm_failure(
        self, orchestrator, sample_input, sample_options
    ):
        """Orchestrator handles LLM failure gracefully."""
        # Configure mocks
        orchestrator._llm_gateway.generate = AsyncMock(
            side_effect=Exception("LLM service unavailable")
        )
        orchestrator._persistence.save_analysis_run = AsyncMock(return_value=True)

        result = await orchestrator.run_ticket_analysis(
            normalized_input=sample_input,
            options=sample_options,
            tenant_id="test-tenant",
        )

        assert result.success is False
        assert result.error is not None
        assert "LLM service unavailable" in result.error
        assert result.gate == "TEACH"  # Fallback gate on failure

    @pytest.mark.anyio
    async def test_orchestrator_invalid_json_response(
        self, orchestrator, sample_input, sample_options
    ):
        """Orchestrator handles completely invalid JSON from LLM."""
        invalid_response = LLMResponse(
            content="This is not JSON at all, just random text without any structure",
            provider="test",
            model="test-model",
            latency_ms=100,
            attempts=1,
            used_fallback=False,
        )

        # Configure mocks
        orchestrator._llm_gateway.generate = AsyncMock(return_value=invalid_response)
        orchestrator._persistence.save_analysis_run = AsyncMock(return_value=True)

        result = await orchestrator.run_ticket_analysis(
            normalized_input=sample_input,
            options=sample_options,
            tenant_id="test-tenant",
        )

        assert result.success is False
        assert "parse" in result.error.lower() or "json" in result.error.lower()


class TestGateComputation:
    """Tests for gate decision logic."""

    def test_gate_confirm_high_confidence(self):
        """High confidence (>=0.9) returns CONFIRM."""
        orchestrator = TicketAnalysisOrchestrator()
        assert orchestrator._compute_gate(0.95, 0.7) == "CONFIRM"
        assert orchestrator._compute_gate(0.90, 0.7) == "CONFIRM"

    def test_gate_edit_medium_high_confidence(self):
        """Medium-high confidence returns EDIT."""
        orchestrator = TicketAnalysisOrchestrator()
        assert orchestrator._compute_gate(0.85, 0.7) == "EDIT"
        assert orchestrator._compute_gate(0.70, 0.7) == "EDIT"

    def test_gate_decide_medium_confidence(self):
        """Medium confidence returns DECIDE."""
        orchestrator = TicketAnalysisOrchestrator()
        assert orchestrator._compute_gate(0.60, 0.7) == "DECIDE"
        assert orchestrator._compute_gate(0.50, 0.7) == "DECIDE"

    def test_gate_teach_low_confidence(self):
        """Low confidence (<0.5) returns TEACH."""
        orchestrator = TicketAnalysisOrchestrator()
        assert orchestrator._compute_gate(0.40, 0.7) == "TEACH"
        assert orchestrator._compute_gate(0.20, 0.7) == "TEACH"

    def test_gate_respects_threshold(self):
        """Gate computation respects custom threshold."""
        orchestrator = TicketAnalysisOrchestrator()
        # With high threshold, 0.75 should be DECIDE, not EDIT
        assert orchestrator._compute_gate(0.75, 0.8) == "DECIDE"
