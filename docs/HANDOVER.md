# AI Contact Center OS — 핸드오버 문서

> **최종 업데이트**: 2026-03-05
> **브랜치**: agent-platform `main` / project-a `feat/analyze`

---

## 1. 아키텍처 개요

```
┌─────────────────────────────┐
│  project-a (FDK 프론트엔드)  │
│  Freshdesk ticket_top_nav   │
│  HTML/CSS/JS                │
└──────────┬──────────────────┘
           │  POST /api/tickets/{id}/analyze/stream (Path B)
           │  SSE events: progress → field* → complete
           ▼
┌─────────────────────────────┐
│  agent-platform (백엔드)     │
│  FastAPI + Supabase         │
│  Python 3.9                 │
└──────────┬──────────────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
  LLM Gateway   Supabase
  (Gemini 등)   (분석 결과 저장)
```

---

## 2. 분석 경로 (Path A / Path B)

### Path B — 현재 주 경로 (Orchestrator + YAML 프롬프트)

| 항목 | 값 |
|------|----|
| 엔드포인트 | `POST /api/tickets/{ticket_id}/analyze/stream` |
| 라우트 파일 | `app/api/routes/tickets.py` |
| Orchestrator | `app/services/orchestrator/ticket_analysis_orchestrator.py` |
| 프롬프트 | `app/prompts/registry/ticket_analysis_cot.yaml` |
| 입력 스키마 | `app/schemas/ticket_normalized.json` |
| 출력 스키마 | `app/schemas/ticket_analysis.json` |
| 특징 | confidence + gate 판정, summary_sections, narrative, denoise, guardrails |

**프론트엔드 호출 흐름**:
```
app.js handleAnalyzeTicket()
  → analysis-ui.js runAnalysis()
    → stream-utils.js fetchAnalysisStream()
      → POST /api/tickets/{id}/analyze/stream
```

**SSE 이벤트 순서**:
```
{"type": "progress", "step": "analyzing", "analysis_id": "..."}
{"type": "field", "name": "narrative", "data": {...}}
{"type": "field", "name": "summary_sections", "data": [...]}
{"type": "field", "name": "confidence", "data": 0.85}
{"type": "field", "name": "root_cause", "data": "..."}
  ... (resolution, intent, sentiment, risk_tags, field_proposals, ...)
{"type": "complete", "analysis_id": "...", "gate": "EDIT", "meta": {...}}
```

### Path A — 레거시 (인라인 프롬프트, 유지만 함)

| 항목 | 값 |
|------|----|
| 엔드포인트 | `POST /api/assist/analyze/stream` |
| 라우트 파일 | `app/api/routes/assist.py` |
| 어댑터 | `app/services/llm_adapter.py` |
| 특징 | summary_sections는 있으나 confidence/gate 없음, LangGraph 기반 |

> Path A 코드는 삭제하지 않고 유지. 프론트에서 Path B로 전환 완료 후 별도 정리.

---

## 3. Gate 판정 시스템

Orchestrator가 LLM 응답의 `confidence` 값을 기반으로 gate를 결정:

| Gate | 조건 | UI 동작 |
|------|------|---------|
| `CONFIRM` | confidence ≥ 0.9 | 자동 적용 가능 |
| `EDIT` | confidence ≥ 0.7 | 경미한 리뷰 후 적용 |
| `DECIDE` | confidence ≥ 0.5 | 상담원 판단 필요 |
| `TEACH` | confidence < 0.5 | 학습 피드백 필요 |

---

## 4. 백엔드 핵심 파일 구조

```
app/
├── api/routes/
│   ├── tickets.py           # Path B 엔드포인트 (analyze, analyze/stream, analyses)
│   ├── assist.py            # Path A 엔드포인트 + HITL feedback
│   ├── channel_fdk.py       # FDK BFF (/fdk/v1/chat)
│   ├── channel_web.py       # Web BFF (/web/v1/chat)
│   ├── chat.py              # 레거시 채팅
│   ├── multitenant.py       # 멀티테넌트 채팅
│   ├── admin.py             # 관리자 API
│   ├── sync.py              # 데이터 동기화
│   ├── onboarding.py        # 온보딩
│   ├── curriculum.py        # 제품 교육
│   └── health.py            # 헬스체크
├── services/
│   ├── orchestrator/
│   │   ├── ticket_analysis_orchestrator.py  # 분석 파이프라인 코어
│   │   ├── json_repair.py                   # LLM JSON 파싱/복구
│   │   └── persistence.py                   # Supabase 저장
│   ├── llm_gateway.py        # LLM 프로바이더 추상화 (Gemini/OpenAI/DeepSeek)
│   ├── llm_adapter.py        # Path A용 LLM 어댑터
│   ├── denoise.py            # 대화 노이즈 제거 (NoCut)
│   ├── guardrails.py         # LLM 출력 가드레일
│   └── feedback_repository.py # HITL 피드백 DB
├── prompts/registry/
│   └── ticket_analysis_cot.yaml  # CoT 분석 프롬프트
├── schemas/
│   ├── ticket_analysis.json      # 출력 스키마
│   └── ticket_normalized.json    # 입력 스키마
└── core/
    └── config.py             # 환경 설정 (LLM, Supabase 등)
```

---

## 5. 프론트엔드 핵심 파일 구조

```
project-a/fdk/
├── app/
│   ├── index.html            # 메인 UI (ticket_top_navigation 사이드바)
│   ├── scripts/
│   │   ├── app.js            # 메인 진입점, 티켓 데이터 로딩, 이벤트 바인딩
│   │   ├── analysis-ui.js    # 분석 탭 UI (상태 머신, 렌더링, HITL)
│   │   ├── stream-utils.js   # 백엔드 API 호출 래퍼 (SSE, REST)
│   │   ├── backend-config.js # 환경별 백엔드 URL 관리
│   │   └── components/       # UI 컴포넌트
│   └── styles/
│       └── main.css
├── server/
│   └── server.js             # FDK Serverless 함수
├── config/
│   └── requests.json         # 외부 API 요청 설정
└── manifest.json             # FDK 플랫폼 설정 (v3.0)
```

**프론트 분석 호출 단일화 완료**:
- `app.js`의 `handleAnalyzeTicket()`이 단일 진입점
- `analysis-ui.js`의 `runAnalysis()`를 호출
- `stream-utils.js`의 `fetchAnalysisStream()`으로 Path B 호출

---

## 6. API 엔드포인트 전체 목록

### 분석 (Path B — 주 경로)

| Method | Path | 역할 |
|--------|------|------|
| POST | `/api/tickets/{id}/analyze` | 동기 분석 |
| POST | `/api/tickets/{id}/analyze/stream` | SSE 스트리밍 분석 |
| GET | `/api/tickets/{id}/analyses` | 분석 이력 조회 |
| GET | `/api/tickets/{id}/analyses/{analysis_id}` | 특정 분석 조회 |

### 분석 (Path A — 레거시 유지)

| Method | Path | 역할 |
|--------|------|------|
| POST | `/api/assist/analyze` | 동기 분석 |
| POST | `/api/assist/analyze/stream` | SSE 스트리밍 분석 |
| POST | `/api/assist/field-proposals` | 필드 제안만 (경량) |
| POST | `/api/assist/field-proposals/stream` | 필드 제안 SSE |

### HITL 피드백

| Method | Path | 역할 |
|--------|------|------|
| POST | `/api/assist/feedback/submit` | 피드백 제출 (helpful/not_helpful) |
| POST | `/api/assist/feedback/edit` | 상담원 수정 응답 저장 |
| POST | `/api/assist/training/export` | 파인튜닝 데이터 내보내기 |

### 채널 BFF

| Method | Path | 역할 |
|--------|------|------|
| POST | `/api/fdk/v1/chat` | FDK 채팅 |
| GET | `/api/fdk/v1/chat/stream` | FDK 채팅 SSE |
| POST | `/api/web/v1/chat` | 웹 채팅 |
| GET | `/api/web/v1/chat/stream` | 웹 채팅 SSE |

---

## 7. LLM 설정

```
llm_provider: "gemini"  (기본값)
gemini_primary_model: "gemini-2.5-flash"
gemini_fallback_model: "gemini-2.0-flash"
```

- 멀티 프로바이더 지원: Gemini, OpenAI, DeepSeek
- 로컬 LLM 옵션: `llm_local_enabled=true`로 특정 purpose에만 사용 가능
- 모든 설정은 환경 변수로 주입 (하드코딩 없음)

---

## 8. Orchestrator 파이프라인 상세

```
1. 입력 검증 (ticket_normalized 스키마)
2. 프롬프트 렌더링 (ticket_analysis_cot.yaml + Jinja2)
3. 대화 노이즈 제거 (denoise_conversations)
4. LLM 호출 (llm_gateway)
5. JSON 파싱 + 복구 (json_repair)
6. 가드레일 적용 (guardrails — 금지어, evidence 정규화)
7. summary_sections 보장 (LLM 미생성 시 narrative에서 fallback)
8. Gate 판정 (confidence → CONFIRM/EDIT/DECIDE/TEACH)
9. 결과 저장 (Supabase persistence)
10. SSE 스트리밍 (필드별 순차 전송)
```

**스트리밍 필드 순서**: narrative → summary_sections → confidence → root_cause → resolution → intent → sentiment → risk_tags → field_proposals → escalation_history → current_status → evidence

---

## 9. 개발 환경

### 백엔드

```bash
cd ~/GitHub/agent-platform
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
pytest -q  # 테스트
```

### 프론트엔드

```bash
export PATH=/Users/alan/.nvm/versions/node/v18.20.8/bin:$PATH
cd ~/GitHub/project-a/fdk
fdk run  # http://localhost:10001
```

### Freshdesk 라이브 테스트

```
https://support.wedosoft.net/a/tickets/{id}?dev=true
```

### 환경별 백엔드 연결

| 환경 | 호스트 |
|------|--------|
| production | `api.wedosoft.net` |
| development (FDK localhost) | ngrok 터널 |

---

## 10. 네이밍 규칙

- `_v1`, `_v2`, `_optimized`, `_improved` 등 버전 접미사 사용 금지
- 리팩터링 시 이전 코드 백업은 `_YYYYMMDD` 형식
- Python: snake_case / JS: camelCase, PascalCase

---

## 11. Trust Boundary

- Freshdesk API Key → FDK iparams에만 존재, 외부 전송 금지
- tenant_id → 서버가 도메인에서 추출 (클라이언트 주장 불가)
- 원문 대화/PII → DB 저장 금지
- Sentry에 PII 스크러빙 적용 (`_scrub_pii()`)

---

## 12. 미완료 / 알려진 이슈

### DB 마이그레이션
- `supabase/migrations/20260224000000_hitl_feedback_tables.sql` 미적용
- HITL 피드백 API는 DB 없이도 graceful 동작하나 실제 저장 안 됨

### 프론트엔드 Path B 전환
- `analysis-ui.js`는 이미 Path B(`/api/tickets/{id}/analyze/stream`) 호출
- 기존 Path A 관련 UI 코드(field_proposals, chat, teach 등)는 프론트에서 주석처리 진행 중

### HITL 피드백 엔드포인트 위치
- 현재 `/api/assist/feedback/*`에 있음 (Path A 라우트 파일)
- Path B로 완전 전환 시 `/api/tickets/` 하위로 이동 검토 필요

---

## 13. 저장소 분리 안내

**2026-03-05 이후:**

- **agent-platform**: 홈페이지(www.wedosoft.net) 백엔드만 관리
- **project-a-backend**: 에이전트 플랫폼 핵심 로직을 이 저장소로 클론 후 새로 작업 진행

자세한 내용은 `project-a-backend` 저장소의 HANDOVER 문서를 참고하세요.
