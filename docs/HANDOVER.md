# NexusDesk 통합 핸드오버 문서

> **작성일**: 2026-02-23
> **대상 브랜치**: `feat/nexus-integration` (agent-platform, project-a 양쪽)
> **작업 범위**: nexus-ai의 핵심 기능 6가지를 agent-platform(백엔드) + project-a(프론트엔드)에 이식

---

## 1. 작업 배경 및 목적

`nexus-ai`(모노레포 원형)에서 검증된 핵심 차별화 기능들을 실제 프로덕션 레포 두 곳에 이식하는 작업이다.

| 레포 | 역할 | 브랜치 |
|------|------|--------|
| `agent-platform` | FastAPI 백엔드 | `feat/nexus-integration` |
| `project-a` | Freshdesk FDK 프론트엔드 | `feat/nexus-integration` |

---

## 2. 백엔드 변경사항 (agent-platform)

### 2.1 새 파일

#### `app/services/denoise.py` (NoCut Denoise)
- **출처**: nexus-ai `api/app/denoise.py` (218줄) 기반
- **역할**: 긴 대화를 절단하지 않고 결정적 규칙으로 노이즈 제거
  - 이메일 인용 블록 제거 (>`>` 패턴)
  - 서명/footer 자동 제거
  - HTML → 텍스트 정규화
  - 중복 메시지 제거
  - **보존 규칙**: agent 메시지 및 해결/조치 신호가 있는 메시지는 절대 제거하지 않음
- **핵심 함수**: `denoise_conversations(conversations: List[Dict]) -> DenoiseResult`
- **포맷 어댑터**: agent-platform의 `{"body_text", "incoming", "private"}` → nexus-ai의 `{"author_role", "channel", "text"}` 변환 내장

#### `app/services/guardrails.py` (가드레일)
- **출처**: nexus-ai `api/app/main.py` lines 460–589 기반
- **역할**: LLM 출력의 품질 보장
- **포함 기능**:
  - `FORBIDDEN_PHRASES`: 금지 표현 목록 ("무조건", "원인입니다", "확실히", "반드시", "문제 없습니다", "때문입니다")
  - `normalize_evidence_items()`: 비정수 evidence 자동 제거
  - `fix_evidence_fields()`: root_causes + recommended_actions의 evidence 일괄 보정
  - `contains_forbidden_phrases()`: 금지어 검사
  - `apply_guardrails(analysis) -> Tuple[Dict, List[str]]`: 통합 가드레일 적용

#### `app/models/feedback.py` (HITL 데이터 모델)
- `FeedbackSubmitRequest`: `analysis_id`, `event_type`, `rating?`, `feedback_text?`, `agent_id?`
- `FeedbackEditRequest`: `analysis_id`, `approved_response`, `agent_id?`
- `TrainingExportRequest`: `limit?`, `min_rating?`, `mark_exported?`
- 각 요청의 Response 모델 포함

#### `app/services/feedback_repository.py` (HITL DB 접근)
- **출처**: nexus-ai `api/app/repository.py` HITL 메서드 기반
- Supabase 클라이언트 사용 (agent-platform 패턴 준수)
- **메서드**:
  - `upsert_training_sample()`: 티켓 분석 결과를 training_samples에 저장
  - `submit_feedback()`: helpful/not_helpful 피드백 이벤트 기록
  - `update_approved_response()`: 상담원이 수정한 응답을 approved_response로 저장
  - `get_exportable_samples()`: 파인튜닝용 데이터 조회
  - `insert_quality_log()`: 품질 로그 기록

#### `supabase/migrations/20260224000000_hitl_feedback_tables.sql`
세 개 테이블 신규 생성:
- `training_samples`: 티켓별 분석 원본 + AI 응답 + 상담원 승인 응답
- `feedback_events`: 이벤트 소싱 패턴의 피드백 로그
- `quality_logs`: 언어 불일치, 가드레일 위반 등 품질 이벤트

> **주의**: 이 마이그레이션은 아직 Supabase에 직접 적용하지 않았음 (DB 스키마 확인 후 수동 적용 필요)

#### 테스트 파일 (3개)
- `tests/test_denoise.py`: denoise 규칙 단위 테스트 (109줄)
- `tests/test_guardrails.py`: 가드레일 검증 단위 테스트 (126줄)
- `tests/test_feedback_api.py`: HITL API 통합 테스트 (160줄)

### 2.2 수정 파일

#### `app/services/orchestrator/ticket_analysis_orchestrator.py`
두 곳에 통합:

```python
# Step 3.5: 가드레일 적용 (JSON 파싱 성공 후, gate 판단 전)
from app.services.guardrails import apply_guardrails
analysis, violations = apply_guardrails(analysis)
if violations:
    logger.warning(f"[guardrails] Violations fixed: {violations}")
```

```python
# _build_prompt_context() 내부 — denoise 적용
from app.services.denoise import denoise_conversations
raw_conversations = normalized_input.get("conversations", [])
denoise_result = denoise_conversations(raw_conversations)
context["conversations"] = denoise_result.conversation
context["denoise_kept_indices"] = denoise_result.kept_original_indices
```

#### `app/api/routes/assist.py`
HITL 엔드포인트 3개 추가:

| Method | Path | 역할 |
|--------|------|------|
| `POST` | `/assist/feedback/submit` | helpful/not_helpful 피드백 제출 |
| `POST` | `/assist/feedback/edit` | 상담원 응답 수정/승인 |
| `POST` | `/assist/training/export` | 파인튜닝 데이터 내보내기 |

#### `app/core/config.py`
```python
sentry_dsn: Optional[str] = None
sentry_environment: str = "development"
```

#### `app/main.py`
Sentry 초기화 + PII 스크러빙:
- `_scrub_pii()` 함수 추가: conversation, subject, approved_response, feedback_text 필드 마스킹
- Authorization, X-Api-Key 헤더 마스킹
- `sentry_sdk.init()` 조건부 초기화 (`SENTRY_DSN` 설정 시)

#### `pyproject.toml`
```toml
"sentry-sdk[fastapi]"  # 추가
```

---

## 3. 프론트엔드 변경사항 (project-a)

### 3.1 브랜치 커밋 요약

| 커밋 | 내용 |
|------|------|
| `654ea2e` | 4탭 UI, 다크모드, 에스컬레이션/상태 섹션, HITL 피드백 UI 초기 구현 |
| `7d17980` | 분석 UI 개선, renderSolutionSteps 추가, 다크모드 색상 수정 |
| `237ce30` | HITL 피드백 버튼 표시 버그 수정, showFeedbackSection export |

### 3.2 `fdk/app/index.html` 주요 추가 영역

```
Analyze 탭 (id="analysisContent")
  └── #analysisResult
      ├── 기존: gate badge, summary, root_cause, resolution, field_proposals
      ├── 신규: #escalationSection     — 에스컬레이션 이력 (has_escalation, handoffs)
      ├── 신규: #currentStatusSection  — 현재 상태 (resolved/pending/unresolved)
      └── 신규: #feedbackSection       — HITL 피드백 (👍👎✏️) [초기 hidden]
```

CSS 추가 (`<style>` 블록):
- CSS 변수 기반 다크모드: `--color-app-bg`, `--color-app-card`, `--color-app-text`, `--color-app-muted`, `--color-app-border`, `--color-app-primary`
- `html.dark { ... }` 오버라이드
- `.feedback-btn.selected-helpful/selected-not-helpful` 상태 스타일

### 3.3 `fdk/app/scripts/analysis-ui.js` 주요 추가

| 함수 | 역할 |
|------|------|
| `renderEscalationHistory(escalation)` | handoff 체인 시각화 (from → to, reason, evidence) |
| `renderCurrentStatus(status)` | resolved/pending/unresolved 배지 + pending items |
| `showFeedbackSection(analysisId)` | #feedbackSection hidden 해제 + 버튼 상태 초기화 |
| `submitFeedback(eventType)` | `/api/assist/feedback/submit` 호출 |
| `openEditModal()` / `saveEditedResponse()` | 수정 모달 open + `/api/assist/feedback/edit` 호출 |
| `setupDarkMode()` / `toggleDarkMode()` | localStorage 저장 + html.dark 클래스 토글 |

**window.AnalysisUI exports** (외부 접근용):
```js
window.AnalysisUI = {
  runAnalysis, setState, setCurrentTab, AnalysisState,
  submitFeedback, openEditModal, toggleDarkMode,
  showFeedbackSection   // ← 이번 세션에서 추가
};
```

### 3.4 `fdk/app/scripts/app.js` 주요 변경

#### renderSolutionSteps(solution) 함수 신규 추가
- `solution`이 JSON 문자열 `'["step1","step2"]'`로 올 때 파싱 후 번호 리스트 렌더링
- 배열 of 객체 `[{action, rationale}]` 형식도 지원
- plain string fallback

#### renderAnalysisResult(proposal) 수정
1. **다크모드 색상**: `bg-white` → `bg-app-card`, `bg-gray-50` → `bg-app-bg`, `text-gray-800` → `text-app-text` 등 전면 교체
2. **HITL 피드백 HTML 인라인 추가** (innerHTML 교체 후 #feedbackSection 복원 문제 해결):

```js
html += `
  <div id="feedbackSection" class="... hidden">
    <h4>이 분석이 도움이 되었나요?</h4>
    <button id="feedbackHelpfulBtn">👍 도움됨</button>
    <button id="feedbackNotHelpfulBtn">👎 부정확</button>
    <button id="feedbackEditBtn">✏️ 수정</button>
    <div id="feedbackResult" class="hidden"></div>
  </div>
`;
elements.analysisContent.innerHTML = html;

// analysis_id 없으면 proposal.id를 fallback으로 사용
const analysisId = proposal.analysis_id || proposal.analysisId || proposal.id;
if (window.AnalysisUI?.showFeedbackSection) {
  window.AnalysisUI.showFeedbackSection(analysisId || 'pending');
}
```

3. **renderFieldSuggestions / renderFieldSuggestionsCard**: 동일하게 다크모드 색상 수정

### 3.5 `fdk/app/styles/main.css`
다크모드 CSS 변수 오버라이드 및 에스컬레이션 관련 스타일 추가

### 3.6 `fdk/app/scripts/backend-config.js` (변경 없음)
```js
const BACKEND_HOSTS = {
  production: 'api.wedosoft.net',
  development: 'ameer-timberless-paragogically.ngrok-free.dev',  // 로컬 FDK → ngrok
  local: 'localhost:8000'  // 현재 사용 안 됨
};
// localhost → 'development' 환경 → ngrok (의도된 동작)
// freshdesk.com → 'production' → api.wedosoft.net
```

---

## 4. 알려진 이슈 및 미완료 사항

### 4.1 두 renderAnalysisResult() 충돌 문제 (중요)

`app.js`와 `analysis-ui.js` 둘 다 `#analyzeBtn` click 리스너를 등록하여, 분석 버튼 클릭 시 두 API 호출이 동시에 발생한다:

| 파일 | 함수 | API 엔드포인트 |
|------|------|----------------|
| `app.js` | `handleAnalyzeTicket()` | `POST /api/assist/analyze/stream` (SSE) |
| `analysis-ui.js` | `runAnalysis()` | `StreamUtils.analyzeTicketV2()` |

현재는 `app.js` 버전이 나중에 완료되어 UI를 덮어씀 (사실상 `app.js` 버전이 표시됨). 장기적으로는 **하나의 분석 흐름으로 통합**해야 한다.

### 4.2 analysis_id 미전달 문제

`/api/assist/analyze/stream`의 `complete` 이벤트에 `analysis_id`가 없음:
```json
{ "type": "complete", "data": { "proposal": {...}, "analysis": {...} } }
```
현재 workaround: `proposal.id`를 `analysis_id` fallback으로 사용. 백엔드에서 `analysis_id`를 complete 이벤트에 포함시키면 더 정확한 HITL 추적 가능.

### 4.3 DB 마이그레이션 미적용

`supabase/migrations/20260224000000_hitl_feedback_tables.sql`을 Supabase에 아직 적용하지 않았다. 피드백 API 엔드포인트는 DB 없이도 graceful하게 동작하나 (경고 로그 후 계속), 실제 저장은 안 된다.

적용 방법:
```bash
cd ~/GitHub/agent-platform
supabase link --project-ref <PROJECT_REF>
supabase db push --password <DB_PASSWORD>
```

### 4.4 에스컬레이션/현재상태 섹션 미표시

`#escalationSection` 및 `#currentStatusSection`은 `analysis-ui.js`의 `renderAnalysisResult()`에서 채워지는데, 현재 `app.js` 버전이 최종 렌더링을 덮어쓰기 때문에 이 섹션이 표시되지 않는다. 위 4.1 이슈와 동일한 원인.

### 4.5 프롬프트 스키마 미확인

`app/prompts/registry/ticket_analysis_cot_v1.yaml`에 `escalation_history` + `current_status` 필드가 추가되었으나, 현재 백엔드가 실제로 이 필드를 반환하는지 검증이 필요하다.

---

## 5. 개발 환경 설정

### 5.1 nexus-ai 레포에서 두 서버 실행

`/Users/alan/GitHub/nexus-ai/.claude/launch.json` 설정:

```json
{
  "configurations": [
    {
      "name": "FDK (project-a)",
      "runtimeExecutable": "/bin/bash",
      "runtimeArgs": ["-c", "export PATH=/Users/alan/.nvm/versions/node/v18.20.8/bin:$PATH && cd /Users/alan/GitHub/project-a/fdk && fdk run"],
      "port": 10001
    },
    {
      "name": "agent-platform API",
      "runtimeExecutable": "/bin/bash",
      "runtimeArgs": ["-c", "cd /Users/alan/GitHub/agent-platform && source venv/bin/activate && uvicorn app.main:app --reload --port 8000"],
      "port": 8000
    }
  ]
}
```

### 5.2 FDK 로컬 실행 시 Node 버전

FDK CLI는 nvm으로 관리되는 Node v18이 필요하다:
```bash
export PATH=/Users/alan/.nvm/versions/node/v18.20.8/bin:$PATH
cd ~/GitHub/project-a/fdk
fdk run
# → http://localhost:10001
```

### 5.3 agent-platform 실행

```bash
cd ~/GitHub/agent-platform
source venv/bin/activate   # .venv가 아닌 venv/
uvicorn app.main:app --reload --port 8000
```

### 5.4 Freshdesk 라이브 테스트

```
https://support.wedosoft.net/a/tickets/12791?dev=true
```
- `?dev=true`: FDK가 localhost:10001 iframe을 로드
- 인증은 수동으로 처리
- FDK → ngrok → 로컬 uvicorn 연결 순서

---

## 6. 검증 방법

### 6.1 백엔드 단위 테스트

```bash
cd ~/GitHub/agent-platform
source venv/bin/activate
python -m pytest tests/test_denoise.py tests/test_guardrails.py tests/test_feedback_api.py -v
```

### 6.2 HITL API 스모크 테스트

```bash
# 피드백 제출 (DB 설정 필요)
curl -X POST http://localhost:8000/api/assist/feedback/submit \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-ID: wedosoft' \
  -d '{"analysis_id":"test-uuid","event_type":"helpful","rating":5}'

# 응답 수정
curl -X POST http://localhost:8000/api/assist/feedback/edit \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-ID: wedosoft' \
  -d '{"analysis_id":"test-uuid","approved_response":{"summary":"수정된 요약"}}'
```

### 6.3 프론트엔드 검증 체크리스트

| 항목 | 방법 |
|------|------|
| 다크모드 토글 | 사이드바 헤더 달/해 아이콘 클릭 |
| 해결책 번호 리스트 | 분석 실행 후 "해결책:" 섹션 확인 |
| HITL 피드백 버튼 | 분석 완료 후 사이드바 하단 확인 |
| 필드 제안 | "필드 업데이트 제안" 섹션 + "변경 사항 적용하기" 버튼 |

---

## 7. 다음 작업 제안 (Priority 순)

### P0 — 즉시 필요

1. **Supabase 마이그레이션 적용**
   `supabase/migrations/20260224000000_hitl_feedback_tables.sql`을 프로덕션/스테이징 DB에 적용

2. **두 renderAnalysisResult 통합**
   `app.js`의 `handleAnalyzeTicket`과 `analysis-ui.js`의 `runAnalysis` 중 하나로 통일
   → 권장: `app.js` 버전으로 통합 후 `analysis-ui.js`의 버튼 리스너 제거

3. **complete 이벤트에 analysis_id 포함**
   `assist.py`의 `/api/assist/analyze/stream` complete 이벤트에 `analysis_id` 추가:
   ```python
   yield {"type": "complete", "data": {
     "proposal": proposal_data,
     "analysis": analysis,
     "analysis_id": analysis_id,   # ← 추가
     ...
   }}
   ```

### P1 — 단기

4. **에스컬레이션/현재상태 렌더링 복구**
   `app.js`의 `renderAnalysisResult()`에 `renderEscalationHistory()` + `renderCurrentStatus()` 호출 추가

5. **프롬프트 스키마 escalation_history + current_status 검증**
   실제 API 응답에서 해당 필드가 반환되는지 확인 및 LLM 출력 검증

6. **feat/nexus-integration → main PR 생성**
   양쪽 레포 모두 PR 생성 및 코드 리뷰

### P2 — 중기

7. **ngrok URL 교체**
   `backend-config.js`의 `development` 호스트를 ngrok free tier에서 안정적인 터널 또는 스테이징 서버로 전환

8. **Sentry DSN 설정**
   프로덕션 배포 시 `SENTRY_DSN` 환경 변수 설정 (현재 미설정)

9. **파인튜닝 파이프라인 연결**
   `POST /assist/training/export` 결과를 OpenAI fine-tuning 포맷으로 변환하는 후처리 스크립트 작성

---

## 8. 파일 변경 전체 목록

### agent-platform (`feat/nexus-integration`)

| 구분 | 파일 |
|------|------|
| NEW | `app/services/denoise.py` |
| NEW | `app/services/guardrails.py` |
| NEW | `app/models/feedback.py` |
| NEW | `app/services/feedback_repository.py` |
| NEW | `supabase/migrations/20260224000000_hitl_feedback_tables.sql` |
| NEW | `tests/test_denoise.py` |
| NEW | `tests/test_guardrails.py` |
| NEW | `tests/test_feedback_api.py` |
| EDIT | `app/services/orchestrator/ticket_analysis_orchestrator.py` |
| EDIT | `app/api/routes/assist.py` |
| EDIT | `app/core/config.py` |
| EDIT | `app/main.py` |
| EDIT | `pyproject.toml` |

### project-a (`feat/nexus-integration`)

| 구분 | 파일 |
|------|------|
| EDIT | `fdk/app/index.html` |
| EDIT | `fdk/app/scripts/analysis-ui.js` |
| EDIT | `fdk/app/scripts/app.js` |
| EDIT | `fdk/app/styles/main.css` |
| NEW | `.claude/launch.json` |
