---
name: fastapi-guidelines
description: FastAPI/Python backend development patterns for agent-platform. Use when creating routes, services, repositories, models, or working with Pydantic, async endpoints, SSE streaming, Supabase, Sentry error tracking, or dependency injection. Covers layered architecture (routes → services → repositories), Settings pattern, error handling, and async best practices.
---

# FastAPI Backend Development Guidelines

## Purpose

Ensure consistency across `agent-platform` FastAPI backend following established patterns in `app/`.

## When to Use This Skill

- Creating or modifying routes/endpoints (`app/api/routes/`)
- Building services (`app/services/`) or repositories (`app/repositories/`)
- Defining Pydantic models (`app/models/`)
- Adding middleware (`app/middleware/`)
- Implementing SSE streaming
- Adding Sentry error tracking

---

## Project Structure

```
app/
├── api/routes/      # FastAPI APIRouter 엔드포인트
├── core/config.py   # Settings (pydantic_settings)
├── middleware/      # 인증, 로깅, 요청 ID
├── models/          # Pydantic 요청/응답 모델
├── repositories/    # DB 접근 계층 (Supabase)
├── services/        # 비즈니스 로직
├── agents/          # LangGraph 에이전트
├── prompts/         # 프롬프트 로더
└── main.py          # FastAPI app 초기화
```

---

## 핵심 규칙 (7가지)

### 1. 설정은 반드시 Settings 사용 — 절대 os.getenv 직접 사용 금지

```python
# ❌ 절대 금지
import os
api_key = os.getenv("API_KEY")

# ✅ 항상 이 방법 사용
from app.core.config import get_settings
settings = get_settings()
api_key = settings.api_key
```

### 2. 라우트는 라우팅만, 로직은 서비스로

```python
# ❌ 라우트에 비즈니스 로직 금지
@router.post("/analyze")
async def analyze(req: AnalyzeRequest):
    # 200줄의 비즈니스 로직...

# ✅ 서비스에 위임
@router.post("/analyze")
async def analyze(
    req: AnalyzeRequest,
    service: AssistService = Depends(get_assist_service),
):
    return await service.analyze(req)
```

### 3. 모든 모델은 Pydantic BaseModel

```python
from pydantic import BaseModel, Field
from typing import Optional

class AnalyzeRequest(BaseModel):
    ticket_id: int
    domain: str
    conversation: list[dict]
    options: Optional[dict] = None
```

### 4. 의존성 주입 패턴 (Depends)

```python
# services/my_service.py
class MyService:
    def __init__(self, settings: Settings):
        self.settings = settings

def get_my_service() -> MyService:
    return MyService(settings=get_settings())

# routes/my_route.py
@router.get("/data")
async def get_data(service: MyService = Depends(get_my_service)):
    return await service.fetch()
```

### 5. 에러는 반드시 Sentry에 캡처

```python
import sentry_sdk
import logging

logger = logging.getLogger(__name__)

try:
    result = await some_operation()
except Exception as e:
    sentry_sdk.capture_exception(e)
    logger.error("Operation failed: %s", e)
    raise HTTPException(status_code=500, detail="Internal error")
```

### 6. SSE 스트리밍 패턴

```python
from fastapi.responses import StreamingResponse
from typing import AsyncGenerator

async def event_stream(ticket_id: int) -> AsyncGenerator[str, None]:
    yield f"data: {json.dumps({'event': 'started'})}\n\n"

    async for chunk in some_async_generator():
        yield f"data: {json.dumps({'event': 'progress', 'data': chunk})}\n\n"

    yield f"data: {json.dumps({'event': 'complete'})}\n\n"

@router.get("/stream")
async def stream_endpoint(ticket_id: int):
    return StreamingResponse(
        event_stream(ticket_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

### 7. Repository 패턴 (Supabase)

```python
# repositories/my_repository.py
from app.core.config import get_settings
from supabase import create_client

class MyRepository:
    def __init__(self):
        settings = get_settings()
        self.client = create_client(settings.supabase_url, settings.supabase_key)

    async def find_by_id(self, record_id: str) -> dict | None:
        response = self.client.table("my_table").select("*").eq("id", record_id).execute()
        return response.data[0] if response.data else None

def get_my_repository() -> MyRepository:
    return MyRepository()
```

---

## Settings 패턴

```python
# app/core/config.py 에 추가
from functools import lru_cache
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    my_new_setting: str = "default_value"
    my_optional_setting: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

---

## 라우트 파일 템플릿

```python
"""
[기능명] API 라우트
[간단한 설명]
"""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.models.my_model import MyRequest, MyResponse
from app.services.my_service import MyService, get_my_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/endpoint", response_model=MyResponse)
async def my_endpoint(
    req: MyRequest,
    service: MyService = Depends(get_my_service),
) -> MyResponse:
    try:
        return await service.process(req)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        import sentry_sdk
        sentry_sdk.capture_exception(e)
        logger.error("Endpoint failed: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal error")
```

---

## 새 기능 체크리스트

- [ ] `app/models/` — Pydantic 요청/응답 모델 정의
- [ ] `app/services/` — 비즈니스 로직 + `get_xxx_service()` 팩토리
- [ ] `app/repositories/` — DB 접근 (필요시)
- [ ] `app/api/routes/` — 라우트 정의 (로직 없이)
- [ ] `app/api/router.py` — 새 라우터 등록
- [ ] `.env` / `Settings` — 새 환경 변수 추가
- [ ] `tests/test_xxx.py` — 테스트 작성

---

## 자주 하는 실수

| 잘못된 패턴 | 올바른 패턴 |
|-------------|------------|
| `os.getenv("KEY")` | `get_settings().key` |
| 라우트에 비즈니스 로직 | 서비스 레이어로 분리 |
| `except: pass` | Sentry 캡처 + 적절한 HTTPException |
| 동기 함수로 I/O 작업 | `async def` + `await` |
| `print()` 디버깅 | `logger.debug/info/error()` |

---

## 관련 스킬

- **error-tracking** — Sentry 통합 패턴 (Python)
- **skill-developer** — 스킬 시스템 관리
