
from app.services.query_filter_analyzer import QueryFilterAnalyzer


class FakeMetadataService:
    async def resolve_priority_label(self, label: str):  # pragma: no cover - simple stub
        return {"urgent": 4, "high": 3}.get(label.lower())

    async def resolve_status_label(self, label: str):  # pragma: no cover - simple stub
        return {"open": 2, "closed": 5}.get(label.lower())

    async def list_priority_labels(self):  # pragma: no cover - simple stub
        return ["Urgent", "High", "Medium", "Low"]

    async def list_status_labels(self):  # pragma: no cover - simple stub
        return ["Open", "Pending", "Resolved", "Closed"]


def test_query_filter_analyzer_fallback(monkeypatch):
    analyzer = QueryFilterAnalyzer(fallback_months=6, metadata_service=FakeMetadataService())
    analyzer.llm_client = None
    result = analyzer.analyze("상담 티켓 상태 알려줘")

    assert result.success is True
    assert result.filters[0].key == "createdAt"
    assert result.clarification_needed is True


def test_query_filter_analyzer_normalizes_with_metadata(monkeypatch):
    analyzer = QueryFilterAnalyzer(metadata_service=FakeMetadataService())

    class FakeModels:
        @staticmethod
        def generate_content(*args, **kwargs):
            return type("Resp", (), {"text": '{"filters": [{"field": "priority", "value": "Urgent"}, {"field": "status", "value": "open"}]}'})()

    analyzer.llm_client = type("Stub", (), {"client": type("C", (), {"models": FakeModels})(), "models": ["stub"]})()
    result = analyzer.analyze("긴급 티켓")
    assert result.filters[0].value == "4"
    assert result.filters[1].value == "2"
    assert result.clarification_needed is False


def test_query_filter_analyzer_requires_clarification(monkeypatch):
    class RejectingMetadata(FakeMetadataService):
        async def resolve_priority_label(self, label: str):
            return None

    analyzer = QueryFilterAnalyzer(metadata_service=RejectingMetadata())

    class FakeModels:
        @staticmethod
        def generate_content(*args, **kwargs):
            return type("Resp", (), {"text": '{"filters": [{"field": "priority", "value": "Weird"}]}'})()

    analyzer.llm_client = type("Stub", (), {"client": type("C", (), {"models": FakeModels})(), "models": ["stub"]})()
    result = analyzer.analyze("이상한 우선순위")
    assert result.clarification_needed is True
    assert result.clarification and "옵션" in result.clarification.message


def test_query_filter_analyzer_applies_follow_up_choice():
    analyzer = QueryFilterAnalyzer(metadata_service=FakeMetadataService())
    analyzer.llm_client = None  # force fallback path
    clarification_state = {
        "clarification": {
            "field": "priority",
            "reason": "INVALID_PRIORITY",
        }
    }
    result = analyzer.analyze(
        "최근 티켓",
        clarification_option="Urgent",
        clarification_state=clarification_state,
    )
    assert any(filter_.key == "priority" and filter_.value == "4" for filter_ in result.filters)
    assert result.clarification_needed is False
