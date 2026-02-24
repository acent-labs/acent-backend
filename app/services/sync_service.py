"""
Sync Service - Data Synchronization Orchestrator
Coordinates Freshdesk data collection → Normalization → Transformation → RAG Upload

Based on TypeScript orchestrator.ts implementation

Features:
- Full data sync pipeline
- Incremental sync with timestamp tracking
- EntityMapper integration for ID→Label resolution
- Progress tracking
- Error handling with retry
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Awaitable

from app.services.freshdesk_client import FreshdeskClient
from app.services.ingestion_service import FreshdeskIngestionService
from app.services.entity_mapper import EntityMapper
from app.services.normalizer import FreshdeskNormalizer, FieldMappings, NormalizedTicket, NormalizedArticle
from app.services.transformer import DataTransformer, GeminiDocument
from app.services.ticket_metadata_service import (
    TicketMetadataService,
    TicketMetadataRecord,
    ArticleMetadataRecord,
)

logger = logging.getLogger(__name__)


@dataclass
class SyncProgress:
    """동기화 진행 상황"""
    phase: str = "idle"  # "idle" | "initializing" | "tickets" | "articles" | "uploading" | "completed" | "failed"
    tickets_processed: int = 0
    tickets_total: int = 0
    articles_processed: int = 0
    articles_total: int = 0
    documents_uploaded: int = 0
    documents_total: int = 0
    current_batch: int = 0
    total_batches: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


@dataclass
class SyncResult:
    """동기화 결과"""
    synced_at: str
    tickets_count: int = 0
    articles_count: int = 0
    documents_count: int = 0
    metadata_tickets_saved: int = 0
    metadata_articles_saved: int = 0
    errors: list[str] = field(default_factory=list)
    rag_store_name: str | None = None


@dataclass
class SyncOptions:
    """동기화 옵션"""
    include_tickets: bool = True
    include_articles: bool = True
    incremental: bool = False
    ticket_since: datetime | None = None
    article_since: datetime | None = None
    batch_size: int = 10
    max_concurrency: int = 5
    upload_batch_size: int = 10


# Type alias for upload callback
UploadCallback = Callable[[list[GeminiDocument]], Awaitable[None]]


class SyncService:
    """
    Data Sync Orchestrator
    
    Coordinates the entire Freshdesk → Gemini data pipeline:
    1. Initialize EntityMapper (load batch entities)
    2. Fetch tickets/articles via IngestionService
    3. Normalize with EntityMapper labels
    4. Transform to Gemini documents
    5. Upload via callback (Gemini RAG API)
    
    Usage:
        client = FreshdeskClient(domain, api_key)
        sync_service = SyncService(client, tenant_id="wedosoft")
        
        async def upload_docs(docs: list[GeminiDocument]) -> None:
            # Upload to Gemini
            pass
        
        result = await sync_service.sync(
            options=SyncOptions(include_tickets=True),
            upload_callback=upload_docs,
        )
    """
    
    def __init__(
        self,
        client: FreshdeskClient,
        *,
        tenant_id: str = "default",
        platform: str = "freshdesk",
        batch_size: int = 10,
        max_concurrency: int = 5,
        metadata_service: TicketMetadataService | None = None,
    ) -> None:
        self.client = client
        self.tenant_id = tenant_id
        self.platform = platform
        self.batch_size = batch_size
        self.max_concurrency = max_concurrency
        
        # Components
        self._entity_mapper = EntityMapper(client)
        self._ingestion_service = FreshdeskIngestionService(client)
        self._normalizer = FreshdeskNormalizer()
        self._transformer = DataTransformer(tenant_id=tenant_id, platform=platform)
        self._metadata_service = metadata_service
        
        # State
        self._progress = SyncProgress()
        self._initialized = False
        
        # Last sync timestamps (for incremental sync)
        self._last_ticket_sync: datetime | None = None
        self._last_article_sync: datetime | None = None
        
        # Collected metadata records for batch save
        self._ticket_metadata_records: list[TicketMetadataRecord] = []
        self._article_metadata_records: list[ArticleMetadataRecord] = []
    
    @property
    def progress(self) -> SyncProgress:
        """현재 진행 상황"""
        return self._progress
    
    async def initialize(self) -> None:
        """
        Initialize pipeline - load EntityMapper data
        """
        if self._initialized:
            return
        
        logger.info("Initializing SyncService...")
        self._progress.phase = "initializing"
        
        try:
            # Initialize EntityMapper (loads agents, groups, products, categories, ticket_fields)
            await self._entity_mapper.initialize()
            logger.info(f"EntityMapper initialized: {self._entity_mapper.get_stats()}")
            
            # Load field mappings into Normalizer
            field_mappings = FieldMappings(
                status=self._entity_mapper.get_field_choices("status"),
                priority=self._entity_mapper.get_field_choices("priority"),
                source=self._entity_mapper.get_field_choices("source"),
                type=self._entity_mapper.get_field_choices("type"),
            )
            self._normalizer.load_field_mappings(field_mappings)
            logger.info("Field mappings loaded into normalizer")
            
            self._initialized = True
            logger.info("SyncService initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize SyncService: {e}")
            self._progress.phase = "failed"
            self._progress.error = str(e)
            raise
    
    async def sync(
        self,
        options: SyncOptions | None = None,
        upload_callback: UploadCallback | None = None,
    ) -> SyncResult:
        """
        Execute sync pipeline
        
        Args:
            options: Sync configuration options
            upload_callback: Async function to upload documents to RAG store
        
        Returns:
            SyncResult with sync statistics
        """
        opts = options or SyncOptions()
        
        logger.info(
            f"Starting sync (tenant={self.tenant_id}, "
            f"tickets={opts.include_tickets}, articles={opts.include_articles}, "
            f"incremental={opts.incremental})"
        )
        
        self._progress = SyncProgress(
            phase="initializing",
            started_at=datetime.now(),
        )
        
        result = SyncResult(synced_at=datetime.now().isoformat())
        all_documents: list[GeminiDocument] = []
        
        try:
            # Initialize if needed
            await self.initialize()
            
            # Determine since dates
            ticket_since = opts.ticket_since
            article_since = opts.article_since
            
            if opts.incremental:
                if not ticket_since and self._last_ticket_sync:
                    ticket_since = self._last_ticket_sync
                if not article_since and self._last_article_sync:
                    article_since = self._last_article_sync
            
            # Run sync tasks in parallel
            # 1. Ticket Sync Task
            async def run_ticket_sync():
                if not opts.include_tickets:
                    return 0
                
                if upload_callback:
                    count = await self._sync_tickets_batch(
                        upload_callback=upload_callback,
                        since=ticket_since,
                        concurrency=opts.max_concurrency,
                    )
                    self._last_ticket_sync = datetime.now()
                    return count
                else:
                    # Legacy mode (collect all)
                    ticket_docs = await self._sync_tickets(
                        since=ticket_since,
                        concurrency=opts.max_concurrency,
                    )
                    all_documents.extend(ticket_docs)
                    self._last_ticket_sync = datetime.now()
                    return len(ticket_docs)

            # 2. Article Sync Task
            async def run_article_sync():
                if not opts.include_articles:
                    return 0
                
                article_docs = await self._sync_articles(since=article_since)
                
                if upload_callback and article_docs:
                    await self._upload_documents(
                        article_docs,
                        upload_callback,
                        batch_size=opts.upload_batch_size,
                    )
                else:
                    all_documents.extend(article_docs)
                
                self._last_article_sync = datetime.now()
                return len(article_docs)

            # Execute both in parallel
            self._progress.phase = "syncing"
            results = await asyncio.gather(run_ticket_sync(), run_article_sync())
            
            result.tickets_count = results[0]
            result.articles_count = results[1]
            result.documents_count = result.tickets_count + result.articles_count
            
            # Upload remaining documents (only for legacy mode where upload_callback wasn't used inside tasks)
            if upload_callback and all_documents:
                await self._upload_documents(
                    all_documents,
                    upload_callback,
                    batch_size=opts.upload_batch_size,
                )
            
            # Save metadata to Supabase (final flush)
            if self._metadata_service:
                await self._save_metadata(result)
            
            self._progress.phase = "completed"
            self._progress.completed_at = datetime.now()
            
            logger.info(
                f"Sync completed: {result.tickets_count} tickets, "
                f"{result.articles_count} articles, {result.documents_count} documents, "
                f"metadata: {result.metadata_tickets_saved} tickets, {result.metadata_articles_saved} articles"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Sync failed: {e}")
            self._progress.phase = "failed"
            self._progress.error = str(e)
            result.errors.append(str(e))
            raise

    async def _sync_tickets_batch(
        self,
        upload_callback: UploadCallback,
        since: datetime | None = None,
        concurrency: int = 5,
    ) -> int:
        """
        Sync tickets in batches: Fetch Generator → Normalize → Transform → Upload
        """
        self._progress.phase = "tickets"
        logger.info(f"Fetching tickets in batches (since={since})")
        
        total_count = 0
        
        async for batch_records in self._ingestion_service.fetch_tickets_generator(
            since=since,
            include_conversations=True,
            conversation_concurrency=concurrency,
        ):
            # Normalize batch
            normalized_tickets = []
            for record in batch_records:
                # Map ticket entities using EntityMapper
                entity_labels = await self._entity_mapper.map_ticket_entities(record.ticket)
                
                # Enrich ticket with labels
                enriched_ticket = {
                    **record.ticket,
                    "responder_label": entity_labels["responder_label"],
                    "group_label": entity_labels["group_label"],
                    "company_label": entity_labels["company_label"],
                    "requester_label": entity_labels["requester_label"],
                    "product_label": entity_labels["product_label"],
                }
                
                # Normalize
                normalized = self._normalizer.normalize_ticket(
                    enriched_ticket,
                    record.conversations,
                )
                normalized_tickets.append(normalized)
                
                # Collect metadata for Supabase
                if self._metadata_service:
                    self._collect_ticket_metadata(record.ticket, normalized)
            
            # Transform batch
            documents = self._transformer.transform_tickets(normalized_tickets)
            
            if documents:
                # Upload batch immediately
                await self._upload_documents(
                    documents,
                    upload_callback,
                    batch_size=len(documents) # Upload whole batch at once
                )
                total_count += len(documents)
            
            # Update progress
            self._progress.tickets_processed += len(batch_records)
            self._progress.tickets_total = self._progress.tickets_processed # Estimate total as we go
            
            logger.info(f"Sync progress: {self._progress.tickets_processed} tickets processed so far")
            
            # Flush metadata periodically (optional, but good for safety)
            # For now, we keep metadata in memory until end or implement batch flush
            
        logger.info(f"Batch sync completed: {total_count} tickets processed")
        return total_count
    
    async def _sync_tickets(
        self,
        since: datetime | None = None,
        concurrency: int = 5,
    ) -> list[GeminiDocument]:
        """
        Sync tickets: Fetch → Enrich with EntityMapper → Normalize → Transform
        """
        self._progress.phase = "tickets"
        
        logger.info(f"Fetching tickets (since={since})")
        
        # Fetch tickets with conversations
        ticket_records = await self._ingestion_service.fetch_tickets(
            since=since,
            include_conversations=True,
            conversation_concurrency=concurrency,
        )
        
        self._progress.tickets_total = len(ticket_records)
        logger.info(f"Fetched {len(ticket_records)} tickets")
        
        # Normalize with EntityMapper labels
        normalized_tickets = []
        for idx, record in enumerate(ticket_records):
            # Map ticket entities using EntityMapper
            entity_labels = await self._entity_mapper.map_ticket_entities(record.ticket)
            
            # Enrich ticket with labels
            enriched_ticket = {
                **record.ticket,
                "responder_label": entity_labels["responder_label"],
                "group_label": entity_labels["group_label"],
                "company_label": entity_labels["company_label"],
                "requester_label": entity_labels["requester_label"],
                "product_label": entity_labels["product_label"],
            }
            
            # Normalize
            normalized = self._normalizer.normalize_ticket(
                enriched_ticket,
                record.conversations,
            )
            normalized_tickets.append(normalized)
            
            # Collect metadata for Supabase
            if self._metadata_service:
                self._collect_ticket_metadata(record.ticket, normalized)
            
            self._progress.tickets_processed = idx + 1
        
        total_conversations = sum(len(r.conversations) for r in ticket_records)
        logger.info(
            f"Normalized {len(normalized_tickets)} tickets with "
            f"{total_conversations} total conversations"
        )
        
        # Transform to Gemini documents
        documents = self._transformer.transform_tickets(normalized_tickets)
        logger.info(f"Transformed to {len(documents)} ticket documents")
        
        return documents
    
    async def _sync_articles(
        self,
        since: datetime | None = None,
    ) -> list[GeminiDocument]:
        """
        Sync articles: Fetch → Enrich with EntityMapper → Normalize → Transform
        """
        self._progress.phase = "articles"
        
        logger.info(f"Fetching articles (since={since})")
        
        # Fetch articles
        raw_articles = await self._ingestion_service.fetch_articles(since=since)
        
        self._progress.articles_total = len(raw_articles)
        logger.info(f"Fetched {len(raw_articles)} articles")
        
        # Normalize with EntityMapper labels
        normalized_articles = []
        for idx, article in enumerate(raw_articles):
            # Map article entities using EntityMapper
            entity_labels = await self._entity_mapper.map_article_entities(article)
            
            # Enrich article with labels
            enriched_article = {
                **article,
                "category_label": entity_labels["category_label"],
                "folder_label": entity_labels["folder_label"],
            }
            
            # Normalize
            normalized = self._normalizer.normalize_article(enriched_article)
            normalized_articles.append(normalized)
            
            # Collect metadata for Supabase
            if self._metadata_service:
                self._collect_article_metadata(article, normalized)
            
            self._progress.articles_processed = idx + 1
        
        logger.info(f"Normalized {len(normalized_articles)} articles")
        
        # Transform to Gemini documents
        documents = self._transformer.transform_articles(normalized_articles)
        logger.info(f"Transformed to {len(documents)} article documents")
        
        return documents
    
    async def _upload_documents(
        self,
        documents: list[GeminiDocument],
        upload_callback: UploadCallback,
        batch_size: int = 10,
    ) -> None:
        """
        Upload documents in batches
        """
        self._progress.phase = "uploading"
        self._progress.documents_total = len(documents)
        
        total_batches = (len(documents) + batch_size - 1) // batch_size
        self._progress.total_batches = total_batches
        
        logger.info(f"Uploading {len(documents)} documents in {total_batches} batches")
        
        for batch_idx in range(total_batches):
            start = batch_idx * batch_size
            end = min(start + batch_size, len(documents))
            batch = documents[start:end]
            
            self._progress.current_batch = batch_idx + 1
            
            try:
                await upload_callback(batch)
                self._progress.documents_uploaded += len(batch)
                
                logger.info(
                    f"Upload progress: {self._progress.documents_uploaded}/{len(documents)} "
                    f"({batch_idx + 1}/{total_batches} batches)"
                )
            except Exception as e:
                logger.error(f"Batch upload failed: {e}")
                raise
    
    async def _save_metadata(self, result: SyncResult) -> None:
        """
        Save collected metadata to Supabase
        """
        if not self._metadata_service:
            return
        
        logger.info(
            f"Saving metadata: {len(self._ticket_metadata_records)} tickets, "
            f"{len(self._article_metadata_records)} articles"
        )
        
        # Save ticket metadata
        if self._ticket_metadata_records:
            ticket_result = await self._metadata_service.upsert_tickets(
                self._ticket_metadata_records
            )
            result.metadata_tickets_saved = ticket_result.success
            if ticket_result.failed > 0:
                result.errors.append(f"Failed to save {ticket_result.failed} ticket metadata records")
        
        # Save article metadata
        if self._article_metadata_records:
            article_result = await self._metadata_service.upsert_articles(
                self._article_metadata_records
            )
            result.metadata_articles_saved = article_result.success
            if article_result.failed > 0:
                result.errors.append(f"Failed to save {article_result.failed} article metadata records")
        
        # Clear collected records
        self._ticket_metadata_records = []
        self._article_metadata_records = []
    
    def _collect_ticket_metadata(self, ticket: dict, normalized: NormalizedTicket) -> None:
        """
        Collect ticket metadata for batch save
        """
        ticket_id = ticket.get("id")
        if not ticket_id:
            return
        
        record = TicketMetadataRecord(
            platform=self.platform,
            ticket_id=int(ticket_id),
            external_id=str(ticket_id),
            status=normalized.status,
            priority=normalized.priority,
            source=normalized.source,
            requester=normalized.requester,
            requester_id=normalized.requester_id,
            responder=normalized.responder,
            responder_id=normalized.responder_id,
            group_name=normalized.group,
            group_id=normalized.group_id,
            tags=normalized.tags,
            ticket_created_at=normalized.created_at,
            ticket_updated_at=normalized.updated_at,
        )
        self._ticket_metadata_records.append(record)
    
    def _collect_article_metadata(self, article: dict, normalized: NormalizedArticle) -> None:
        """
        Collect article metadata for batch save
        """
        article_id = article.get("id")
        if not article_id:
            return
        
        record = ArticleMetadataRecord(
            platform=self.platform,
            article_id=int(article_id),
            external_id=str(article_id),
            title=normalized.title,
            folder_id=normalized.folder_id,
            folder_name=normalized.folder,
            category_id=normalized.category_id,
            category_name=normalized.category,
            status=normalized.status,
            article_created_at=normalized.created_at,
            article_updated_at=normalized.updated_at,
        )
        self._article_metadata_records.append(record)
    
    async def close(self) -> None:
        """Cleanup resources"""
        await self.client.close()


def create_sync_service(
    domain: str,
    api_key: str,
    *,
    tenant_id: str = "default",
    platform: str = "freshdesk",
    batch_size: int = 10,
    max_concurrency: int = 5,
) -> SyncService:
    """
    Factory function to create SyncService
    
    Usage:
        sync_service = create_sync_service(
            domain="wedosoft.freshdesk.com",
            api_key="your-api-key",
            tenant_id="wedosoft",
        )
        result = await sync_service.sync()
    """
    client = FreshdeskClient(domain, api_key)
    return SyncService(
        client,
        tenant_id=tenant_id,
        platform=platform,
        batch_size=batch_size,
        max_concurrency=max_concurrency,
    )
