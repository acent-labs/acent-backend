"""
Guardrails Module

Ported from nexus-ai. Applies post-LLM validation:
1. Evidence enforcement: ensures all evidence fields are integer arrays
2. Forbidden phrase filter: blocks definitive/assertive language
3. Language mismatch detection: logs when detected != response language

Philosophy: "AI should not assert causes. It provides evidence and lets agents decide."
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Forbidden Phrases (Korean assertive/definitive expressions)
# =============================================================================

FORBIDDEN_PHRASES = [
    "\ubb34\uc870\uac74",      # 무조건 (unconditionally)
    "\uc6d0\uc778\uc785\ub2c8\ub2e4",  # 원인입니다 (is the cause)
    "\ud655\uc2e4\ud788",      # 확실히 (certainly)
    "\ubc18\ub4dc\uc2dc",      # 반드시 (must)
    "\ubb38\uc81c \uc5c6\uc2b5\ub2c8\ub2e4",  # 문제 없습니다 (no problem)
    "\ub54c\ubb38\uc785\ub2c8\ub2e4",  # 때문입니다 (because - definitive)
]


# =============================================================================
# Evidence Validation
# =============================================================================


def normalize_evidence_items(
    items: Any, label: str = "item"
) -> Tuple[Any, List[str]]:
    """
    Ensure evidence fields in items are integer arrays.

    Returns:
        Tuple of (fixed items, list of violation messages)
    """
    violations: List[str] = []

    if not isinstance(items, list):
        return items, violations

    for item in items:
        if not isinstance(item, dict):
            continue

        evidence = item.get("evidence")
        if evidence is None:
            continue

        if not isinstance(evidence, list):
            item["evidence"] = []
            violations.append(
                f"{label} '{item.get('title', 'Unknown')}': "
                f"evidence was not a list, reset to []"
            )
            continue

        fixed = [ev for ev in evidence if isinstance(ev, int)]
        removed = [ev for ev in evidence if not isinstance(ev, int)]

        if removed:
            violations.append(
                f"{label} '{item.get('title', 'Unknown')}': "
                f"removed non-integer evidence values {removed}"
            )

        item["evidence"] = fixed

    return items, violations


def fix_evidence_fields(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Fix evidence fields in root_causes and recommended_actions / resolution."""
    all_violations: List[str] = []

    # nexus-ai style fields
    if "root_causes" in data:
        data["root_causes"], v = normalize_evidence_items(
            data.get("root_causes", []), "root_cause"
        )
        all_violations.extend(v)

    if "recommended_actions" in data:
        data["recommended_actions"], v = normalize_evidence_items(
            data.get("recommended_actions", []), "action"
        )
        all_violations.extend(v)

    # agent-platform style: evidence is a top-level array of objects
    if "evidence" in data and isinstance(data["evidence"], list):
        for ev in data["evidence"]:
            if isinstance(ev, dict):
                score = ev.get("relevance_score")
                if score is not None and not isinstance(score, (int, float)):
                    try:
                        ev["relevance_score"] = float(score)
                    except (ValueError, TypeError):
                        ev["relevance_score"] = 0.0
                        all_violations.append(
                            f"evidence: invalid relevance_score '{score}', reset to 0.0"
                        )

    return data, all_violations


# =============================================================================
# Forbidden Phrase Check
# =============================================================================


def contains_forbidden_phrases(text: str) -> bool:
    """Check if text contains any forbidden assertive phrases."""
    return any(phrase in text for phrase in FORBIDDEN_PHRASES)


def find_forbidden_phrases(text: str) -> List[str]:
    """Return list of forbidden phrases found in text."""
    return [phrase for phrase in FORBIDDEN_PHRASES if phrase in text]


# =============================================================================
# Language Mismatch Detection
# =============================================================================


def check_language_mismatch(data: Dict[str, Any]) -> List[str]:
    """Check for language mismatch between detected and response language."""
    violations: List[str] = []

    detected = data.get("detected_language", "")
    response = data.get("response_language", "")

    if detected and response and detected != response:
        violations.append(
            f"Language mismatch: detected={detected}, response={response}"
        )
        logger.warning(
            "Language mismatch: detected=%s, response=%s", detected, response
        )

    return violations


# =============================================================================
# Unified Guardrails
# =============================================================================


def apply_guardrails(
    analysis: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Apply all guardrails to an LLM analysis result.

    Steps:
    1. Fix evidence fields (ensure integer arrays)
    2. Check for forbidden phrases
    3. Check language mismatch

    Args:
        analysis: Parsed LLM output dict

    Returns:
        Tuple of (fixed analysis dict, list of violation messages)
    """
    all_violations: List[str] = []

    # Step 1: Evidence validation
    analysis, evidence_violations = fix_evidence_fields(analysis)
    all_violations.extend(evidence_violations)

    # Step 2: Forbidden phrase check
    text_dump = json.dumps(analysis, ensure_ascii=False)
    found = find_forbidden_phrases(text_dump)
    if found:
        all_violations.append(
            f"Forbidden phrases found: {found}"
        )
        logger.warning("Guardrail: forbidden phrases detected: %s", found)

    # Step 3: Language mismatch
    lang_violations = check_language_mismatch(analysis)
    all_violations.extend(lang_violations)

    if all_violations:
        logger.info(
            "[guardrails] Applied %d fixes: %s",
            len(all_violations),
            all_violations,
        )

    return analysis, all_violations
