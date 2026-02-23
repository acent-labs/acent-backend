#!/bin/bash

# Claude Code Infrastructure - 모듈 활성화 설정
# 기존 저장소에 인프라 복사 후 실행

set -e

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "⚙️ Claude Code 모듈 설정"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

SKILL_RULES=".claude/skills/skill-rules.json"

if [ ! -f "$SKILL_RULES" ]; then
    echo "❌ $SKILL_RULES 파일을 찾을 수 없습니다"
    echo "   먼저 .claude/ 폴더를 복사하세요"
    exit 1
fi

echo "📦 사용할 기술 스택을 선택하세요"
echo ""

# 현재 상태 표시
echo "현재 설정:"
grep -E '"(python|typescript|react|nextjs)".*enabled' "$SKILL_RULES" | head -4
echo ""

# 기술 스택 선택
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

# JSON 업데이트 (jq 사용 가능하면 jq, 아니면 sed)
if command -v jq &> /dev/null; then
    # jq로 정확한 JSON 수정
    TMP_FILE=$(mktemp)
    
    jq --arg py "$ENABLE_PYTHON" \
       --arg ts "$ENABLE_TYPESCRIPT" \
       --arg react "$ENABLE_REACT" \
       --arg next "$ENABLE_NEXTJS" \
       '.modules.python.enabled = ($py == "true") |
        .modules.typescript.enabled = ($ts == "true") |
        .modules.react.enabled = ($react == "true") |
        .modules.nextjs.enabled = ($next == "true")' \
       "$SKILL_RULES" > "$TMP_FILE"
    
    mv "$TMP_FILE" "$SKILL_RULES"
else
    # sed 사용 (macOS/Linux 호환)
    if [[ "$OSTYPE" == "darwin"* ]]; then
        SED_CMD="sed -i ''"
    else
        SED_CMD="sed -i"
    fi
    
    # 모든 모듈 먼저 비활성화
    $SED_CMD 's/"enabled": true/"enabled": false/g' "$SKILL_RULES"
    
    # core는 항상 활성화
    $SED_CMD '/"core":/,/enabled/s/"enabled": false/"enabled": true/' "$SKILL_RULES"
    
    # 선택한 모듈 활성화
    [ "$ENABLE_PYTHON" = true ] && $SED_CMD '/"python":/,/enabled/s/"enabled": false/"enabled": true/' "$SKILL_RULES"
    [ "$ENABLE_TYPESCRIPT" = true ] && $SED_CMD '/"typescript":/,/enabled/s/"enabled": false/"enabled": true/' "$SKILL_RULES"
    [ "$ENABLE_REACT" = true ] && $SED_CMD '/"react":/,/enabled/s/"enabled": false/"enabled": true/' "$SKILL_RULES"
    [ "$ENABLE_NEXTJS" = true ] && $SED_CMD '/"nextjs":/,/enabled/s/"enabled": false/"enabled": true/' "$SKILL_RULES"
fi

echo ""
echo "✅ 설정 완료!"
echo ""
echo "활성화된 모듈:"
[ "$ENABLE_PYTHON" = true ] && echo "  ✅ Python/FastAPI"
[ "$ENABLE_TYPESCRIPT" = true ] && echo "  ✅ TypeScript/Express"
[ "$ENABLE_REACT" = true ] && echo "  ✅ React"
[ "$ENABLE_NEXTJS" = true ] && echo "  ✅ Next.js"
echo "  ✅ Core (항상 활성)"
echo ""

# Python 린터 설치 안내
if [ "$ENABLE_PYTHON" = true ]; then
    read -p "🐍 Python 린터(ruff) 설치할까요? (y/n): " choice
    if [[ "$choice" =~ ^[Yy]$ ]]; then
        pip install ruff || pip3 install ruff
        echo "  ✅ ruff 설치 완료"
    fi
fi

echo ""
echo "이제 Claude Code/Cursor에서 프로젝트를 열고 사용하세요!"
echo ""
