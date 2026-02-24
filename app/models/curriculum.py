"""커리큘럼 및 퀴즈 관련 데이터 모델."""

from datetime import datetime
from typing import List, Optional, Dict
from uuid import UUID

from pydantic import BaseModel, Field


# ============================================
# Curriculum Module (학습 모듈)
# ============================================

class CurriculumModule(BaseModel):
    """커리큘럼 모듈 모델."""

    id: UUID
    target_product_id: str = Field(..., alias="targetProductId")
    target_product_type: str = Field("module", alias="targetProductType")
    name_ko: str = Field(..., alias="nameKo")
    name_en: Optional[str] = Field(None, alias="nameEn")
    slug: str
    description: Optional[str] = None
    icon: Optional[str] = None
    estimated_minutes: int = Field(30, alias="estimatedMinutes")
    display_order: int = Field(0, alias="displayOrder")
    learning_objectives: Optional[List[str]] = Field(None, alias="learningObjectives")
    content_strategy: str = Field("hybrid", alias="contentStrategy")
    is_active: bool = Field(True, alias="isActive")
    kb_category_slug: Optional[str] = Field(None, alias="kbCategorySlug")
    created_at: Optional[datetime] = Field(None, alias="createdAt")

    class Config:
        populate_by_name = True
        from_attributes = True


class CurriculumModuleResponse(BaseModel):
    """커리큘럼 모듈 응답 (진도 포함)."""

    id: UUID
    target_product_id: str = Field(..., alias="targetProductId")
    target_product_type: str = Field("module", alias="targetProductType")
    name_ko: str = Field(..., alias="nameKo")
    name_en: Optional[str] = Field(None, alias="nameEn")
    slug: str
    description: Optional[str] = None
    icon: Optional[str] = None
    estimated_minutes: int = Field(alias="estimatedMinutes")
    display_order: int = Field(alias="displayOrder")
    
    # 진도 정보 (단순화: unlock 제거, 기초/심화 → 단일 퀴즈)
    status: str = "not_started"  # not_started, learning, completed
    quiz_score: Optional[int] = Field(None, alias="quizScore")
    quiz_attempts: int = Field(0, alias="quizAttempts")
    learning_time_minutes: int = Field(0, alias="learningTimeMinutes")

    class Config:
        populate_by_name = True


# ============================================
# Quiz Question (퀴즈 문제)
# ============================================

class QuizChoice(BaseModel):
    """퀴즈 선택지."""

    id: str
    text: str


class QuizQuestion(BaseModel):
    """퀴즈 문제 모델 (자가 점검용)."""

    id: UUID
    module_id: UUID = Field(..., alias="moduleId")
    question_order: int = Field(0, alias="questionOrder")
    question: str
    context: Optional[str] = None
    choices: List[QuizChoice]
    # 정답은 클라이언트에 보내지 않음
    related_doc_url: Optional[str] = Field(None, alias="relatedDocUrl")

    class Config:
        populate_by_name = True
        from_attributes = True


class QuizQuestionWithAnswer(QuizQuestion):
    """퀴즈 문제 (정답 포함 - 내부용)."""

    correct_choice_id: str = Field(..., alias="correctChoiceId")
    explanation: Optional[str] = None
    learning_point: Optional[str] = Field(None, alias="learningPoint")


# ============================================
# Quiz Submit (퀴즈 제출)
# ============================================

class QuizAnswer(BaseModel):
    """퀴즈 답변."""

    question_id: UUID = Field(..., alias="questionId")
    choice_id: str = Field(..., alias="choiceId")


class QuizSubmitRequest(BaseModel):
    """퀴즈 제출 요청 (자가 점검)."""

    session_id: str = Field(..., alias="sessionId")
    module_id: UUID = Field(..., alias="moduleId")
    answers: List[QuizAnswer]
    started_at: Optional[datetime] = Field(None, alias="startedAt")

    class Config:
        populate_by_name = True


class QuizAnswerResult(BaseModel):
    """개별 답변 결과."""

    question_id: UUID = Field(..., alias="questionId")
    choice_id: str = Field(..., alias="choiceId")
    is_correct: bool = Field(..., alias="isCorrect")
    correct_choice_id: str = Field(..., alias="correctChoiceId")
    explanation: Optional[str] = None


class QuizSubmitResponse(BaseModel):
    """퀴즈 제출 응답 (자가 점검 - 통과/불통과 없음)."""

    module_id: UUID = Field(..., alias="moduleId")
    score: int  # 점수 (참고용)
    total_questions: int = Field(..., alias="totalQuestions")
    correct_count: int = Field(..., alias="correctCount")
    answers: List[QuizAnswerResult]
    duration_seconds: Optional[int] = Field(None, alias="durationSeconds")
    # AI 피드백 (개선 포인트 제안)
    feedback: Optional[str] = None

    class Config:
        populate_by_name = True


# ============================================
# Module Progress (모듈 진도)
# ============================================

class ModuleProgress(BaseModel):
    """모듈 진도 모델 (단순화)."""

    id: Optional[UUID] = None
    session_id: str = Field(..., alias="sessionId")
    module_id: UUID = Field(..., alias="moduleId")
    status: str = "not_started"  # not_started, learning, completed
    
    learning_started_at: Optional[datetime] = Field(None, alias="learningStartedAt")
    learning_completed_at: Optional[datetime] = Field(None, alias="learningCompletedAt")
    
    # 자가 점검 퀴즈 (통과/불통과 없음, 점수만 기록)
    # DB 컬럼: basic_quiz_score, basic_quiz_attempts
    quiz_score: Optional[int] = Field(None, alias="quizScore")
    quiz_attempts: int = Field(0, alias="quizAttempts")
    
    total_time_seconds: int = Field(0, alias="totalTimeSeconds")
    completed_at: Optional[datetime] = Field(None, alias="completedAt")

    class Config:
        populate_by_name = True
        from_attributes = True


class UpdateProgressRequest(BaseModel):
    """진도 업데이트 요청."""

    session_id: str = Field(..., alias="sessionId")
    status: Optional[str] = None  # learning, completed
    learning_completed: bool = Field(False, alias="learningCompleted")
    force: bool = Field(False, description="완료된 상태도 덮어쓰기 허용 (재시작 시 사용)")

    class Config:
        populate_by_name = True


class ProgressSummary(BaseModel):
    """전체 진도 요약."""

    session_id: str = Field(..., alias="sessionId")
    total_modules: int = Field(..., alias="totalModules")
    completed_modules: int = Field(..., alias="completedModules")
    in_progress_modules: int = Field(..., alias="inProgressModules")
    completion_rate: float = Field(..., alias="completionRate")
    modules: List[CurriculumModuleResponse]

    class Config:
        populate_by_name = True


# ============================================
# Module Content (학습 콘텐츠)
# ============================================

class ModuleContent(BaseModel):
    """모듈 학습 콘텐츠 (정적 콘텐츠)."""

    id: UUID
    module_id: UUID = Field(..., alias="moduleId")
    section_type: str = Field(..., alias="sectionType")  # overview, core_concepts, features, practice, faq
    level: str = "basic"  # basic, intermediate, advanced
    title_ko: str = Field(..., alias="titleKo")
    title_en: Optional[str] = Field(None, alias="titleEn")
    content_md: str = Field(..., alias="contentMd")
    display_order: int = Field(0, alias="displayOrder")
    estimated_minutes: int = Field(5, alias="estimatedMinutes")
    is_active: bool = Field(True, alias="isActive")

    class Config:
        populate_by_name = True
        from_attributes = True


class ModuleContentResponse(BaseModel):
    """모듈별 콘텐츠 응답 (레벨별 섹션 포함)."""

    module_id: UUID = Field(..., alias="moduleId")
    module_name: str = Field(..., alias="moduleName")
    levels: List[str]  # ["basic", "intermediate", "advanced"]
    sections: Dict[str, List[ModuleContent]]  # level -> contents

    class Config:
        populate_by_name = True
