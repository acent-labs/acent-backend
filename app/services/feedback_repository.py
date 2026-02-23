"""
Feedback Repository

Supabase-backed persistence for HITL feedback, training samples,
and quality logs.

Ported from nexus-ai repository.py with agent-platform patterns.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _get_supabase():
    """Get Supabase client (lazy import to avoid circular deps)."""
    settings = get_settings()
    if not settings.supabase_common_url or not settings.supabase_common_service_role_key:
        logger.warning("Supabase not configured; feedback operations will be skipped")
        return None
    from supabase import create_client
    return create_client(
        settings.supabase_common_url,
        settings.supabase_common_service_role_key,
    )


class FeedbackRepository:
    """Repository for HITL feedback and training data."""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = _get_supabase()
        return self._client

    # =========================================================================
    # Training Samples
    # =========================================================================

    async def upsert_training_sample(
        self,
        *,
        tenant_id: str,
        ticket_id: str,
        analysis_id: str,
        original_response: Dict[str, Any],
        prompt_version: Optional[str] = None,
        model: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[str]:
        """Create or update a training sample for an analysis."""
        if not self.client:
            return None

        sample_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        try:
            result = self.client.table("training_samples").upsert(
                {
                    "id": sample_id,
                    "tenant_id": tenant_id,
                    "ticket_id": ticket_id,
                    "analysis_id": analysis_id,
                    "original_response": original_response,
                    "prompt_version": prompt_version,
                    "model": model,
                    "agent_id": agent_id,
                    "created_at": now,
                    "updated_at": now,
                },
                on_conflict="tenant_id,ticket_id,analysis_id",
            ).execute()

            if result.data:
                return result.data[0].get("id", sample_id)
            return sample_id
        except Exception as e:
            logger.error("Failed to upsert training sample: %s", e, exc_info=True)
            return None

    # =========================================================================
    # Feedback Events
    # =========================================================================

    async def submit_feedback(
        self,
        *,
        analysis_id: str,
        event_type: str,
        rating: Optional[int] = None,
        feedback_text: Optional[str] = None,
        agent_id: Optional[str] = None,
        tenant_id: str,
    ) -> Optional[str]:
        """Submit a feedback event (helpful/not_helpful)."""
        if not self.client:
            return None

        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        try:
            metadata: Dict[str, Any] = {}
            if rating is not None:
                metadata["rating"] = rating
            if feedback_text:
                metadata["feedback_text"] = feedback_text

            self.client.table("feedback_events").insert(
                {
                    "id": event_id,
                    "analysis_id": analysis_id,
                    "tenant_id": tenant_id,
                    "event_type": event_type,
                    "agent_id": agent_id,
                    "metadata": metadata,
                    "created_at": now,
                }
            ).execute()

            # Also insert quality log for not_helpful
            if event_type == "not_helpful":
                await self.insert_quality_log(
                    tenant_id=tenant_id,
                    analysis_id=analysis_id,
                    event_type="not_helpful",
                    agent_id=agent_id,
                    metadata=metadata,
                )

            logger.info(
                "Feedback submitted: event_id=%s analysis_id=%s type=%s",
                event_id, analysis_id, event_type,
            )
            return event_id
        except Exception as e:
            logger.error("Failed to submit feedback: %s", e, exc_info=True)
            return None

    # =========================================================================
    # Approved Response (Edit)
    # =========================================================================

    async def update_approved_response(
        self,
        *,
        analysis_id: str,
        approved_response: Dict[str, Any],
        agent_id: Optional[str] = None,
        tenant_id: str,
    ) -> Optional[str]:
        """Save an agent-edited/approved response as a training sample."""
        if not self.client:
            return None

        now = datetime.now(timezone.utc).isoformat()

        try:
            # Find or create training sample
            existing = (
                self.client.table("training_samples")
                .select("id")
                .eq("analysis_id", analysis_id)
                .limit(1)
                .execute()
            )

            if existing.data:
                sample_id = existing.data[0]["id"]
                self.client.table("training_samples").update(
                    {
                        "approved_response": approved_response,
                        "agent_id": agent_id or existing.data[0].get("agent_id"),
                        "updated_at": now,
                    }
                ).eq("id", sample_id).execute()
            else:
                # Create new sample from analysis result
                sample_id = str(uuid.uuid4())

                # Get original analysis for context
                analysis_result = (
                    self.client.table("ticket_analyses")
                    .select("*")
                    .eq("run_id", analysis_id)
                    .limit(1)
                    .execute()
                )

                original = {}
                ticket_id = "unknown"
                if analysis_result.data:
                    row = analysis_result.data[0]
                    ticket_id = row.get("ticket_id", "unknown")
                    original = {
                        "narrative": row.get("narrative"),
                        "root_cause": row.get("root_cause"),
                        "resolution": row.get("resolution"),
                        "confidence": float(row.get("confidence", 0)),
                    }

                self.client.table("training_samples").insert(
                    {
                        "id": sample_id,
                        "tenant_id": tenant_id,
                        "ticket_id": ticket_id,
                        "analysis_id": analysis_id,
                        "original_response": original,
                        "approved_response": approved_response,
                        "agent_id": agent_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                ).execute()

            # Record edit event
            await self.submit_feedback(
                analysis_id=analysis_id,
                event_type="edited",
                agent_id=agent_id,
                tenant_id=tenant_id,
            )

            logger.info(
                "Approved response saved: sample_id=%s analysis_id=%s",
                sample_id, analysis_id,
            )
            return sample_id
        except Exception as e:
            logger.error(
                "Failed to save approved response: %s", e, exc_info=True
            )
            return None

    # =========================================================================
    # Training Export
    # =========================================================================

    async def get_exportable_samples(
        self,
        *,
        tenant_id: str,
        limit: int = 100,
        min_rating: Optional[int] = None,
        mark_exported: bool = True,
    ) -> List[Dict[str, Any]]:
        """Get training samples with approved responses for fine-tuning."""
        if not self.client:
            return []

        try:
            query = (
                self.client.table("training_samples")
                .select("*")
                .eq("tenant_id", tenant_id)
                .not_.is_("approved_response", "null")
                .eq("is_exported", False)
                .order("created_at", desc=False)
                .limit(limit)
            )

            if min_rating is not None:
                # Join with feedback_events for rating filter
                pass  # TODO: implement rating filter via join

            result = query.execute()
            samples = result.data or []

            # Mark as exported if requested
            if mark_exported and samples:
                sample_ids = [s["id"] for s in samples]
                now = datetime.now(timezone.utc).isoformat()
                self.client.table("training_samples").update(
                    {"is_exported": True, "exported_at": now}
                ).in_("id", sample_ids).execute()

            return samples
        except Exception as e:
            logger.error("Failed to get exportable samples: %s", e, exc_info=True)
            return []

    # =========================================================================
    # Quality Logs
    # =========================================================================

    async def insert_quality_log(
        self,
        *,
        tenant_id: str,
        analysis_id: Optional[str] = None,
        event_type: str,
        agent_id: Optional[str] = None,
        detected_language: Optional[str] = None,
        response_language: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Insert a quality log entry."""
        if not self.client:
            return None

        log_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        try:
            self.client.table("quality_logs").insert(
                {
                    "id": log_id,
                    "tenant_id": tenant_id,
                    "analysis_id": analysis_id,
                    "event_type": event_type,
                    "agent_id": agent_id,
                    "detected_language": detected_language,
                    "response_language": response_language,
                    "metadata": metadata or {},
                    "created_at": now,
                }
            ).execute()
            return log_id
        except Exception as e:
            logger.error("Failed to insert quality log: %s", e, exc_info=True)
            return None


# =============================================================================
# Singleton
# =============================================================================

_repository: Optional[FeedbackRepository] = None


def get_feedback_repository() -> FeedbackRepository:
    """Get singleton feedback repository."""
    global _repository
    if _repository is None:
        _repository = FeedbackRepository()
    return _repository
