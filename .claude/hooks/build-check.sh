#!/bin/bash

# Universal Build Check Hook
# Auto-detects project type (Python/TypeScript/Both) and runs appropriate linters
# Triggered on Stop event to catch errors before session ends

CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
HOOK_INPUT=$(cat)
SESSION_ID="${session_id:-default}"
CACHE_DIR="$HOME/.claude/build-cache/$SESSION_ID"

mkdir -p "$CACHE_DIR"

# Detect project type based on config files
detect_project_type() {
    local types=""
    
    # Check for Python
    if [ -f "$CLAUDE_PROJECT_DIR/pyproject.toml" ] || \
       [ -f "$CLAUDE_PROJECT_DIR/requirements.txt" ] || \
       [ -f "$CLAUDE_PROJECT_DIR/setup.py" ]; then
        types="python"
    fi
    
    # Check for TypeScript/JavaScript
    if [ -f "$CLAUDE_PROJECT_DIR/tsconfig.json" ] || \
       [ -f "$CLAUDE_PROJECT_DIR/package.json" ]; then
        [ -n "$types" ] && types="$types " 
        types="${types}typescript"
    fi
    
    echo "$types"
}

# Run Python linting (ruff preferred, fallback to flake8)
run_python_check() {
    echo "🐍 Python 검사 중..." >&2
    
    local errors=""
    local has_errors=0
    
    # Try ruff first (fast, modern)
    if command -v ruff &> /dev/null; then
        echo "  ruff check..." >&2
        errors=$(ruff check "$CLAUDE_PROJECT_DIR" 2>&1)
        if [ $? -ne 0 ]; then
            has_errors=1
        fi
    # Fallback to flake8
    elif command -v flake8 &> /dev/null; then
        echo "  flake8..." >&2
        errors=$(flake8 "$CLAUDE_PROJECT_DIR" 2>&1)
        if [ $? -ne 0 ]; then
            has_errors=1
        fi
    else
        echo "  ⚠️ ruff 미설치 - 자동 설치 중..." >&2
        if command -v pip &> /dev/null; then
            pip install ruff --quiet && echo "  ✅ ruff 설치 완료" >&2
            errors=$(ruff check "$CLAUDE_PROJECT_DIR" 2>&1)
            if [ $? -ne 0 ]; then
                has_errors=1
            fi
        elif command -v pip3 &> /dev/null; then
            pip3 install ruff --quiet && echo "  ✅ ruff 설치 완료" >&2
            errors=$(ruff check "$CLAUDE_PROJECT_DIR" 2>&1)
            if [ $? -ne 0 ]; then
                has_errors=1
            fi
        else
            echo "  ❌ pip 없음 - 수동 설치 필요: pip install ruff" >&2
            return 0
        fi
    fi
    
    # Type checking with mypy (optional)
    if command -v mypy &> /dev/null && [ -f "$CLAUDE_PROJECT_DIR/pyproject.toml" ]; then
        echo "  mypy 타입 검사..." >&2
        local mypy_errors=$(mypy "$CLAUDE_PROJECT_DIR" --ignore-missing-imports 2>&1)
        if [ $? -ne 0 ]; then
            errors="${errors}\n${mypy_errors}"
            has_errors=1
        fi
    fi
    
    if [ $has_errors -eq 1 ]; then
        echo "$errors" > "$CACHE_DIR/python-errors.txt"
        echo "  ❌ Python 오류 발견" >&2
        return 1
    fi
    
    echo "  ✅ Python OK" >&2
    return 0
}

# Run TypeScript check
run_typescript_check() {
    echo "📘 TypeScript 검사 중..." >&2
    
    cd "$CLAUDE_PROJECT_DIR" || return 1
    
    # Determine the right tsconfig
    local tsc_cmd="npx tsc --noEmit"
    if [ -f "tsconfig.app.json" ]; then
        tsc_cmd="npx tsc --project tsconfig.app.json --noEmit"
    elif [ -f "tsconfig.build.json" ]; then
        tsc_cmd="npx tsc --project tsconfig.build.json --noEmit"
    fi
    
    echo "  $tsc_cmd..." >&2
    local errors=$(eval "$tsc_cmd" 2>&1)
    
    if [ $? -ne 0 ] || echo "$errors" | grep -q "error TS"; then
        echo "$errors" > "$CACHE_DIR/typescript-errors.txt"
        echo "  ❌ TypeScript 오류 발견" >&2
        return 1
    fi
    
    echo "  ✅ TypeScript OK" >&2
    return 0
}

# Main execution
PROJECT_TYPES=$(detect_project_type)

if [ -z "$PROJECT_TYPES" ]; then
    echo "ℹ️ 프로젝트 타입 감지 불가 - 검사 건너뜀" >&2
    exit 0
fi

echo "" >&2
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2
echo "⚡ 빌드 검사: $PROJECT_TYPES" >&2
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2

TOTAL_ERRORS=0

for ptype in $PROJECT_TYPES; do
    case "$ptype" in
        python)
            run_python_check || TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
            ;;
        typescript)
            run_typescript_check || TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
            ;;
    esac
done

if [ $TOTAL_ERRORS -gt 0 ]; then
    echo "" >&2
    echo "🚨 $TOTAL_ERRORS 개 프로젝트에서 오류 발견" >&2
    echo "👉 auto-error-resolver 에이전트로 수정하세요" >&2
    echo "" >&2
    exit 1
fi

echo "" >&2
echo "✅ 모든 검사 통과" >&2
echo "" >&2

# Cleanup old cache
find "$HOME/.claude/build-cache" -maxdepth 1 -type d -mtime +7 -exec rm -rf {} \; 2>/dev/null || true

exit 0
