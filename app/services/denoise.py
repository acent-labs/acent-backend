"""
NoCut Denoise Engine

Ported from nexus-ai. Removes noise from conversations WITHOUT cutting/truncating.
Uses deterministic rules to strip:
- Auto-reply messages
- Email quoted blocks
- Signatures and footers
- Debug/meta headers
- High-entropy tokens (hashes, tracking IDs)
- Duplicate messages

Preserves:
- Agent messages with resolution/action signals
- All meaningful content regardless of length
"""
from __future__ import annotations

import functools
import html
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Regex Patterns
# =============================================================================

_AUTO_REPLY_PATTERNS = [
    re.compile(r"this is an automated (message|response)", re.IGNORECASE),
    re.compile(r"do not reply", re.IGNORECASE),
    re.compile(r"\uc218\uc2e0 \uc804\uc6a9", re.IGNORECASE),  # 수신 전용
    re.compile(r"\uc790\ub3d9(\s*)\uc751\ub2f5", re.IGNORECASE),  # 자동응답
]

_QUOTE_BLOCK_PATTERNS = [
    re.compile(r"^>+", re.MULTILINE),
    re.compile(
        r"^-{2,}\s*Original Message\s*-{2,}$", re.IGNORECASE | re.MULTILINE
    ),
    re.compile(r"^On .+ wrote:$", re.IGNORECASE | re.MULTILINE),
]

_SIGNATURE_PATTERNS = [
    re.compile(r"^Sent from my", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^--\s*$", re.MULTILINE),
]

_DEBUG_HEADER_PATTERNS = [
    re.compile(r"^===\s*[^=]+\s*===\s*$", re.MULTILINE),
    re.compile(r"^FULL\s+PROMPT\s*:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^PROMPT\s+LENGTH\s*:\s*\d+\s*$", re.IGNORECASE | re.MULTILINE),
]

_FOOTER_PATTERNS = [
    re.compile(
        r"\ubb38\uc758\s*\ub0b4\uc6a9\s*\(.*?\)\s*:\s*.*$",
        re.IGNORECASE | re.MULTILINE,
    ),  # 문의 내용(...)
    re.compile(r"powered\s+by\s+freshdesk", re.IGNORECASE),
    re.compile(r"do\s+not\s+share\s+this\s+email", re.IGNORECASE),
]

_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

_HIGH_ENTROPY_COMBINED = re.compile(
    r"\b[0-9a-f]{32,}\b|\b[A-Za-z0-9_-]{40,}\b", re.IGNORECASE
)

# Keep signal: lines with these words are preserved (resolution/action content)
_KEEP_SIGNAL = re.compile(
    r"(\ud574\uacb0|\uc870\uce58|\uc6d0\uc778|\uc7ac\ud604|\uc99d\uc0c1"
    r"|\uacb0\ub860|\ubc30\ud3ec|\ub864\uc544\uc6c3|\uc218\uc815|\ud655\uc778"
    r"|\uc5d0\ub7ec|\uc624\ub958|\uc81c\ud55c"
    r"|limit|patch|workaround|fixed|resolved|solution)",
    re.IGNORECASE,
)
# Korean: 해결|조치|원인|재현|증상|결론|배포|롤아웃|수정|확인|에러|오류|제한


# =============================================================================
# Data Types
# =============================================================================


@dataclass
class DenoiseResult:
    """Result of denoising a conversation."""

    conversation: List[Dict[str, Any]]
    kept_original_indices: List[int]


# =============================================================================
# Internal Helpers
# =============================================================================


@functools.lru_cache(maxsize=512)
def _normalize_for_dedupe(text: str) -> str:
    """Normalize text for deduplication (focuses on similarity, not meaning)."""
    if not text:
        return ""
    t = html.unescape(text)
    t = _URL_PATTERN.sub("", t)
    t = _HIGH_ENTROPY_COMBINED.sub("", t)
    t = re.sub(r"[\t\r ]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip().lower()


def _strip_noise(text: str) -> str:
    """Strip noise from a single message text while preserving meaningful content."""
    t = html.unescape(text or "")

    # Remove debug/meta headers
    for p in _DEBUG_HEADER_PATTERNS:
        t = p.sub("", t)

    # Line-by-line processing
    lines = t.splitlines()
    kept: List[str] = []
    in_signature = False

    for line in lines:
        raw_line = line
        line = line.rstrip("\r")

        # Footer removal (except keep-signal lines)
        if not _KEEP_SIGNAL.search(line):
            if any(p.search(line) for p in _FOOTER_PATTERNS):
                continue

        # Signature detection
        if any(p.search(line) for p in _SIGNATURE_PATTERNS):
            in_signature = True
            continue

        if in_signature and not _KEEP_SIGNAL.search(line):
            continue

        # Quoted line removal
        is_quoted = False
        if any(p.search(line) for p in _QUOTE_BLOCK_PATTERNS):
            is_quoted = True
        if line.lstrip().startswith(">"):
            is_quoted = True

        if is_quoted and not _KEEP_SIGNAL.search(line):
            continue

        kept.append(raw_line)

    t = "\n".join(kept)

    # URL and high-entropy token removal
    t = _URL_PATTERN.sub("", t)
    if not _KEEP_SIGNAL.search(t):
        t = _HIGH_ENTROPY_COMBINED.sub("", t)

    # Whitespace cleanup
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[\t\r ]+", " ", t)
    return t.strip()


def _get_text_field(msg: Dict[str, Any]) -> str:
    """Extract text from a conversation message (supports multiple formats)."""
    # agent-platform format
    if "body_text" in msg:
        return (msg.get("body_text") or "").strip()
    # nexus-ai / normalized format
    if "text" in msg:
        return (msg.get("text") or "").strip()
    # Freshdesk raw format
    if "body" in msg:
        return (msg.get("body") or "").strip()
    return ""


def _get_author_role(msg: Dict[str, Any]) -> str:
    """Extract author role from a conversation message."""
    if "author_role" in msg:
        return msg["author_role"]
    # agent-platform uses incoming/private flags
    if "incoming" in msg:
        if msg.get("private"):
            return "agent"  # private note
        return "customer" if msg.get("incoming") else "agent"
    return "unknown"


def _get_channel(msg: Dict[str, Any]) -> str:
    """Extract channel from a conversation message."""
    if "channel" in msg:
        return msg["channel"]
    if msg.get("private"):
        return "note"
    return "email"


# =============================================================================
# Public API
# =============================================================================


def denoise_conversations(
    conversations: List[Dict[str, Any]],
) -> DenoiseResult:
    """
    Denoise a conversation list using NoCut strategy.

    - Never truncates/cuts messages
    - Removes noise using deterministic rules
    - Preserves agent messages with resolution/action signals
    - Deduplicates based on normalized content

    Args:
        conversations: List of conversation message dicts (any format)

    Returns:
        DenoiseResult with cleaned conversations and original index mapping
    """
    t0 = time.perf_counter()

    cleaned: List[Dict[str, Any]] = []
    kept_idx: List[int] = []
    seen: set = set()

    removed_stats = {
        "empty": 0,
        "auto_reply": 0,
        "noise_only": 0,
        "duplicate": 0,
    }

    for idx, msg in enumerate(conversations):
        raw = _get_text_field(msg)

        # Early exit: empty messages
        if not raw:
            removed_stats["empty"] += 1
            continue

        # Early exit: auto-reply (before expensive noise stripping)
        has_keep_signal = _KEEP_SIGNAL.search(raw)
        if not has_keep_signal:
            if any(p.search(raw) for p in _AUTO_REPLY_PATTERNS):
                removed_stats["auto_reply"] += 1
                continue

        # Strip noise
        text = _strip_noise(raw)
        if not text:
            removed_stats["noise_only"] += 1
            continue

        # Deduplication with cached normalization
        author_role = _get_author_role(msg)
        channel = _get_channel(msg)
        norm = _normalize_for_dedupe(text)
        key = (author_role, channel, norm)

        if key in seen:
            removed_stats["duplicate"] += 1
            continue

        seen.add(key)

        # Build cleaned message (preserve original structure, replace text)
        cleaned_msg = dict(msg)
        if "body_text" in msg:
            cleaned_msg["body_text"] = text
        elif "text" in msg:
            cleaned_msg["text"] = text
        elif "body" in msg:
            cleaned_msg["body"] = text
        else:
            cleaned_msg["body_text"] = text

        cleaned.append(cleaned_msg)
        kept_idx.append(idx)

    duration_ms = int((time.perf_counter() - t0) * 1000)
    total_removed = sum(removed_stats.values())

    if total_removed > 0:
        logger.info(
            "Denoise removed %d/%d messages: %s",
            total_removed,
            len(conversations),
            removed_stats,
        )
    logger.info(
        "[METRIC] denoise_duration_ms=%d input_count=%d output_count=%d",
        duration_ms,
        len(conversations),
        len(cleaned),
    )

    return DenoiseResult(conversation=cleaned, kept_original_indices=kept_idx)
