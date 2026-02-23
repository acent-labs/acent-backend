"""Tests for Guardrails Module."""
import pytest
from app.services.guardrails import (
    apply_guardrails,
    contains_forbidden_phrases,
    fix_evidence_fields,
    normalize_evidence_items,
    find_forbidden_phrases,
    check_language_mismatch,
)


class TestEvidenceValidation:
    """Test evidence field validation."""

    def test_valid_integer_evidence(self):
        items = [{"title": "원인 A", "evidence": [0, 1, 2]}]
        result, violations = normalize_evidence_items(items, "root_cause")
        assert result[0]["evidence"] == [0, 1, 2]
        assert violations == []

    def test_removes_non_integer_evidence(self):
        items = [{"title": "원인 A", "evidence": [0, "msg_1", 2, None]}]
        result, violations = normalize_evidence_items(items, "root_cause")
        assert result[0]["evidence"] == [0, 2]
        assert len(violations) == 1
        assert "non-integer" in violations[0]

    def test_empty_evidence_list(self):
        items = [{"title": "원인 A", "evidence": []}]
        result, violations = normalize_evidence_items(items, "root_cause")
        assert result[0]["evidence"] == []
        assert violations == []

    def test_non_list_evidence_reset(self):
        items = [{"title": "원인 A", "evidence": "invalid"}]
        result, violations = normalize_evidence_items(items, "root_cause")
        assert result[0]["evidence"] == []
        assert len(violations) == 1

    def test_fix_evidence_fields_both(self):
        data = {
            "root_causes": [
                {"title": "A", "evidence": [0, "x"]},
            ],
            "recommended_actions": [
                {"title": "B", "evidence": [1, 2.5]},
            ],
        }
        result, violations = fix_evidence_fields(data)
        assert result["root_causes"][0]["evidence"] == [0]
        assert result["recommended_actions"][0]["evidence"] == [1]
        assert len(violations) == 2


class TestForbiddenPhrases:
    """Test forbidden phrase detection."""

    def test_detects_forbidden_phrase(self):
        assert contains_forbidden_phrases("이 문제의 원인입니다.") is True

    def test_detects_unconditional(self):
        assert contains_forbidden_phrases("무조건 이렇게 해야 합니다.") is True

    def test_no_forbidden_phrases(self):
        assert contains_forbidden_phrases("~로 추정됩니다.") is False

    def test_find_multiple_phrases(self):
        found = find_forbidden_phrases("무조건 이것이 원인입니다. 확실히 맞습니다.")
        assert "무조건" in found
        assert "원인입니다" in found
        assert "확실히" in found


class TestLanguageMismatch:
    """Test language mismatch detection."""

    def test_no_mismatch(self):
        data = {"detected_language": "ko-KR", "response_language": "ko-KR"}
        violations = check_language_mismatch(data)
        assert violations == []

    def test_mismatch_detected(self):
        data = {"detected_language": "ko-KR", "response_language": "en-US"}
        violations = check_language_mismatch(data)
        assert len(violations) == 1
        assert "mismatch" in violations[0].lower()

    def test_missing_fields_ok(self):
        data = {"detected_language": "ko-KR"}
        violations = check_language_mismatch(data)
        assert violations == []


class TestApplyGuardrails:
    """Test unified guardrails application."""

    def test_clean_analysis_passes(self):
        analysis = {
            "narrative": {"summary": "로그인 오류 추정"},
            "confidence": 0.7,
            "root_cause": "세션 만료로 추정됩니다",
            "detected_language": "ko-KR",
            "response_language": "ko-KR",
        }
        result, violations = apply_guardrails(analysis)
        assert result == analysis
        assert violations == []

    def test_fixes_bad_evidence(self):
        analysis = {
            "root_causes": [
                {"title": "원인", "evidence": [0, "bad", 2]},
            ],
            "recommended_actions": [],
        }
        result, violations = apply_guardrails(analysis)
        assert result["root_causes"][0]["evidence"] == [0, 2]
        assert any("non-integer" in v for v in violations)

    def test_detects_forbidden_phrases(self):
        analysis = {
            "root_cause": "이것이 확실히 원인입니다",
        }
        result, violations = apply_guardrails(analysis)
        assert any("Forbidden" in v for v in violations)
