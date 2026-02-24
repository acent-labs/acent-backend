"""
Ticket Analysis Orchestrator

Coordinates the full analysis pipeline:
1. Validate input
2. Build prompt from registry
3. Call LLM
4. Parse and repair JSON
5. Validate output
6. Compute gate decision
7. Persist to Supabase
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.prompts.loader import get_prompt
from app.services.denoise import denoise_conversations
from app.services.guardrails import apply_guardrails
from app.services.llm_gateway import get_llm_gateway, LLMRequest, LLMResponse
from app.services.orchestrator.json_repair import try_parse_json
from app.services.orchestrator.persistence import get_analysis_persistence

logger = logging.getLogger(__name__)


@dataclass
class AnalysisOptions:
    """Options for analysis run."""

    skip_retrieval: bool = False
    include_evidence: bool = True
    confidence_threshold: float = 0.7
    selected_fields: Optional[List[str]] = None
    response_tone: str = "formal"


@dataclass
class AnalysisResult:
    """Result from analysis pipeline."""

    analysis_id: str
    analysis: Dict[str, Any]
    gate: str
    meta: Dict[str, Any]
    success: bool = True
    error: Optional[str] = None


class TicketAnalysisOrchestrator:
    """
    Orchestrates ticket analysis pipeline.

    Pipeline steps:
    1. Validate input (done by caller via schema_validation)
    2. Build prompt from registry
    3. Call LLM via gateway
    4. Parse JSON (with repair if needed)
    5. Validate output against schema
    6. Compute gate based on confidence
    7. Persist run and result to Supabase
    """

    PROMPT_ID = "ticket_analysis_cot_v1"

    # Gate thresholds
    GATE_CONFIRM_THRESHOLD = 0.9
    GATE_EDIT_THRESHOLD = 0.7
    GATE_DECIDE_THRESHOLD = 0.5

    def __init__(self):
        self._llm_gateway = None
        self._persistence = None

    @property
    def llm_gateway(self):
        """Lazy-load LLM gateway."""
        if self._llm_gateway is None:
            self._llm_gateway = get_llm_gateway()
        return self._llm_gateway

    @property
    def persistence(self):
        """Lazy-load persistence layer."""
        if self._persistence is None:
            self._persistence = get_analysis_persistence()
        return self._persistence

    async def run_ticket_analysis(
        self,
        normalized_input: Dict[str, Any],
        options: AnalysisOptions,
        tenant_id: str,
    ) -> AnalysisResult:
        """
        Run full analysis pipeline.

        Args:
            normalized_input: Validated ticket data (ticket_normalized_v1)
            options: Analysis configuration
            tenant_id: Tenant identifier

        Returns:
            AnalysisResult with analysis, gate, and metadata
        """
        analysis_id = str(uuid.uuid4())
        ticket_id = normalized_input.get("ticket_id", "unknown")
        t0 = time.perf_counter()

        # Mark run as started
        await self.persistence.save_analysis_run(
            analysis_id=analysis_id,
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            status="running",
        )

        try:
            # Step 1: Load and render prompt
            prompt_spec = get_prompt(self.PROMPT_ID)
            context = self._build_prompt_context(normalized_input, options)
            system_prompt, user_prompt = prompt_spec.render(context)

            # Step 2: Call LLM
            llm_request = LLMRequest(
                purpose="analyze_ticket_cot",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=prompt_spec.temperature,
                json_mode=prompt_spec.json_mode,
            )

            llm_response: LLMResponse = await self.llm_gateway.generate(llm_request)

            # Step 3: Parse JSON with repair
            analysis, parse_error = try_parse_json(llm_response.content)

            if analysis is None:
                raise ValueError(f"Failed to parse LLM response: {parse_error}")

            # Step 3.5: Apply guardrails (evidence validation + forbidden phrases)
            analysis, violations = apply_guardrails(analysis)
            if violations:
                logger.warning(
                    "[orchestrator] Guardrail violations fixed: %s", violations
                )

            # Step 4: Compute gate
            confidence = analysis.get("confidence", 0.0)
            gate = self._compute_gate(confidence, options.confidence_threshold)

            # Step 5: Apply field filtering if specified
            if options.selected_fields:
                proposals = analysis.get("field_proposals", [])
                analysis["field_proposals"] = [
                    p for p in proposals
                    if p.get("field_name") in options.selected_fields
                ]

            # Step 6: Build metadata
            latency_ms = int((time.perf_counter() - t0) * 1000)
            meta = {
                "llm_provider": llm_response.provider,
                "llm_model": llm_response.model,
                "prompt_version": self.PROMPT_ID,
                "latency_ms": latency_ms,
                "token_usage": {"input": 0, "output": 0},  # TODO: Track tokens
                "retrieval_count": len(context.get("similar_cases", [])) + len(context.get("kb_articles", [])),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "used_fallback": llm_response.used_fallback,
                "attempts": llm_response.attempts,
            }

            # Step 7: Persist success
            await self.persistence.save_analysis_run(
                analysis_id=analysis_id,
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                status="completed",
                gate=gate,
                meta=meta,
            )

            await self.persistence.save_analysis_result(
                analysis_id=analysis_id,
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                analysis=analysis,
            )

            logger.info(
                f"[orchestrator] Success: analysis_id={analysis_id} "
                f"gate={gate} confidence={confidence:.2f} latency_ms={latency_ms}"
            )

            return AnalysisResult(
                analysis_id=analysis_id,
                analysis=analysis,
                gate=gate,
                meta=meta,
                success=True,
            )

        except Exception as e:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            error_msg = str(e)

            logger.error(
                f"[orchestrator] Failed: analysis_id={analysis_id} "
                f"error={error_msg} latency_ms={latency_ms}",
                exc_info=True
            )

            # Persist failure
            await self.persistence.save_analysis_run(
                analysis_id=analysis_id,
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                status="failed",
                meta={"latency_ms": latency_ms},
                error_message=error_msg,
            )

            return AnalysisResult(
                analysis_id=analysis_id,
                analysis={},
                gate="TEACH",
                meta={
                    "llm_provider": "none",
                    "llm_model": "none",
                    "prompt_version": self.PROMPT_ID,
                    "latency_ms": latency_ms,
                    "error": error_msg,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                success=False,
                error=error_msg,
            )

    def _build_prompt_context(
        self,
        normalized_input: Dict[str, Any],
        options: AnalysisOptions,
    ) -> Dict[str, Any]:
        """Build context for prompt rendering."""
        # Denoise conversations (NoCut strategy)
        raw_conversations = normalized_input.get("conversations", [])
        denoise_result = denoise_conversations(raw_conversations)

        # Summarize ticket fields for prompt
        ticket_fields = normalized_input.get("ticket_fields", [])
        fields_summary = self._summarize_fields(ticket_fields)

        return {
            "ticket_id": normalized_input.get("ticket_id", ""),
            "subject": normalized_input.get("subject", "(제목 없음)"),
            "description": (
                normalized_input.get("description") or
                normalized_input.get("description_text") or
                "(설명 없음)"
            ),
            "conversations": denoise_result.conversation,
            "denoise_kept_indices": denoise_result.kept_original_indices,
            "custom_fields": normalized_input.get("custom_fields", {}),
            "ticket_fields_summary": fields_summary,
            "similar_cases": [],  # TODO: Implement retrieval
            "kb_articles": [],  # TODO: Implement retrieval
            "response_tone": options.response_tone,
        }

    def _summarize_fields(self, fields: List[Dict[str, Any]]) -> str:
        """Create compact field summary for prompt."""
        if not fields:
            return "(필드 스키마 없음)"

        lines = []
        for f in fields[:30]:  # Limit to prevent prompt bloat
            if not isinstance(f, dict):
                continue
            name = f.get("name", "unknown")
            label = f.get("label") or f.get("label_for_customers", "")
            f_type = f.get("type", "")
            lines.append(f"- {name} ({label}): {f_type}")

        return "\n".join(lines) if lines else "(필드 정보 없음)"

    def _compute_gate(self, confidence: float, threshold: float) -> str:
        """
        Compute gate decision based on confidence.

        Gates:
        - CONFIRM: High confidence, can auto-apply
        - EDIT: Good confidence, minor review needed
        - DECIDE: Medium confidence, agent decision needed
        - TEACH: Low confidence, requires learning feedback
        """
        if confidence >= self.GATE_CONFIRM_THRESHOLD:
            return "CONFIRM"
        elif confidence >= max(threshold, self.GATE_EDIT_THRESHOLD):
            return "EDIT"
        elif confidence >= self.GATE_DECIDE_THRESHOLD:
            return "DECIDE"
        else:
            return "TEACH"


# Singleton instance
_orchestrator: Optional[TicketAnalysisOrchestrator] = None


def get_ticket_analysis_orchestrator() -> TicketAnalysisOrchestrator:
    """Get singleton orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = TicketAnalysisOrchestrator()
    return _orchestrator
