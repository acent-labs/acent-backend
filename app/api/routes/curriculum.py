"""커리큘럼 API 라우터."""

import json
import logging
from typing import List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Path
from fastapi.responses import StreamingResponse

from app.models.curriculum import (
    CurriculumModule,
    QuizQuestion,
    QuizChoice,
    QuizSubmitRequest,
    QuizSubmitResponse,
    ModuleProgress,
    UpdateProgressRequest,
    ProgressSummary,
    ModuleContent,
    ModuleContentResponse,
)
from app.services.curriculum_repository import (
    get_curriculum_repository,
    CurriculumRepositoryError,
)
from app.services.gemini_file_search_client import GeminiFileSearchClient
from app.services.gemini_client import get_gemini_client
from app.models.metadata import MetadataFilter
from app.core.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/curriculum", tags=["curriculum"])


def _get_settings():
    """설정 가져오기."""
    return get_settings()


def _build_product_filters(product_id: str, category_slug: Optional[str] = None) -> List[MetadataFilter]:
    """공용 스토어에서 제품별 문서만 검색하도록 메타데이터 필터를 생성."""
    filters: List[MetadataFilter] = []
    if product_id:
        filters.append(MetadataFilter(key="product", value=product_id))
    if category_slug:
        filters.append(MetadataFilter(key="category", value=category_slug))
    return filters


def _get_file_search_client() -> GeminiFileSearchClient:
    """GeminiFileSearchClient 인스턴스 생성."""
    settings = _get_settings()
    return GeminiFileSearchClient(
        api_key=settings.gemini_api_key,
        primary_model=settings.gemini_primary_model,
        fallback_model=settings.gemini_fallback_model,
    )


# ============================================
# SSE 헬퍼
# ============================================

def format_sse(event: str, data: dict) -> str:
    """SSE 포맷으로 변환."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ============================================
# 모듈 조회
# ============================================

@router.get("/modules", response_model=List[CurriculumModule])
async def get_modules(
    session_id: str = Query(..., alias="sessionId", description="세션 ID"),
    product: str = Query("freshservice", description="제품 ID"),
):
    """
    커리큘럼 모듈 목록 조회.
    
    - 각 모듈의 기본 정보 반환
    """
    try:
        repo = get_curriculum_repository()
        modules = await repo.get_modules(product=product)
        return modules
    except CurriculumRepositoryError as e:
        logger.error(f"Failed to get modules: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modules/{module_id}", response_model=CurriculumModule)
async def get_module(
    module_id: UUID = Path(..., description="모듈 ID"),
):
    """모듈 상세 조회."""
    try:
        repo = get_curriculum_repository()
        module = await repo.get_module_by_id(module_id)
        if not module:
            raise HTTPException(status_code=404, detail="Module not found")
        return module
    except CurriculumRepositoryError as e:
        logger.error(f"Failed to get module: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 학습 콘텐츠 스트리밍 (RAG 기반)
# ============================================

@router.get("/modules/{module_id}/learn/stream")
async def stream_learning_content(
    module_id: UUID = Path(..., description="모듈 ID"),
    session_id: str = Query(..., alias="sessionId", description="세션 ID"),
):
    """
    모듈 학습 콘텐츠 스트리밍 (RAG 기반).
    
    - RAG 스토어에서 모듈 관련 콘텐츠 검색
    - 스트리밍 응답으로 학습 콘텐츠 제공
    - 학습 시작 시 진도 기록
    """
    repo = get_curriculum_repository()
    
    # 모듈 조회
    module = await repo.get_module_by_id(module_id)
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # 학습 시작 기록
    await repo.start_learning(session_id, module_id)
    
    async def event_generator():
        try:
            settings = _get_settings()
            store_product = settings.gemini_store_common
            
            # RAG 검색 쿼리 구성 (제품/카테고리 범위 명시)
            query = f"""{module.target_product_id} {module.name_ko} 모듈 전용 학습 콘텐츠를 생성하세요.

다음 섹션을 모두 포함하세요:
1) 개요 및 핵심 개념
2) 주요 기능과 사용법
3) 실무 활용 팁
4) 자주 묻는 질문

모듈 설명: {module.description or '설명 없음'}
제품/카테고리 외의 내용은 포함하지 마세요."""

            # RAG 스토어 검색
            rag_stores = []
            if store_product:
                rag_stores.append(store_product)
            
            if not rag_stores:
                yield format_sse("error", {"message": "RAG store not configured"})
                return
            
            # 스트리밍 생성
            client = _get_file_search_client()
            
            full_response = ""
            product_filters = _build_product_filters(module.target_product_id, module.kb_category_slug)

            async for chunk in client.stream_search(
                query=query,
                store_names=rag_stores,
                metadata_filters=product_filters,
                system_instruction=(
                    f"당신은 {module.target_product_id} 제품의 '{module.name_ko}' 모듈 강사입니다. "
                    "스토어 메타데이터 product/category에 맞는 문서만 사용하세요. 범위를 벗어나면 에러를 반환하세요."
                ),
            ):
                event_type = chunk.get("event")
                data = chunk.get("data", {})
                
                if event_type == "status":
                    yield format_sse("status", data)
                elif event_type == "result":
                    text = data.get("text", "")
                    if text:
                        full_response = text
                        yield format_sse("chunk", {"text": text})
                        yield format_sse("result", {"text": text})
                elif event_type == "error":
                    yield format_sse("error", data)
                    return

            if not full_response:
                yield format_sse("error", {"message": "학습 콘텐츠를 생성하지 못했습니다. 관리자에게 콘텐츠를 추가해 달라고 요청하세요."})
            
        except Exception as e:
            logger.error(f"Learning content stream error: {e}")
            yield format_sse("error", {"message": str(e)})
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )


# ============================================
# 모듈 콘텐츠 조회 (정적 콘텐츠 - DB에서 로드)
# ============================================

@router.get("/modules/{module_id}/contents", response_model=ModuleContentResponse)
async def get_module_contents(
    module_id: UUID = Path(..., description="모듈 ID"),
    level: Optional[str] = Query(None, description="난이도 필터 (basic, intermediate, advanced)"),
):
    """
    모듈의 정적 학습 콘텐츠 조회.
    
    - DB에 저장된 콘텐츠를 즉시 반환 (LLM 지연 없음)
    - 난이도별 (기초/중급/고급) 콘텐츠 제공
    - 각 섹션: 개요, 핵심 개념, 기능, 실습, FAQ
    """
    try:
        repo = get_curriculum_repository()
        contents = await repo.get_module_contents(module_id, level)
        return contents
    except CurriculumRepositoryError as e:
        logger.error(f"Failed to get module contents: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/modules/{module_id}/contents/{section_type}", response_model=ModuleContent)
async def get_section_content(
    module_id: UUID = Path(..., description="모듈 ID"),
    section_type: str = Path(..., description="섹션 타입 (overview, core_concepts, features, practice, faq)"),
    level: str = Query("basic", description="난이도 (basic, intermediate, advanced)"),
):
    """
    특정 섹션의 콘텐츠 조회.
    
    - 한 섹션만 필요할 때 사용
    - 콘텐츠가 없으면 404 반환
    """
    try:
        repo = get_curriculum_repository()
        content = await repo.get_section_content(module_id, section_type, level)
        if not content:
            raise HTTPException(
                status_code=404, 
                detail=f"Content not found for section '{section_type}' at level '{level}'"
            )
        return content
    except CurriculumRepositoryError as e:
        logger.error(f"Failed to get section content: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 모듈 섹션 콘텐츠 스트리밍 (LLM 기반 - 폴백/채팅용)
# ============================================

@router.get("/modules/{module_id}/section/stream")
async def stream_section_content(
    module_id: UUID = Path(..., description="모듈 ID"),
    session_id: str = Query(..., alias="sessionId", description="세션 ID"),
    section_id: str = Query(..., alias="sectionId", description="섹션 ID"),
    section_prompt: str = Query(..., alias="sectionPrompt", description="섹션 프롬프트"),
):
    """
    모듈 섹션별 학습 콘텐츠 스트리밍 (RAG 기반).
    
    - 개요, 주요 기능, 실무 활용, FAQ 등 섹션별로 콘텐츠 생성
    - 짧고 집중된 콘텐츠로 학습 효율 향상
    - 정적 콘텐츠가 없을 때 폴백으로 사용
    """
    repo = get_curriculum_repository()
    
    # 모듈 조회
    module = await repo.get_module_by_id(module_id)
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    async def event_generator():
        try:
            settings = _get_settings()
            store_product = settings.gemini_store_common
            
            # RAG 검색 쿼리 구성
            query = f"""{module.target_product_id} 제품의 '{module.name_ko}' 모듈 섹션 요청입니다.

{section_prompt}

컨텍스트(모듈 설명): {module.description or '설명 없음'}
제품/카테고리 범위를 벗어난 내용은 포함하지 마세요."""

            # RAG 스토어 검색
            rag_stores = []
            if store_product:
                rag_stores.append(store_product)
            
            if not rag_stores:
                yield format_sse("error", {"message": "RAG store not configured"})
                return
            
            # 스트리밍 생성
            client = _get_file_search_client()
            
            product_filters = _build_product_filters(module.target_product_id, module.kb_category_slug)

            async for chunk in client.stream_search(
                query=query,
                store_names=rag_stores,
                metadata_filters=product_filters,
                system_instruction=(
                    f"당신은 {module.target_product_id} 제품의 '{module.name_ko}' 모듈 강사입니다. "
                    "스토어 메타데이터 product/category에 맞는 문서만 사용하세요. 범위를 벗어나면 에러를 반환하세요."
                ),
            ):
                event_type = chunk.get("event")
                data = chunk.get("data", {})
                
                if event_type == "status":
                    yield format_sse("status", data)
                elif event_type == "result":
                    text = data.get("text", "")
                    if text:
                        yield format_sse("chunk", {"text": text})
                        yield format_sse("result", {"text": text})
                elif event_type == "error":
                    yield format_sse("error", data)
                    return

            if not text:
                yield format_sse("error", {"message": "해당 섹션 콘텐츠를 생성하지 못했습니다. 관리자에게 콘텐츠를 추가해 달라고 요청하세요."})
            
        except Exception as e:
            logger.error(f"Section content stream error: {e}")
            yield format_sse("error", {"message": str(e)})
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )


# ============================================
# AI 멘토 채팅 (모듈 컨텍스트 인식)
# ============================================

# 모듈별 대화 히스토리 캐시
_module_chat_cache: dict = {}


@router.get("/modules/{module_id}/chat/stream")
async def stream_module_chat(
    module_id: UUID = Path(..., description="모듈 ID"),
    session_id: str = Query(..., alias="sessionId", description="세션 ID"),
    query: str = Query(..., description="질문"),
):
    """
    모듈 컨텍스트 인식 AI 멘토 채팅.
    
    - 현재 학습 중인 모듈에 맞춤화된 답변 제공
    - RAG 검색으로 관련 문서 참조
    - 대화 히스토리 유지
    """
    repo = get_curriculum_repository()
    
    # 모듈 조회
    module = await repo.get_module_by_id(module_id)
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    
    # 대화 히스토리 조회
    cache_key = f"{session_id}:{module_id}"
    history = _module_chat_cache.get(cache_key, [])
    
    async def event_generator():
        try:
            settings = _get_settings()
            store_product = settings.gemini_store_common
            
            # 제품명 매핑 (targetProductId -> 표시명, RAG 필터값)
            product_id = module.target_product_id or "freshworks"
            product_display_names = {
                "freshdesk": "Freshdesk",
                "freshdesk-omni": "Freshdesk Omni",
                "freshchat": "Freshchat",
                "freshsales": "Freshsales",
                "freshservice": "Freshservice",
            }
            product_name = product_display_names.get(product_id, "Freshworks")

            # 시스템 프롬프트 (모듈 컨텍스트 포함)
            system_prompt = f"""당신은 {product_name} {module.name_ko} 전문 교육 멘토입니다.

현재 학습 제품: {product_name}
현재 학습 모듈: {module.name_ko} ({module.name_en or ''})
모듈 설명: {module.description or ''}

당신의 역할:
- 신입사원의 {product_name} {module.name_ko} 관련 질문에 명확하고 실용적인 답변 제공
- {product_name} 공식 문서와 베스트 프랙티스 기반 설명
- 실무에서 바로 활용할 수 있는 구체적인 예시 제공
- 이해를 돕기 위한 단계별 가이드 제공

중요: 반드시 {product_name} 제품에 대해서만 답변하세요. 다른 Freshworks 제품(예: Freshservice, Freshsales 등)의 내용을 혼동하지 마세요.

답변 스타일:
- 간결하고 본론 중심 (인사말 생략)
- 한국어로 답변
- 마크다운 형식 사용"""

            # RAG 스토어 및 메타데이터 필터
            rag_stores = []
            if store_product:
                rag_stores.append(store_product)

            metadata_filters = _build_product_filters(
                product_id=product_id,
                category_slug=module.kb_category_slug,
            )
            
            # 스트리밍 생성
            client = _get_file_search_client()
            
            full_response = ""
            async for chunk in client.stream_search(
                query=query,
                store_names=rag_stores,
                metadata_filters=metadata_filters if metadata_filters else None,
                conversation_history=history[-4:],  # 최근 4턴
                system_instruction=system_prompt,
            ):
                event_type = chunk.get("event")
                data = chunk.get("data", {})

                if event_type == "status":
                    continue
                if event_type == "error":
                    yield format_sse("error", data or {"message": "문서를 불러오지 못했습니다."})
                    return
                if event_type == "result" and data.get("text"):
                    text = data["text"]
                    full_response = text
                    yield format_sse("chunk", {"text": text})
            
            if not full_response:
                yield format_sse("error", {"message": "모듈 컨텍스트에 맞는 답변을 생성하지 못했습니다. 관리자에게 콘텐츠 보강을 요청하세요."})
                return

            # 히스토리 업데이트
            history.append({"user": query, "model": full_response})
            _module_chat_cache[cache_key] = history[-10:]  # 최근 10턴만 유지

            yield format_sse("result", {"text": full_response})
            
        except Exception as e:
            logger.error(f"Module chat stream error: {e}")
            yield format_sse("error", {"message": str(e)})
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )


# ============================================
# 퀴즈 문제 조회 (자가 점검용)
# ============================================

async def _generate_quiz_questions(module_id: UUID, module_name: str, module_desc: str = "", module_content: str = "") -> List[QuizQuestion]:
    """Gemini를 사용하여 퀴즈 문제 생성 및 DB 저장."""
    try:
        # 콘텐츠 검증: 최소 300자 이상 필요
        MIN_CONTENT_LENGTH = 300
        if not module_content or len(module_content.strip()) < MIN_CONTENT_LENGTH:
            logger.warning(
                f"Module {module_id} ({module_name}) has insufficient content "
                f"({len(module_content) if module_content else 0} chars). "
                f"Minimum {MIN_CONTENT_LENGTH} chars required for quiz generation."
            )
            return []
    
        client = get_gemini_client()
        
        # 컨텍스트가 너무 길면 잘라냄 (토큰 제한 고려)
        context_text = module_content[:15000] if module_content else ""
        
        prompt = f"""
        Topic: {module_name}
        Description: {module_desc or module_name}
        
        Reference Content:
        {context_text}
        
        **IMPORTANT**: You MUST create quiz questions based ONLY on the Reference Content provided above.
        Do NOT use general knowledge or external information.
        If the reference content is insufficient, return an error message instead of generating questions.
        
        Based on the Reference Content above, generate 3 multiple-choice quiz questions to check understanding of this specific module content.
        
        Requirements:
        - Questions must be directly related to the content provided
        - Answers must be found in the reference content
        - Target audience: New employees learning the system
        - Language: Korean
        
        Format: JSON Array of objects.
        Each object must have:
        - question: string (질문)
        - choices: array of objects {{"id": "a", "text": "..."}} (ids should be a, b, c, d)
        - context: string (optional, brief background context from the reference)
        - correct_choice_id: string (id of the correct choice, e.g., "a")
        - explanation: string (explanation of why the answer is correct, referencing the content)
        - learning_point: string (key takeaway from this question)
        
        Output ONLY the JSON array.
        """
        
        response_stream = client.generate_content_stream(contents=prompt)
        text = ""
        async for chunk in response_stream:
            if chunk.text:
                text += chunk.text
        
        # Clean up markdown code blocks if present
        if text.strip().startswith("```json"):
            text = text.strip()[7:]
        if text.strip().endswith("```"):
            text = text.strip()[:-3]
            
        data = json.loads(text)
        
        repo = get_curriculum_repository()
        db_questions = []
        result_questions = []
        
        for i, item in enumerate(data):
            q_id = str(uuid4())
            
            # DB 저장용 데이터
            db_q = {
                "id": q_id,
                "module_id": str(module_id),
                "question_order": i + 1,
                "difficulty": "basic",
                "question": item["question"],
                "context": item.get("context"),
                "choices": item["choices"],
                "correct_choice_id": item["correct_choice_id"],
                "explanation": item["explanation"],
                "learning_point": item.get("learning_point"),
                "is_active": True
            }
            db_questions.append(db_q)
            
            # 반환용 데이터 (정답 제외)
            q = QuizQuestion(
                id=UUID(q_id),
                moduleId=module_id,
                questionOrder=i+1,
                question=item["question"],
                context=item.get("context"),
                choices=[QuizChoice(id=c["id"], text=c["text"]) for c in item["choices"]],
                relatedDocUrl=None
            )
            result_questions.append(q)
            
        # DB에 저장 (비동기적으로 처리하거나 기다림)
        try:
            await repo.create_questions(db_questions)
            logger.info(f"Successfully saved {len(db_questions)} generated questions to DB.")
        except Exception as e:
            logger.error(f"Failed to save generated questions to DB: {e}")
            
        return result_questions
    except Exception as e:
        logger.error(f"Failed to generate quiz questions: {e}")
        return []


@router.get("/modules/{module_id}/questions", response_model=List[QuizQuestion])
async def get_questions(
    module_id: UUID = Path(..., description="모듈 ID"),
):
    """
    모듈별 자가 점검 퀴즈 문제 조회.
    
    - 정답은 포함되지 않음
    - 학습 이해도 확인용 (통과/불통과 없음)
    """
    repo = get_curriculum_repository()
    questions = []
    
    try:
        questions = await repo.get_questions(module_id=module_id)
    except Exception as e:
        logger.warning(f"Failed to get questions from DB (might be missing tables): {e}")
        # DB 에러 시에도 AI 생성 시도로 넘어감
    
    if not questions:
        # 퀴즈가 없으면 AI로 생성 및 저장
        logger.info(f"No questions found for module {module_id}. Attempting to generate with AI...")
        
        module_name = "Onboarding Knowledge"
        module_desc = ""
        module_content = ""
        
        try:
            # 모듈 정보 조회
            module = await repo.get_module_by_id(module_id)
            if module:
                module_name = module.name_ko
                module_desc = module.description
            
            # 모듈 콘텐츠(섹션) 조회하여 컨텍스트 구성
            try:
                contents_resp = await repo.get_module_contents(module_id)
                content_parts = []
                for level, sections in contents_resp.sections.items():
                    for section in sections:
                        if section.content_md:
                            content_parts.append(f"--- Section: {section.title_ko} ---\n{section.content_md}")
                module_content = "\n\n".join(content_parts)
                
                logger.info(f"Retrieved {len(content_parts)} content sections, total length: {len(module_content)} chars")
            except Exception as e:
                logger.warning(f"Failed to get module contents for context: {e}")

            # [Fallback] 콘텐츠가 부족한 경우 RAG 검색으로 보완
            if not module_content or len(module_content.strip()) < 300:
                logger.info(f"Content insufficient for module {module_id}. Attempting RAG search to supplement content...")
                try:
                    settings = _get_settings()
                    # 제품 지식(Common)만 사용하여 RAG 검색
                    rag_stores = []
                    if settings.gemini_store_common:
                        rag_stores.append(settings.gemini_store_common)
                    
                    if rag_stores:
                        search_client = _get_file_search_client()
                        product_id = module.target_product_id if module else ""
                        category_slug = module.kb_category_slug if module else None
                        product_filters = _build_product_filters(product_id, category_slug) if product_id else None
                        logger.info(
                            "RAG quiz supplement: module=%s product=%s category=%s stores=%s filters_applied=%s",
                            str(module_id),
                            product_id,
                            category_slug,
                            rag_stores,
                            bool(product_filters),
                        )

                        search_query = (
                            f"[{product_id}] {module_name}\n"
                            f"설명: {module_desc}\n"
                            "위 모듈의 '자가 점검(퀴즈) 문항'을 만들기 위해 필요한 핵심 개념/절차를 문서 근거로 요약해 주세요. "
                            "제품 범위를 벗어난 일반 온보딩/회사 정책/업무 규칙 내용은 제외하세요."
                        )
                        
                        # 검색 수행 (요약 요청)
                        search_result = await search_client.search(
                            query=search_query,
                            store_names=rag_stores,
                            metadata_filters=product_filters,
                            system_instruction=(
                                f"당신은 {product_id} 제품의 '{module_name}' 모듈 퀴즈를 만드는 조교입니다. "
                                "반드시 fileSearch로 찾은 문서의 사실만 요약하세요. "
                                "회사 온보딩/정책/업무 규칙 등 제품 범위를 벗어나면 요약하지 말고 근거 부족으로 답할 수 없다고 말하세요."
                            ),
                        )
                        
                        if search_result and search_result.get("text"):
                            rag_content = search_result["text"]
                            module_content = f"--- RAG Retrieved Content (Supplemented from {', '.join(rag_stores)}) ---\n{rag_content}\n\n" + module_content
                            logger.info(f"Supplemented content with RAG search result ({len(rag_content)} chars).")
                        else:
                            logger.warning("RAG search returned no text.")
                    else:
                        logger.warning("No Gemini store configured for RAG fallback.")
                except Exception as e:
                    logger.warning(f"Failed to supplement content with RAG: {e}")
        
        except Exception as e:
            logger.warning(f"Failed to get module info for {module_id}: {e}, using default topic.")
        
        # AI로 퀴즈 생성 시도
        questions = await _generate_quiz_questions(module_id, module_name, module_desc, module_content)
        
        # 콘텐츠 부족으로 퀴즈를 생성하지 못한 경우 로그 남김
        if not questions:
            logger.info(
                f"Quiz generation skipped for module {module_id} ({module_name}) "
                f"due to insufficient content. Please add curriculum content first."
            )
            
    return questions


# ============================================
# 퀴즈 제출 (자가 점검)
# ============================================

@router.post("/modules/{module_id}/submit", response_model=QuizSubmitResponse)
async def submit_quiz(
    module_id: UUID = Path(..., description="모듈 ID"),
    request: QuizSubmitRequest = None,  # type: ignore
):
    """
    자가 점검 퀴즈 제출 및 채점.
    
    - 통과/불통과 없음, 점수만 참고용으로 제공
    - 틀린 문제에 대한 설명 제공
    """
    if not request:
        raise HTTPException(status_code=400, detail="Request body is required")
    
    if str(module_id) != str(request.module_id):
        raise HTTPException(
            status_code=400,
            detail="module_id in path and body must match"
        )
    
    try:
        repo = get_curriculum_repository()
        result = await repo.submit_quiz(
            session_id=request.session_id,
            module_id=module_id,
            answers=request.answers,
            started_at=request.started_at,
        )
        return result
    except CurriculumRepositoryError as e:
        logger.error(f"Failed to submit quiz: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 진도 관리
# ============================================

@router.get("/progress", response_model=ProgressSummary)
async def get_progress_summary(
    session_id: str = Query(..., alias="sessionId", description="세션 ID"),
    product: str = Query("freshservice", description="제품 ID"),
):
    """
    전체 학습 진도 요약.
    
    - 모듈별 진도 상태
    - 전체 완료율
    """
    try:
        repo = get_curriculum_repository()
        modules = await repo.get_modules_with_progress(
            session_id=session_id,
            product=product,
        )
        
        total = len(modules)
        completed = sum(1 for m in modules if m.status == "completed")
        in_progress = sum(1 for m in modules if m.status == "learning")
        
        # 진행률 계산: 완료된 모듈은 100%, 진행 중인 모듈은 50%로 반영
        # 예: 완료 2개, 진행 중 1개, 전체 10개 = (2*100% + 1*50%) / 10 = 25%
        progress_points = (completed * 100) + (in_progress * 50)
        completion_rate = round(progress_points / total, 1) if total > 0 else 0.0
        
        return ProgressSummary(
            sessionId=session_id,
            totalModules=total,
            completedModules=completed,
            inProgressModules=in_progress,
            completionRate=completion_rate,
            modules=modules,
        )
    except CurriculumRepositoryError as e:
        logger.error(f"Failed to get progress summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modules/{module_id}/progress", response_model=ModuleProgress)
async def get_module_progress(
    module_id: UUID = Path(..., description="모듈 ID"),
    session_id: str = Query(..., alias="sessionId", description="세션 ID"),
):
    """모듈별 상세 진도 조회."""
    try:
        repo = get_curriculum_repository()
        progress = await repo.get_progress(session_id, module_id)
        
        if not progress:
            # 진도 없으면 기본값 반환
            return ModuleProgress(
                sessionId=session_id,
                moduleId=module_id,
                status="not_started",
                learningStartedAt=None,
                learningCompletedAt=None,
                quizScore=None,
                quizAttempts=0,
                learningTimeMinutes=0,
                completedAt=None,
            )
        return progress
    except CurriculumRepositoryError as e:
        logger.error(f"Failed to get module progress: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/modules/{module_id}/progress", response_model=ModuleProgress)
async def update_module_progress(
    module_id: UUID = Path(..., description="모듈 ID"),
    request: UpdateProgressRequest = None,  # type: ignore
):
    """
    모듈 진도 업데이트.
    
    - 학습 완료 표시
    - 상태 변경
    """
    if not request:
        raise HTTPException(status_code=400, detail="Request body is required")
    
    try:
        repo = get_curriculum_repository()
        progress = await repo.update_progress(
            session_id=request.session_id,
            module_id=module_id,
            status=request.status,
            learning_completed=request.learning_completed,
            force=request.force,
        )
        
        if not progress:
            raise HTTPException(status_code=500, detail="Failed to update progress")
        return progress
    except CurriculumRepositoryError as e:
        logger.error(f"Failed to update module progress: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 디버그: 퀴즈 시도 조회
# ============================================

@router.get("/debug/quiz-attempts")
async def debug_quiz_attempts(
    session_id: str = Query(None, alias="sessionId", description="세션 ID"),
    module_id: str = Query(None, alias="moduleId", description="모듈 ID"),
):
    """디버그: quiz_attempts 테이블 조회."""
    try:
        repo = get_curriculum_repository()
        query = repo.client.table("quiz_attempts").select("*")
        
        if session_id:
            query = query.eq("session_id", session_id)
        if module_id:
            query = query.eq("module_id", module_id)
        
        response = query.order("created_at", desc=True).limit(10).execute()
        return {"attempts": response.data, "count": len(response.data)}
    except Exception as e:
        logger.error(f"Debug quiz attempts error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/debug/module-progress-schema")
async def debug_module_progress_schema():
    """디버그: module_progress 테이블 스키마 조회."""
    try:
        repo = get_curriculum_repository()
        # Get first row to see columns
        response = repo.client.table("module_progress").select("*").limit(1).execute()
        if response.data:
            columns = list(response.data[0].keys())
        else:
            columns = ["No data in table"]
        return {"columns": columns, "sample_data": response.data}
    except Exception as e:
        logger.error(f"Debug module progress schema error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
