# Repository Guidelines

> 모든 커뮤니케이션(이슈, PR, 리뷰, 코멘트)은 한국어로 진행합니다.

## Project Structure & Module Organization
- `app/` FastAPI backend code: `api/` routers, `services/` integrations (Supabase, Freshdesk, Gemini), `models/` pydantic schemas, `core/` settings and shared utils, `main.py` app entrypoint.
- `tests/` pytest suites mirroring app modules (e.g., `test_chat.py`, `test_sessions.py`).
- `static/` optional pre-built frontend; served at `/` when present.
- `docs/` project notes; `fly.toml` and `Dockerfile` for deployment.
- `.env.local.example` lists required environment variables; copy to `.env.local` for local runs.

## Setup, Build, and Development Commands
- Create venv & install (dev extras include pytest/httpx):
  ```bash
  python3 -m venv venv && source venv/bin/activate
  pip install -e '.[dev]'
  ```
- Run API locally with reload on port 8000:
  ```bash
  uvicorn app.main:app --reload --port 8000
  ```
- To use local pipeline proxy, ensure `AGENT_PLATFORM_PIPELINE_BASE_URL` (or default `http://localhost:4000/pipeline`) is reachable; adjust in `.env.local`.

## Coding Style & Naming Conventions
- Python 3.9+, PEP 8 style with 4-space indents; prefer type hints for public functions.
- Pydantic models live in `app/models`; names use `*Request`, `*Response`, or domain nouns (e.g., `SessionCreateRequest`).
- Services under `app/services` are thin adapters; keep pure logic separated from I/O for testability.
- Routes in `app/api` grouped by feature; keep path prefixes consistent with `settings.api_prefix` (default `/api`).
- **홈페이지 연동**: 프론트는 `homepage` repo (www.wedosoft.net)에서 `NEXT_PUBLIC_AGENT_API_BASE_URL=https://acent-api.fly.dev/api`로 호출. CORS는 `app/main.py`에서 `https://www.wedosoft.net`, `https://wedosoft.net` 및 regex로 허용. 문서/에이전트 챗은 `GET /api/status`, `POST /api/session` 사용.

## Testing Guidelines
- Test runner: `pytest`.
  ```bash
  pytest -q
  ```
- Place tests in `tests/` using `test_*.py` and functions `test_*`; mirror module names where possible (`app/services/freshdesk_*` → `tests/test_freshdesk_*.py`).
- Add fixtures in `tests/conftest.py`; favor dependency overrides over network calls.
- Ensure new endpoints or services include positive, edge, and failure cases (mock external APIs rather than calling them).

## Environment & Security
- Never commit secrets. Keep them in `.env.local` (loaded via `pydantic_settings`); start from `.env.local.example` and override locally.
- Required keys include Supabase (`AGENT_PLATFORM_SUPABASE_COMMON_*`), Gemini (`GEMINI_API_KEY`, fallbacks), Freshdesk (`AGENT_PLATFORM_FRESHDESK_*`), and optional Redis (`AGENT_PLATFORM_REDIS_URL`).
- Validate new env vars in `app/core/config.py` and document defaults.

## Commit & Pull Request Guidelines
- Commit style follows Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`). Keep messages under ~72 chars summary, detailed body when needed.
- Before opening a PR: run `pytest -q` and local `uvicorn` smoke test if APIs changed; note any failing scenarios explicitly.
- PR description should include purpose, major changes, test evidence, and linked issue/ticket. Add screenshots or sample requests for API/interface changes.
- Keep diffs focused; prefer separate PRs for unrelated changes (infra vs feature vs docs).

## Instruction Verification
- 사용자 요구사항을 작업 전 체크리스트로 정리하고, 각 항목이 어느 파일/라인에서 충족되는지 명시한다.
- 요구사항 충족 여부를 설명할 때는 실제 코드/로그 근거(파일 경로와 라인 번호 포함)를 함께 제시한다.
- 핵심 동작이나 데이터 흐름에 대한 답변 전에는 dry-run이나 최소한의 재현 절차로 실제 결과를 확인한다.
- 의심스러운 부분이나 사용자의 지적이 있으면 즉시 코드를 다시 확인하고 근거와 함께 상황을 공유한다.
