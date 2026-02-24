"""
Data Transformer
Convert Freshdesk data to Gemini-compatible documents

Based on TypeScript transformer.ts implementation

Features:
- Ticket/Article → Gemini Document 변환
- Unified metadata schema
- ID and Label separation
- LLM-friendly content structure
- Proper null/undefined filtering
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.services.normalizer import NormalizedTicket, NormalizedArticle

logger = logging.getLogger(__name__)


@dataclass
class GeminiDocument:
    """Gemini RAG 문서"""
    id: str
    type: str  # "ticket" | "article"
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class DataTransformer:
    """
    Data Transformer
    
    Convert Freshdesk normalized data to Gemini-compatible documents.
    
    Following project-a's integrator.py pattern:
    - Unified metadata schema with required/optional fields
    - ID and Label separation for entities
    - Proper null/undefined filtering
    - LLM-friendly content structure
    
    Usage:
        transformer = DataTransformer(tenant_id="wedosoft", platform="freshdesk")
        doc = transformer.transform_ticket(normalized_ticket)
    """
    
    # Placeholder pattern for filtering
    PLACEHOLDER_PATTERN = re.compile(r"^(Agent_|Group_|Contact_|Category_|Folder_)\d+$")
    
    def __init__(
        self, 
        tenant_id: str = "default",
        platform: str = "freshdesk",
    ) -> None:
        self.tenant_id = tenant_id
        self.platform = platform
    
    # ========================================================================
    # Ticket Transformation
    # ========================================================================
    
    def transform_ticket(self, ticket: NormalizedTicket) -> GeminiDocument:
        """
        Transform a ticket into a Gemini document
        Following project-a's integrator.py pattern
        """
        content = self._build_ticket_content(ticket)
        
        # Build metadata following project-a's unified schema
        # Required fields (never null):
        # - doc_type, platform, tenant_id, original_id
        # - status, priority, source, tags
        # - created_at, updated_at, has_attachments
        metadata: dict[str, Any] = {
            # Required system fields
            "doc_type": "ticket",
            "platform": self.platform,
            "tenant_id": self.tenant_id,
            "original_id": str(ticket.id),
            
            # Required status fields
            "status": ticket.status,
            "priority": ticket.priority,
            "source": ticket.source,
            
            # Required datetime fields - Unix timestamp (seconds) for numeric comparison
            "created_at": self._to_unix_timestamp(ticket.created_at),
            "updated_at": self._to_unix_timestamp(ticket.updated_at),
            
            # Required array field (as comma-separated string for Gemini)
            "tags": ",".join(ticket.tags) if ticket.tags else "",
            
            # Required boolean field
            "has_attachments": self._has_attachments(ticket),
            
            # Requester (required - always present)
            "requester": ticket.requester or "",
            "requester_id": ticket.requester_id,
        }
        
        # Optional entity fields - only include if they have valid values
        if ticket.responder and not self.PLACEHOLDER_PATTERN.match(ticket.responder):
            metadata["responder"] = ticket.responder
        if ticket.responder_id is not None:
            metadata["responder_id"] = str(ticket.responder_id)
        
        if ticket.group and not self.PLACEHOLDER_PATTERN.match(ticket.group):
            metadata["group"] = ticket.group
        if ticket.group_id is not None:
            metadata["group_id"] = str(ticket.group_id)
        
        # Product field (optional)
        if ticket.product and not self.PLACEHOLDER_PATTERN.match(ticket.product):
            metadata["product"] = ticket.product
        if ticket.product_id is not None:
            metadata["product_id"] = str(ticket.product_id)
        
        # Type field (optional)
        if ticket.type:
            metadata["type"] = ticket.type
        
        # Custom fields (optional)
        if ticket.custom_fields:
            metadata["custom_fields"] = ticket.custom_fields
        
        # Clean metadata
        cleaned_metadata = self._clean_metadata(metadata)
        
        return GeminiDocument(
            id=f"ticket_{ticket.id}",
            type="ticket",
            title=ticket.subject,
            content=content,
            metadata=cleaned_metadata,
        )
    
    def transform_tickets(
        self, 
        tickets: list[NormalizedTicket],
    ) -> list[GeminiDocument]:
        """Transform multiple tickets"""
        logger.info(
            f"Transforming {len(tickets)} tickets to Gemini documents "
            f"(tenant: {self.tenant_id}, platform: {self.platform})"
        )
        return [self.transform_ticket(ticket) for ticket in tickets]
    
    def _has_attachments(self, ticket: NormalizedTicket) -> bool:
        """Check if ticket has attachments"""
        if ticket.conversations:
            return any(
                conv.attachments and len(conv.attachments) > 0
                for conv in ticket.conversations
            )
        return False
    
    def _build_ticket_content(self, ticket: NormalizedTicket) -> str:
        """
        Build searchable content from ticket data
        Uses description_text (already cleaned) and body_text from conversations
        """
        parts: list[str] = []
        
        # Title
        parts.append(f"Subject: {ticket.subject}")
        
        # Description
        if ticket.description_text:
            parts.append(f"\nDescription:\n{ticket.description_text}")
        
        # Conversations
        if ticket.conversations and len(ticket.conversations) > 0:
            parts.append(f"\n--- Conversations ({len(ticket.conversations)}) ---")
            
            for idx, conv in enumerate(ticket.conversations, 1):
                direction = "→ Incoming" if conv.incoming else "← Outgoing"
                visibility = "[Private]" if conv.private else "[Public]"
                sender = conv.user_name or conv.from_email or "Unknown"
                
                parts.append(f"\nConversation #{idx} {direction} {visibility}")
                parts.append(f"From: {sender}")
                parts.append(f"Date: {self._format_datetime(conv.created_at)}")
                
                if conv.body_text:
                    parts.append(f"\n{conv.body_text}")
                
                if conv.attachments and len(conv.attachments) > 0:
                    attachment_names = [a.get("name", "unknown") for a in conv.attachments]
                    parts.append(f"\nAttachments: {', '.join(attachment_names)}")
            
            parts.append("\n--- End of Conversations ---")
        
        # Metadata
        parts.append(f"\nStatus: {ticket.status}")
        parts.append(f"Priority: {ticket.priority}")
        parts.append(f"Source: {ticket.source}")
        
        if ticket.type:
            parts.append(f"Type: {ticket.type}")
        
        if ticket.product:
            parts.append(f"Product: {ticket.product}")
        
        parts.append(f"Requester: {ticket.requester}")
        
        if ticket.responder:
            parts.append(f"Assigned to: {ticket.responder}")
        
        if ticket.group:
            parts.append(f"Group: {ticket.group}")
        
        # Tags
        if ticket.tags and len(ticket.tags) > 0:
            parts.append(f"Tags: {', '.join(ticket.tags)}")
        
        # Dates
        parts.append(f"\nCreated: {self._format_datetime(ticket.created_at)}")
        parts.append(f"Updated: {self._format_datetime(ticket.updated_at)}")
        
        if ticket.due_by:
            parts.append(f"Due by: {self._format_datetime(ticket.due_by)}")
        
        return "\n".join(parts)
    
    # ========================================================================
    # Article Transformation
    # ========================================================================
    
    def transform_article(self, article: NormalizedArticle) -> GeminiDocument:
        """
        Transform an article into a Gemini document
        Following project-a's integrator.py pattern
        """
        content = self._build_article_content(article)
        
        # Build metadata
        metadata: dict[str, Any] = {
            # Required system fields
            "doc_type": "article",
            "platform": self.platform,
            "tenant_id": self.tenant_id,
            "original_id": str(article.id),
            
            # Required status field
            "status": article.status,
            
            # Required datetime fields
            "created_at": self._to_unix_timestamp(article.created_at),
            "updated_at": self._to_unix_timestamp(article.updated_at),
            
            # Required array field (as comma-separated string for Gemini)
            "tags": ",".join(article.tags) if article.tags else "",
            
            # Category and folder (with ID and label)
            "category": article.category,
            "category_id": str(article.category_id),
            "folder": article.folder,
            "folder_id": str(article.folder_id),
            
            # Hierarchy
            "hierarchy": f"{article.category} > {article.folder}",
        }
        
        # SEO data
        if article.seo_data:
            metadata["seo"] = article.seo_data
        
        # Clean metadata
        cleaned_metadata = self._clean_metadata(metadata)
        
        return GeminiDocument(
            id=f"article_{article.id}",
            type="article",
            title=article.title,
            content=content,
            metadata=cleaned_metadata,
        )
    
    def transform_articles(
        self, 
        articles: list[NormalizedArticle],
    ) -> list[GeminiDocument]:
        """Transform multiple articles"""
        logger.info(
            f"Transforming {len(articles)} articles to Gemini documents "
            f"(tenant: {self.tenant_id}, platform: {self.platform})"
        )
        return [self.transform_article(article) for article in articles]
    
    def _build_article_content(self, article: NormalizedArticle) -> str:
        """
        Build searchable content from article data
        Uses description_text (already cleaned, no HTML)
        """
        parts: list[str] = []
        
        # Title
        parts.append(f"Title: {article.title}")
        
        # SEO metadata
        if article.seo_data:
            if article.seo_data.get("meta_title"):
                parts.append(f"Meta Title: {article.seo_data['meta_title']}")
            if article.seo_data.get("meta_description"):
                parts.append(f"Meta Description: {article.seo_data['meta_description']}")
        
        # Content
        if article.description_text:
            parts.append(f"\nContent:\n{article.description_text}")
        
        # Metadata
        parts.append(f"\nCategory: {article.category}")
        parts.append(f"Folder: {article.folder}")
        parts.append(f"Status: {article.status}")
        
        # Tags
        if article.tags and len(article.tags) > 0:
            parts.append(f"Tags: {', '.join(article.tags)}")
        
        # Dates
        parts.append(f"\nCreated: {self._format_datetime(article.created_at)}")
        parts.append(f"Updated: {self._format_datetime(article.updated_at)}")
        
        return "\n".join(parts)
    
    # ========================================================================
    # Utility Methods
    # ========================================================================
    
    def _clean_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """
        Clean metadata by removing undefined/null values and placeholder strings
        """
        cleaned: dict[str, Any] = {}
        
        for key, value in metadata.items():
            # Skip None values
            if value is None:
                continue
            
            # Skip "undefined" strings
            if isinstance(value, str) and value == "undefined":
                continue
            
            # Skip placeholder values like "Agent_123", "Group_456"
            if isinstance(value, str) and self.PLACEHOLDER_PATTERN.match(value):
                continue
            
            cleaned[key] = value
        
        return cleaned
    
    def _to_unix_timestamp(self, datetime_str: str | None) -> int:
        """Convert datetime string to Unix timestamp (seconds)"""
        if not datetime_str:
            return 0
        
        try:
            # ISO 8601 format
            dt = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return 0
    
    def _format_datetime(self, datetime_str: str | None) -> str:
        """Format datetime string for display"""
        if not datetime_str:
            return ""
        
        try:
            dt = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return datetime_str
    
    def document_to_file_content(self, document: GeminiDocument) -> str:
        """
        Convert Gemini document to file content format
        (for temporary file storage or debugging)
        """
        import json
        
        metadata_lines = [
            f"{key}: {json.dumps(value)}"
            for key, value in document.metadata.items()
        ]
        
        return f"""---
Document ID: {document.id}
Type: {document.type}
Title: {document.title}
{chr(10).join(metadata_lines)}
---

{document.content}
"""


def create_transformer(
    tenant_id: str = "default",
    platform: str = "freshdesk",
) -> DataTransformer:
    """Factory function to create transformer"""
    return DataTransformer(tenant_id=tenant_id, platform=platform)
