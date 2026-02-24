

def test_channel_bff_routes_exist_and_chat_is_unique():
    from app.main import app
    from fastapi.routing import APIRoute

    seen = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if method == "HEAD":
                continue
            seen.append((route.path, method))

    # 신규 채널 BFF 경로 존재
    assert ("/api/fdk/v1/chat", "POST") in seen
    assert ("/api/web/v1/chat", "POST") in seen

    # 레거시 경로는 유지
    assert ("/api/chat", "POST") in seen
    assert ("/api/multitenant/chat", "POST") in seen

    # 핵심 충돌 경로는 중복이 없어야 함
    assert seen.count(("/api/chat", "POST")) == 1
    assert seen.count(("/api/chat/stream", "GET")) == 1

    # PR5에서 `/api/web/v1`로 노출되던 유틸 엔드포인트 호환 유지
    assert ("/api/web/v1/tenant/info", "GET") in seen
    assert ("/api/web/v1/health", "GET") in seen


def test_fdk_v1_chat_works(test_client, override_pipeline_client):
    from app.main import app as app_main
    assert test_client.app is app_main
    from fastapi.routing import APIRoute
    paths = []
    for r in test_client.app.routes:
        if isinstance(r, APIRoute):
            paths.append((r.path, sorted([m for m in r.methods if m != "HEAD"])))
    assert ("/api/fdk/v1/chat", ["POST"]) in paths

    session_id = override_pipeline_client.create_session()["sessionId"]
    res = test_client.post(
        "/api/fdk/v1/chat",
        json={
            "sessionId": session_id,
            "query": "hello",
            "sources": ["tickets"],
            "commonProduct": "제품A",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["text"] == "stub response"


def test_web_v1_chat_uses_multitenant_handler_when_available(test_client):
    from app.main import app
    from app.models.session import ChatResponse
    from app.services.multitenant_chat_handler import get_multitenant_chat_handler

    class StubMultitenantHandler:
        def __init__(self):
            self.calls = 0

        async def handle(self, request, tenant, *, history=None, additional_filters=None):
            self.calls += 1
            return ChatResponse.model_validate(
                {
                    "text": "ok",
                    "sources": request.sources or [],
                    "filters": [],
                    "knownContext": {"tenant_id": tenant.tenant_id, "platform": tenant.platform},
                }
            )

        async def stream_handle(self, request, tenant, *, history=None, additional_filters=None):
            yield {
                "event": "result",
                "data": {
                    "text": "ok",
                    "groundingChunks": [],
                },
            }

    stub = StubMultitenantHandler()
    app.dependency_overrides[get_multitenant_chat_handler] = lambda: stub
    try:
        res = test_client.post(
            "/api/web/v1/chat",
            json={
                "session_id": "s2",
                "query": "hi",
            },
            headers={
                "X-Tenant-ID": "t1",
                "X-Platform": "web",
                "X-API-Key": "any",
            },
        )
        assert res.status_code == 200
        assert res.json()["text"] == "ok"
        assert stub.calls == 1
    finally:
        app.dependency_overrides.pop(get_multitenant_chat_handler, None)


def test_legacy_chat_dispatches_to_multitenant_when_tenant_headers_present(test_client):
    from app.main import app
    from app.models.session import ChatResponse
    from app.services.multitenant_chat_handler import get_multitenant_chat_handler

    class StubMultitenantHandler:
        def __init__(self):
            self.calls = 0

        async def handle(self, request, tenant, *, history=None, additional_filters=None):
            self.calls += 1
            return ChatResponse.model_validate({"text": "mt"})

    stub = StubMultitenantHandler()
    app.dependency_overrides[get_multitenant_chat_handler] = lambda: stub
    try:
        res = test_client.post(
            "/api/chat",
            json={
                "session_id": "s3",
                "query": "hi",
            },
            headers={
                "X-Tenant-ID": "t1",
                "X-Platform": "web",
                "X-API-Key": "any",
            },
        )
        assert res.status_code == 200
        assert res.json()["text"] == "mt"
        assert stub.calls == 1
    finally:
        app.dependency_overrides.pop(get_multitenant_chat_handler, None)
