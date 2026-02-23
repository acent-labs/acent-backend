#!/bin/bash

# Claude Code Infrastructure Template Setup
# 새 프로젝트에서 이 템플릿을 사용할 때 실행하세요

set -e

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 Claude Code Infrastructure 설정"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 프로젝트 이름 입력
read -p "📁 프로젝트 이름: " PROJECT_NAME
if [ -z "$PROJECT_NAME" ]; then
    echo "❌ 프로젝트 이름을 입력하세요"
    exit 1
fi

# 기술 스택 선택
echo ""
echo "📦 사용할 기술 스택을 선택하세요 (복수 선택 가능)"
echo ""

ENABLE_PYTHON=false
ENABLE_TYPESCRIPT=false
ENABLE_REACT=false
ENABLE_NEXTJS=false

read -p "  Python/FastAPI 사용? (y/n): " choice
[[ "$choice" =~ ^[Yy]$ ]] && ENABLE_PYTHON=true

read -p "  TypeScript/Express 사용? (y/n): " choice
[[ "$choice" =~ ^[Yy]$ ]] && ENABLE_TYPESCRIPT=true

read -p "  React 사용? (y/n): " choice
[[ "$choice" =~ ^[Yy]$ ]] && ENABLE_REACT=true

read -p "  Next.js 사용? (y/n): " choice
[[ "$choice" =~ ^[Yy]$ ]] && ENABLE_NEXTJS=true

# skill-rules.json 업데이트
SKILL_RULES=".claude/skills/skill-rules.json"

if [ -f "$SKILL_RULES" ]; then
    echo ""
    echo "⚙️ skill-rules.json 업데이트 중..."
    
    # macOS와 Linux 호환 sed
    if [[ "$OSTYPE" == "darwin"* ]]; then
        SED_INPLACE="sed -i ''"
    else
        SED_INPLACE="sed -i"
    fi
    
    # Python 모듈
    if [ "$ENABLE_PYTHON" = true ]; then
        $SED_INPLACE 's/"python": {[^}]*"enabled": false/"python": { "enabled": true/' "$SKILL_RULES"
        echo "  ✅ Python 모듈 활성화"
    fi
    
    # TypeScript 모듈
    if [ "$ENABLE_TYPESCRIPT" = true ]; then
        $SED_INPLACE 's/"typescript": {[^}]*"enabled": false/"typescript": { "enabled": true/' "$SKILL_RULES"
        echo "  ✅ TypeScript 모듈 활성화"
    fi
    
    # React 모듈
    if [ "$ENABLE_REACT" = true ]; then
        $SED_INPLACE 's/"react": {[^}]*"enabled": false/"react": { "enabled": true/' "$SKILL_RULES"
        echo "  ✅ React 모듈 활성화"
    fi
    
    # Next.js 모듈
    if [ "$ENABLE_NEXTJS" = true ]; then
        $SED_INPLACE 's/"nextjs": {[^}]*"enabled": false/"nextjs": { "enabled": true/' "$SKILL_RULES"
        echo "  ✅ Next.js 모듈 활성화"
    fi
fi

# Python 린터 설치 안내
if [ "$ENABLE_PYTHON" = true ]; then
    echo ""
    read -p "🐍 Python 린터(ruff) 설치할까요? (y/n): " choice
    if [[ "$choice" =~ ^[Yy]$ ]]; then
        pip install ruff || pip3 install ruff
        echo "  ✅ ruff 설치 완료"
    fi
fi

# dev 폴더 초기화
echo ""
echo "📝 개발 문서 폴더 초기화..."
mkdir -p dev
if [ ! -f "dev/plan.md" ]; then
    cp dev/templates/plan-template.md dev/plan.md 2>/dev/null || true
fi
if [ ! -f "dev/context.md" ]; then
    cp dev/templates/context-template.md dev/context.md 2>/dev/null || true
fi
if [ ! -f "dev/tasks.md" ]; then
    cp dev/templates/tasks-template.md dev/tasks.md 2>/dev/null || true
fi
echo "  ✅ dev/plan.md, context.md, tasks.md 생성"

# 템플릿 파일 정리
echo ""
read -p "🧹 템플릿 파일 정리할까요? (setup.sh, 샘플 파일 삭제) (y/n): " choice
if [[ "$choice" =~ ^[Yy]$ ]]; then
    rm -f setup.sh
    rm -rf data/  # 샘플 데이터 폴더
    echo "  ✅ 템플릿 파일 정리 완료"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 설정 완료!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "다음 단계:"
echo "  1. Claude Code/Cursor에서 프로젝트 열기"
echo "  2. AI에게 '/dev-docs' 로 개발 문서 초기화"
echo "  3. 코딩 시작!"
echo ""
