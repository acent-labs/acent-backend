"""
HITL Feedback Models

Pydantic schemas for feedback submission, response editing,
and training data export.

Ported from nexus-ai with adaptations for agent-platform.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# =============================================================================
# Feedback Submit
# =============================================================================


class FeedbackSubmitRequest(BaseModel):
    """Request to submit feedback on an analysis."""

    analysis_id: str = Field(..., description="Analysis run ID (UUID)")
    event_type: Literal["helpful", "not_helpful"] = Field(
        ..., description="Feedback type"
    )
    rating: Optional[int] = Field(
        None, ge=1, le=5, description="Optional rating (1-5)"
    )
    feedback_text: Optional[str] = Field(
        None, max_length=2000, description="Optional text feedback"
    )
    agent_id: Optional[str] = Field(None, description="Freshdesk agent ID")


class FeedbackSubmitResponse(BaseModel):
    """Response after submitting feedback."""

    success: bool
    event_id: str = Field(..., description="Created feedback event ID")
    message: str = "Feedback submitted successfully"


# =============================================================================
# Feedback Edit (Approved Response)
# =============================================================================


class FeedbackEditRequest(BaseModel):
    """Request to submit an agent-edited/approved response."""

    analysis_id: str = Field(..., description="Analysis run ID (UUID)")
    approved_response: Dict[str, Any] = Field(
        ..., description="Agent-edited response (full analysis JSON)"
    )
    agent_id: Optional[str] = Field(None, description="Freshdesk agent ID")


class FeedbackEditResponse(BaseModel):
    """Response after submitting edited response."""

    success: bool
    sample_id: str = Field(..., description="Training sample ID")
    message: str = "Edited response saved successfully"


# =============================================================================
# Training Export
# =============================================================================


class TrainingExportRequest(BaseModel):
    """Request to export training samples."""

    limit: int = Field(100, ge=1, le=1000, description="Max samples to export")
    min_rating: Optional[int] = Field(
        None, ge=1, le=5, description="Minimum rating filter"
    )
    mark_exported: bool = Field(
        True, description="Mark exported samples as exported"
    )


class TrainingSampleExport(BaseModel):
    """Single training sample for export."""

    id: str
    tenant_id: str
    ticket_id: str
    analysis_id: str
    original_response: Dict[str, Any]
    approved_response: Dict[str, Any]
    rating: Optional[int] = None
    prompt_version: Optional[str] = None
    model: Optional[str] = None
    created_at: Optional[str] = None


class TrainingExportResponse(BaseModel):
    """Response with exported training samples."""

    success: bool
    count: int
    samples: List[TrainingSampleExport]
