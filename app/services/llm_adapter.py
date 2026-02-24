"""
LLM Adapter Service

Provides a unified interface for different LLM providers (DeepSeek, OpenAI).
Handles model selection and API communication.
"""
import logging
from typing import Optional, Dict, Any, List
import json

from app.core.config import get_settings
from app.services.llm_gateway import LLMRequest, get_llm_gateway

logger = logging.getLogger(__name__)


def _build_nested_leaf_paths(choices: Any) -> List[List[str]]:
    """Build all valid paths from Freshdesk nested_field 'choices'.

    Freshdesk nested_field choices can be a mixed structure:
    - dict: {L1: {L2: [L3, ...]}} or {L1: {}}
    - list: [L3, ...] or [] (meaning leaf ends at current level)

    Returns a list of paths (each path is list[str]), where the leaf is the last element.
    """

    paths: List[List[str]] = []

    def walk(node: Any, prefix: List[str]) -> None:
        if node is None:
            return

        if isinstance(node, dict):
            if not node:
                # Leaf ends at current prefix (e.g., L1 leaf)
                if prefix:
                    paths.append(prefix)
                return
            for k, v in node.items():
                walk(v, prefix + [str(k)])
            return

        if isinstance(node, list):
            if len(node) == 0:
                # Leaf ends at current prefix (e.g., L2 leaf)
                if prefix:
                    paths.append(prefix)
                return
            for item in node:
                paths.append(prefix + [str(item)])
            return

        # Fallback: treat scalar as leaf
        paths.append(prefix + [str(node)])

    walk(choices, [])
    # Remove duplicates while preserving order
    seen = set()
    uniq: List[List[str]] = []
    for p in paths:
        key = tuple(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def _pick_best_nested_path(subject: str, description: str, paths: List[List[str]]) -> Optional[List[str]]:
    """Pick the best matching nested path using simple rules + substring match.

    This exists to prevent obvious misclassification like Freshdesk tickets -> Splashtop.
    """
    text = f"{subject}\n{description}".lower()
    if not text.strip() or not paths:
        return None

    # Helper: find a path containing a specific leaf or segment.
    def find_path_containing(token: str) -> Optional[List[str]]:
        tok = token.lower()
        candidates = [p for p in paths if any(seg.lower() == tok for seg in p)]
        if not candidates:
            return None
        # Prefer longer (more specific) paths
        candidates.sort(key=lambda p: (len(p), sum(len(s) for s in p)), reverse=True)
        return candidates[0]

    # Strong keyword rules (Korean/English synonyms)
    # Freshdesk (헬프데스크/티켓 필드/포털 등)
    if any(k in text for k in ["freshdesk", "헬프데스크", "helpdesk", "ticket field", "티켓 필드", "언어 관리", "번역", "yml", "yaml"]):
        # Try to map to Freshworks Suite > Freshdesk
        p = find_path_containing("Freshdesk")
        if p:
            return p

    if any(k in text for k in ["freshservice"]):
        p = find_path_containing("Freshservice")
        if p:
            return p

    if any(k in text for k in ["freshsales"]):
        p = find_path_containing("Freshsales (Suite)") or find_path_containing("Freshsales")
        if p:
            return p

    if any(k in text for k in ["freshchat", "freddy", "bot", "프레디", "챗봇"]):
        p = find_path_containing("Freshchat/Freddy Bot")
        if p:
            return p

    if "splashtop" in text:
        p = find_path_containing("Splashtop")
        if p:
            return p

    if any(k in text for k in ["google workspace", "g suite", "gmail", "google drive", "구글", "지메일", "드라이브", "캘린더"]):
        # Prefer most specific Google leaf if present
        for leaf in ["Gmail", "Google Drive", "Google Calendar", "Control Panel", "Google Workspace"]:
            p = find_path_containing(leaf)
            if p:
                return p

    if any(k in text for k in ["spanning"]):
        p = find_path_containing("Spanning Backup")
        if p:
            return p

    # Generic: choose the longest leaf whose segment appears as substring
    scored: List[tuple[int, int, List[str]]] = []
    for p in paths:
        leaf = p[-1].lower()
        if leaf and leaf in text:
            scored.append((len(leaf), len(p), p))
    if scored:
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return scored[0][2]

    return None


def _upsert_field_proposal(
    proposals: List[Dict[str, Any]],
    field_name: str,
    field_label: str,
    proposed_value: Any,
    reason: str,
) -> List[Dict[str, Any]]:
    # Remove existing entries for same field_name
    out = [p for p in proposals if p.get("field_name") != field_name]
    out.append(
        {
            "field_name": field_name,
            "field_label": field_label,
            "proposed_value": proposed_value,
            "reason": reason,
        }
    )
    return out


def _compact_ticket_fields_for_llm(
    ticket_fields: List[Dict[str, Any]],
    *,
    max_fields: int = 50,
    max_choices: int = 80,
    exclude_nested_fields: bool = True,
) -> List[Dict[str, Any]]:
    """Reduce Freshdesk ticket fields schema for LLM input.

    목적: 토큰/지연 폭증을 막기 위해 "필드 제안"에 필요한 최소 정보만 전달.

    - nested_field는 선택지 트리가 매우 커질 수 있어 기본적으로 LLM 입력에서 제외한다.
      (nested_field는 별도 규칙 기반 후처리로 제안)
    - 각 필드의 choices는 상한을 둔다.
    """
    if not ticket_fields:
        return []

    out: List[Dict[str, Any]] = []

    for f in ticket_fields:
        if not isinstance(f, dict):
            continue

        f_type = f.get("type")
        if exclude_nested_fields and f_type == "nested_field":
            continue

        item: Dict[str, Any] = {
            "name": f.get("name"),
            "label": f.get("label") or f.get("label_for_customers") or f.get("label_in_portal"),
            "type": f_type,
            "required": f.get("required"),
        }

        # Choices trimming (drop huge option lists)
        choices = f.get("choices")
        if isinstance(choices, list):
            if len(choices) > max_choices:
                item["choices"] = [str(x) for x in choices[:max_choices]]
                item["choices_truncated"] = True
            else:
                item["choices"] = choices
        elif isinstance(choices, dict):
            keys = list(choices.keys())
            if len(keys) > max_choices:
                item["choices"] = {k: choices[k] for k in keys[:max_choices]}
                item["choices_truncated"] = True
            else:
                item["choices"] = choices

        out.append(item)

        if len(out) >= max_fields:
            break

    return out


def _postprocess_nested_field_proposals(
    result: Dict[str, Any],
    ticket_context: Dict[str, Any],
    ticket_fields: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """규칙 기반으로 nested_field 제안을 보정/추가한다.

    - LLM이 nested_field를 오분류하거나, 아예 제안하지 않은 경우를 보정한다.
    - mixed-depth(카테고리만/서브까지만/아이템까지) 구조를 지원한다.
    """
    try:
        subject = (ticket_context.get("subject") or "")
        description = (ticket_context.get("description") or ticket_context.get("description_text") or "")

        nested_roots = [f for f in ticket_fields if isinstance(f, dict) and f.get("type") == "nested_field"]
        if not nested_roots:
            return result

        root = nested_roots[0]
        root_name = root.get("name")
        root_label = root.get("label") or root.get("label_for_customers") or root_name
        nested_fields = root.get("nested_ticket_fields") or []
        level2 = next((nf for nf in nested_fields if nf.get("level") == 2), None)
        level3 = next((nf for nf in nested_fields if nf.get("level") == 3), None)

        paths = _build_nested_leaf_paths(root.get("choices"))
        best = _pick_best_nested_path(subject, description, paths)
        if not (best and root_name):
            return result

        proposals = result.get("field_proposals") or []
        if not isinstance(proposals, list):
            proposals = []

        reason = "티켓 내용의 키워드와 중첩 필드 선택지 트리를 기반으로 카테고리를 보정했습니다."

        proposals = _upsert_field_proposal(
            proposals,
            field_name=str(root_name),
            field_label=str(root_label),
            proposed_value=best[0],
            reason=reason,
        )

        if level2 and len(best) >= 2:
            proposals = _upsert_field_proposal(
                proposals,
                field_name=str(level2.get("name")),
                field_label=str(level2.get("label") or level2.get("label_in_portal") or level2.get("name")),
                proposed_value=best[1],
                reason=reason,
            )
        if level3 and len(best) >= 3:
            proposals = _upsert_field_proposal(
                proposals,
                field_name=str(level3.get("name")),
                field_label=str(level3.get("label") or level3.get("label_in_portal") or level3.get("name")),
                proposed_value=best[2],
                reason=reason,
            )

        result["field_proposals"] = proposals
        return result

    except Exception as e:
        logger.warning(f"Nested field post-processing skipped due to error: {e}")
        return result

class LLMAdapter:
    def __init__(self):
        self.settings = get_settings()
        self.gateway = get_llm_gateway()

    async def generate(
        self, 
        system_prompt: str, 
        user_prompt: str, 
        temperature: float = 0.7,
        json_mode: bool = False,
        *,
        purpose: str = "generate",
        timeout_ms: Optional[int] = None,
    ) -> str:
        """Generate text response from LLM"""
        req = LLMRequest(
            purpose=purpose,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            json_mode=json_mode,
            timeout_ms=timeout_ms,
        )
        res = await self.gateway.generate(req)
        return res.content

    async def analyze_ticket(self, ticket_context: Dict[str, Any], response_tone: str = "formal") -> Dict[str, Any]:
        """Analyze ticket for intent, sentiment, summary, and field proposals"""
        
        # Handle both snake_case and camelCase keys (due to Pydantic aliases)
        ticket_fields = ticket_context.get("ticket_fields") or ticket_context.get("ticketFields") or []
        
        # NOTE: analyze_ticket는 intent/summary까지 포함하므로 프롬프트가 크다.
        #       field-only 사용 시에는 propose_fields_only()를 사용하자.
        system_prompt = f"""
        You are an expert customer support analyzer. Analyze the ticket and return JSON with:
        - intent: (inquiry, complaint, request, technical_issue)
        - sentiment: (positive, neutral, negative, urgent)
        - summary: 1-sentence summary in Korean ({response_tone} tone)
                - summary_sections: 2 to 3 sections for human agents to quickly understand the ticket.
                    Each section must include:
                    - title: short title in Korean
                    - content: 1~3 sentences in Korean ({response_tone} tone)
        - key_entities: list of important entities
        - field_proposals: List of suggested field updates based on the provided schema.
          Each proposal must include:
          - field_name: The API name of the field (from schema)
          - field_label: The display label of the field
          - proposed_value: The value to set (must match schema choices if applicable)
          - reason: A clear explanation in Korean ({response_tone} tone) why this value is proposed.

        IMPORTANT: 
        - Only propose updates for fields defined in the 'ticket_fields_schema' below.
        - Pay special attention to nested fields (fields with 'choices' that have sub-choices).
        - For nested fields, propose the leaf value if possible, or the appropriate level value.
        
        Ticket Fields Schema:
        {json.dumps(ticket_fields, ensure_ascii=False, indent=2)}
        """
        
        # Remove ticket_fields from user_prompt to save tokens, as it's in system prompt
        context_copy = ticket_context.copy()
        context_copy.pop("ticket_fields", None)
        context_copy.pop("ticketFields", None)
        
        user_prompt = json.dumps(context_copy, ensure_ascii=False)

        response = await self.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.3,
            json_mode=True,
            purpose="analyze_ticket",
        )

        result = json.loads(response)

        # --- Ensure summary_sections exists (backward compatible with older prompts/models) ---
        try:
            sections = result.get("summary_sections") or result.get("summarySections")
            if not isinstance(sections, list) or not sections:
                raise ValueError("summary_sections missing")

            normalized_sections = []
            for s in sections:
                if not isinstance(s, dict):
                    continue
                title = (s.get("title") or "").strip()
                content = (s.get("content") or "").strip()
                if not title or not content:
                    continue
                normalized_sections.append({"title": title, "content": content})

            if len(normalized_sections) < 2:
                raise ValueError("summary_sections insufficient")

            result["summary_sections"] = normalized_sections[:3]

        except Exception:
            subject = str(ticket_context.get("subject") or "").strip()
            description = str(ticket_context.get("description") or ticket_context.get("description_text") or "").strip()
            summary = str(result.get("summary") or "").strip()
            if not summary:
                summary = subject or "티켓 요약을 생성하지 못했습니다."

            desc_snippet = description
            if len(desc_snippet) > 300:
                desc_snippet = desc_snippet[:300] + "…"

            result["summary_sections"] = [
                {
                    "title": "핵심 이슈",
                    "content": summary,
                },
                {
                    "title": "현재 상태",
                    "content": desc_snippet or "설명(본문) 정보가 없습니다.",
                },
            ]

        result = _postprocess_nested_field_proposals(result, ticket_context, ticket_fields)

        return result

    async def propose_fields_only(self, ticket_context: Dict[str, Any], response_tone: str = "formal") -> Dict[str, Any]:
        """필드 제안만(경량) 생성한다.

        목표:
        - LLM 호출을 1번으로 제한(analysis + synthesis 중 synthesis는 생략 가능)
        - conversations/거대한 nested_field 트리/긴 choices로 인한 토큰 폭증을 줄인다.
        """

        # Handle both snake_case and camelCase keys
        ticket_fields = ticket_context.get("ticket_fields") or ticket_context.get("ticketFields") or []
        if not isinstance(ticket_fields, list):
            ticket_fields = []

        compact_schema = _compact_ticket_fields_for_llm(
            ticket_fields,
            max_fields=60,
            max_choices=80,
            exclude_nested_fields=True,
        )

        system_prompt = f"""
        You are an expert customer support field classifier.

        Return JSON with ONLY:
        - field_proposals: List of suggested field updates based on the provided schema.
          Each proposal must include:
          - field_name: The API name of the field (from schema)
          - field_label: The display label of the field
          - proposed_value: The value to set (must match schema choices if applicable)
          - reason: A short explanation in Korean ({response_tone} tone)

        Hard rules:
        - Only propose updates for fields defined in 'ticket_fields_schema' below.
        - Do NOT propose updates for nested_field here. (Nested fields are handled separately.)
        - Keep reasons concise.

        Ticket Fields Schema (compact):
        {json.dumps(compact_schema, ensure_ascii=False)}
        """

        # Remove huge keys from user prompt
        context_copy = dict(ticket_context)
        context_copy.pop("ticket_fields", None)
        context_copy.pop("ticketFields", None)
        context_copy.pop("conversations", None)

        user_prompt = json.dumps(context_copy, ensure_ascii=False)

        response = await self.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.2,
            json_mode=True,
            purpose="propose_fields_only",
        )

        result = json.loads(response)
        # Ensure expected shape
        if not isinstance(result, dict):
            result = {}
        if "field_proposals" not in result or not isinstance(result.get("field_proposals"), list):
            result["field_proposals"] = []

        # Add/override nested_field proposals via rules
        result = _postprocess_nested_field_proposals(result, ticket_context, ticket_fields)
        return result

    async def propose_solution(
        self, 
        ticket_context: Dict[str, Any], 
        search_results: Dict[str, Any],
        analysis_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate solution analysis and field updates"""
        system_prompt = """
        You are an expert customer support engineer.

        Your job:
        - Read the ticket AND its full conversations (if present) and derive an evidence-based root cause and next steps.

        Hard rules (must follow):
        1) Use ONLY information that appears in the provided context (ticket + conversations + similar cases + kb).
           Do NOT invent a root cause or a fix.
        2) If the evidence is insufficient, say so explicitly and provide concrete follow-up questions/checks.
        3) When you mention a cause or a step, reference what part of the conversations supports it
           (e.g., "대화에서 고객이 '...'라고 말함", "에러 로그/증상", "시도한 조치").
        4) Prefer actionable steps, ordered, with expected outcome and what to do if it fails.

        Return JSON with:
        - cause: Root cause hypothesis grounded in the context (1-3 sentences in Korean)
        - solution: Next steps in Korean (bullet points allowed). Include verification steps.
        - field_updates: { priority, status, type, tags } (only if changes are truly needed)
        - reasoning: Brief evidence-based justification in Korean (cite conversation evidence)
        """
        
        context = {
            "ticket": ticket_context,
            "analysis": analysis_result,
            "similar_cases": search_results.get("similar_cases", []),
            "kb_articles": search_results.get("kb_procedures", [])
        }
        
        user_prompt = json.dumps(context, ensure_ascii=False)
        response = await self.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.7,
            json_mode=True,
            purpose="propose_solution",
        )
        return json.loads(response)
