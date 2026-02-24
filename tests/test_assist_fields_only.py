import json



def _parse_sse_events(lines, *, stop_types=("complete", "error"), max_events=200):
    events = []
    for raw in lines:
        if raw is None:
            continue
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", errors="replace")
        else:
            line = str(raw)
        line = line.strip()
        if not line:
            continue
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        events.append(json.loads(payload))
        if events[-1].get("type") in stop_types:
            break
        if len(events) >= max_events:
            break
    return events


def test_analyze_fields_only_skips_conversation_enrichment(test_client, monkeypatch):
    from app.api.routes import assist as assist_module

    async def _boom(*_args, **_kwargs):
        raise AssertionError("conversation enrichment should be skipped in fieldsOnly mode")

    monkeypatch.setattr(assist_module, "_enrich_ticket_context_with_conversations", _boom, raising=True)

    res = test_client.post(
        "/api/assist/analyze",
        json={
            "ticket_id": "12345",
            "subject": "s",
            "description": "d",
            "fields_only": True,
            "stream_progress": False,
        },
        headers={"X-Tenant-ID": "test-tenant"},
    )
    assert res.status_code == 200


def test_field_proposals_endpoint_forces_fields_only(test_client):
    res = test_client.post(
        "/api/assist/field-proposals",
        json={
            "ticket_id": "12345",
            "subject": "s",
            "description": "d",
            # Even if caller forgets, endpoint should force fieldsOnly internally.
            "fields_only": False,
        },
        headers={"X-Tenant-ID": "test-tenant"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "fieldProposals" in data
    assert "analysis" in data
    assert "timingMs" in data


def test_field_proposals_stream_has_stable_sse_shape_and_no_searching(test_client, monkeypatch):
    from app.api.routes import assist as assist_module

    async def _boom_retrieve(*_args, **_kwargs):
        raise AssertionError("retrieve_context should be skipped in fieldsOnly stream")

    async def _boom_synthesize(*_args, **_kwargs):
        raise AssertionError("synthesize_results should be skipped in fieldsOnly stream")

    monkeypatch.setattr(assist_module, "retrieve_context", _boom_retrieve, raising=True)
    monkeypatch.setattr(assist_module, "synthesize_results", _boom_synthesize, raising=True)

    with test_client.stream(
        "POST",
        "/api/assist/field-proposals/stream",
        json={
            "ticket_id": "12345",
            "subject": "s",
            "description": "d",
        },
        headers={"X-Tenant-ID": "test-tenant"},
    ) as r:
        assert r.status_code == 200
        assert "text/event-stream" in (r.headers.get("content-type") or "")

        events = _parse_sse_events(r.iter_lines())
        types = [e.get("type") for e in events]
        assert "complete" in types
        assert "searching" not in types

        complete = next(e for e in events if e.get("type") == "complete")
        assert isinstance(complete.get("data"), dict)
        proposal = (complete["data"].get("proposal") or {})
        assert proposal.get("mode") == "fields_only"


def test_analyze_stream_fields_only_has_stable_sse_shape_and_no_searching(test_client, monkeypatch):
    from app.api.routes import assist as assist_module

    async def _boom_retrieve(*_args, **_kwargs):
        raise AssertionError("retrieve_context should be skipped in fieldsOnly analyze/stream")

    async def _boom_synthesize(*_args, **_kwargs):
        raise AssertionError("synthesize_results should be skipped in fieldsOnly analyze/stream")

    monkeypatch.setattr(assist_module, "retrieve_context", _boom_retrieve, raising=True)
    monkeypatch.setattr(assist_module, "synthesize_results", _boom_synthesize, raising=True)

    with test_client.stream(
        "POST",
        "/api/assist/analyze/stream",
        json={
            "ticket_id": "12345",
            "subject": "s",
            "description": "d",
            "fields_only": True,
        },
        headers={"X-Tenant-ID": "test-tenant"},
    ) as r:
        assert r.status_code == 200
        assert "text/event-stream" in (r.headers.get("content-type") or "")

        events = _parse_sse_events(r.iter_lines())
        types = [e.get("type") for e in events]
        assert "complete" in types
        assert "searching" not in types

        complete = next(e for e in events if e.get("type") == "complete")
        assert isinstance(complete.get("data"), dict)
        proposal = (complete["data"].get("proposal") or {})
        assert proposal.get("mode") == "fields_only"

