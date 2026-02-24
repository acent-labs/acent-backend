from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

from fastapi import HTTPException, status

from app.core.config import get_settings
from app.models.metadata import MetadataFilter
from app.models.session import ChatRequest, ChatResponse
from app.services.gemini_client import GeminiClientError
from app.services.gemini_file_search_client import GeminiFileSearchClient
from app.services.common_documents import CommonDocumentsService, get_common_documents_service

LOGGER = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You are a helpful customer support assistant. "
    "Answer ONLY the user's CURRENT question based on the provided search results (Context). "
    "Do NOT repeat or re-answer previous questions from the conversation history. "
    "If the answer is not in the context, politely state that you cannot find the information. "
    "Keep your response focused and concise."
)


class CommonChatHandler:
    """모든 RAG 소스 (tickets, articles, common)를 처리하는 통합 핸들러"""
    
    def __init__(
        self,
        *,
        gemini_client: GeminiFileSearchClient,
        store_names: Dict[str, str],  # {"tickets": "store_id", "articles": "store_id", "common": "store_id"}
        documents_service: Optional[CommonDocumentsService] = None,
    ) -> None:
        self.gemini_client = gemini_client
        self.store_names = store_names  # source -> store_name 매핑
        self.documents_service = documents_service

    def can_handle(self, request: ChatRequest) -> bool:
        """사용 가능한 store가 하나라도 있으면 처리 가능"""
        if not self.store_names:
            return False
        
        sources = [source.strip() for source in (request.sources or []) if source.strip()]
        if not sources:
            # sources 지정 안되면 기본적으로 처리
            return True
        
        # 역방향 매핑: store path -> source key
        store_to_source = {v: k for k, v in self.store_names.items()}
        
        # 요청된 sources 중 하나라도 store_names에 있거나, store path로 매칭되면 처리 가능
        for source in sources:
            if source in self.store_names:
                return True
            if source in store_to_source:
                return True
        return False

    def _get_store_names_for_request(self, request: ChatRequest) -> List[str]:
        """요청에 맞는 store names 반환"""
        sources = [source.strip() for source in (request.sources or []) if source.strip()]
        
        if not sources:
            # sources 지정 안되면 모든 사용 가능한 store 사용
            return list(self.store_names.values())
        
        # 역방향 매핑: store path -> source key
        store_to_source = {v: k for k, v in self.store_names.items()}
        
        result = []
        for s in sources:
            if s in self.store_names:
                # source key (e.g., "common") -> store path
                result.append(self.store_names[s])
            elif s in store_to_source:
                # store path 직접 사용
                result.append(s)
        
        return result if result else list(self.store_names.values())

    def _enrich_chunks_with_metadata(self, chunks: List[dict]) -> List[dict]:
        LOGGER.info("🔍 Enrichment called with %d chunks, has service: %s", len(chunks) if chunks else 0, bool(self.documents_service))
        
        if not self.documents_service or not chunks:
            return chunks

        slug_map = {}
        slugs_to_fetch = set()

        for chunk in chunks:
            retrieved = chunk.get("retrievedContext") or {}
            title = retrieved.get("title")
            LOGGER.debug("Processing chunk title: %s", title)
            if not title:
                continue
            
            # Try to extract slug from title (format: {slug}-{lang})
            # We try stripping known suffixes
            slug = None
            for lang in ["ko", "en"]:
                suffix = f"-{lang}"
                if title.endswith(suffix):
                    slug = title[:-len(suffix)]
                    break
            
            if slug:
                slugs_to_fetch.add(slug)
                # Map title to slug for later lookup
                slug_map[title] = slug

        LOGGER.info("📚 Fetching %d slugs from Supabase: %s", len(slugs_to_fetch), list(slugs_to_fetch))
        
        if not slugs_to_fetch:
            return chunks

        try:
            docs = self.documents_service.fetch_by_slugs(list(slugs_to_fetch), columns=["slug", "csv_id", "short_slug", "product", "title_ko", "title_en"])
            doc_map = {doc["slug"]: doc for doc in docs}
            
            LOGGER.info("✅ Fetched %d documents", len(doc_map))
            
            for chunk in chunks:
                retrieved = chunk.get("retrievedContext") or {}
                title = retrieved.get("title")
                if not title:
                    continue
                
                slug = slug_map.get(title)
                if slug and slug in doc_map:
                    doc = doc_map[slug]
                    # Build URL: /docs/{product}/{csv_id}-{short_slug}
                    product = doc.get("product")
                    csv_id = doc.get("csv_id")
                    short_slug = doc.get("short_slug")
                    
                    if product and csv_id and short_slug:
                        doc_url = f"/docs/{product}/{csv_id}-{short_slug}"
                        retrieved["uri"] = doc_url
                        LOGGER.info("🔗 Injected URI for '%s': %s", title, doc_url)
                    else:
                        LOGGER.warning("Missing URL components for '%s': product=%s, csv_id=%s, short_slug=%s",
                                     title, product, csv_id, short_slug)

                    # Add title_ko and title_en for multilingual support
                    title_ko = doc.get("title_ko")
                    title_en = doc.get("title_en")
                    if title_ko:
                        retrieved["title_ko"] = title_ko
                        retrieved["title"] = title_ko  # 기존 호환성 유지
                        LOGGER.info("📝 Added title_ko for '%s': %s", title, title_ko)
                    if title_en:
                        retrieved["title_en"] = title_en
                        LOGGER.info("📝 Added title_en for '%s': %s", title, title_en)
        except Exception as e:
            LOGGER.warning("Failed to enrich chunks with metadata: %s", e)

        return chunks

    async def handle(self, request: ChatRequest, *, history: Optional[List[dict]] = None) -> ChatResponse:
        metadata_filters: List[MetadataFilter] = []
        filter_summaries: List[str] = []
        enhanced_query = request.query

        # context 기반 system instruction 생성
        system_instruction = SYSTEM_INSTRUCTION
        if request.context:
            current_page = request.context.get("currentPage", "")
            page_content = request.context.get("pageContent", "")
            custom_instruction = request.context.get("instruction", "")
            ticket_data = request.context.get("ticket")
            
            context_parts = []
            
            if custom_instruction:
                context_parts.append(custom_instruction)
            if current_page:
                context_parts.append(f"현재 사용자가 보고 있는 문서 제목: '{current_page}'")
            if page_content:
                # 너무 길면 잘라냄 (최대 2000자)
                truncated_content = page_content[:2000] if len(page_content) > 2000 else page_content
                context_parts.append(f"현재 문서 내용:\n{truncated_content}")
            
            if ticket_data:
                ticket = ticket_data
                # If it's wrapped in "ticket" key
                if "ticket" in ticket_data and isinstance(ticket_data["ticket"], dict):
                    ticket = ticket_data["ticket"]
                
                conversations = ticket.get("conversations", [])
                
                ticket_parts = []
                ticket_parts.append(f"--- 현재 티켓 정보 (ID: {ticket.get('id')}) ---")
                ticket_parts.append(f"제목: {ticket.get('subject')}")
                ticket_parts.append(f"내용: {ticket.get('description_text')}")
                
                if conversations:
                    # 시간순 정렬 (오래된 순)
                    try:
                        conversations.sort(key=lambda x: x.get("created_at", ""))
                    except Exception:
                        pass # 정렬 실패시 원본 순서 유지

                    ticket_parts.append("\n--- 대화 내역 (시간순) ---")
                    for conv in conversations:
                        is_incoming = conv.get("incoming", False)
                        is_private = conv.get("private", False)
                        created_at = conv.get("created_at", "")
                        
                        role = "고객"
                        if not is_incoming:
                            role = "상담원"
                            if is_private:
                                role = "내부 메모 (상담원)"
                        
                        body = conv.get("body_text", "").strip()
                        if body:
                            # 타임스탬프 포함하여 맥락 제공
                            timestamp_str = f"[{created_at}] " if created_at else ""
                            ticket_parts.append(f"{timestamp_str}[{role}]: {body}")
                
                context_parts.append("\n".join(ticket_parts))
                LOGGER.info("🎫 Ticket context added for ticket: %s (%d conversations)", ticket.get('id'), len(conversations))

            if context_parts:
                context_instruction = "\n\n".join(context_parts)
                system_instruction = f"{SYSTEM_INSTRUCTION}\n\n[현재 컨텍스트]\n{context_instruction}"
                LOGGER.info("📄 Context-aware system instruction added. Length: %d chars", len(system_instruction))
                
                # 너무 긴 경우 경고 (예: 100,000자 이상)
                if len(system_instruction) > 100000:
                    LOGGER.warning("⚠️ System instruction is very long (%d chars). Model might struggle.", len(system_instruction))

        if request.common_product:
            # 메타데이터 필터: 제품명은 시스템 고정 값이므로 그대로 사용
            metadata_filters.append(MetadataFilter(key="product", value=request.common_product.strip(), operator="EQUALS"))

            filter_summaries.append(f"제품={request.common_product}")
            enhanced_query = f"[{request.common_product}] {request.query}"

        # 요청에 맞는 store names 가져오기
        store_names_to_search = self._get_store_names_for_request(request)
        sources_used = [s for s in (request.sources or []) if s in self.store_names] or list(self.store_names.keys())
        
        LOGGER.info("🔍 Searching stores: %s for sources: %s", store_names_to_search, sources_used)

        try:
            t0 = time.perf_counter()
            result = await self.gemini_client.search(
                query=enhanced_query,
                store_names=store_names_to_search,
                metadata_filters=metadata_filters,
                conversation_history=history,
                system_instruction=system_instruction,
            )
            LOGGER.info(
                "Gemini search done stores=%s sources=%s ms=%s",
                len(store_names_to_search),
                sources_used,
                int((time.perf_counter() - t0) * 1000),
            )
        except GeminiClientError as exc:
            LOGGER.exception("Gemini 검색 실패")
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        grounding_chunks = result.get("grounding_chunks", [])
        grounding_chunks = self._enrich_chunks_with_metadata(grounding_chunks)

        payload = {
            "text": result["text"],
            "groundingChunks": grounding_chunks,
            "ragStoreName": store_names_to_search[0] if store_names_to_search else None,
            "sources": sources_used,
            "filters": filter_summaries,
            "knownContext": {},
        }
        return ChatResponse.model_validate(payload)

    async def stream_handle(self, request: ChatRequest, *, history: Optional[List[dict]] = None):
        metadata_filters: List[MetadataFilter] = []
        filter_summaries: List[str] = []
        enhanced_query = request.query

        # context 기반 system instruction 생성
        system_instruction = SYSTEM_INSTRUCTION
        if request.context:
            current_page = request.context.get("currentPage", "")
            page_content = request.context.get("pageContent", "")
            custom_instruction = request.context.get("instruction", "")
            ticket_data = request.context.get("ticket")
            
            context_parts = []
            
            if custom_instruction:
                context_parts.append(custom_instruction)
            if current_page:
                context_parts.append(f"현재 사용자가 보고 있는 문서 제목: '{current_page}'")
            if page_content:
                # 너무 길면 잘라냄 (최대 2000자)
                truncated_content = page_content[:2000] if len(page_content) > 2000 else page_content
                context_parts.append(f"현재 문서 내용:\n{truncated_content}")
            
            if ticket_data:
                ticket = ticket_data
                # If it's wrapped in "ticket" key
                if "ticket" in ticket_data and isinstance(ticket_data["ticket"], dict):
                    ticket = ticket_data["ticket"]
                
                conversations = ticket.get("conversations", [])
                
                ticket_parts = []
                ticket_parts.append(f"--- 현재 티켓 정보 (ID: {ticket.get('id')}) ---")
                ticket_parts.append(f"제목: {ticket.get('subject')}")
                ticket_parts.append(f"내용: {ticket.get('description_text')}")
                
                if conversations:
                    # 시간순 정렬 (오래된 순)
                    try:
                        conversations.sort(key=lambda x: x.get("created_at", ""))
                    except Exception:
                        pass # 정렬 실패시 원본 순서 유지

                    ticket_parts.append("\n--- 대화 내역 (시간순) ---")
                    for conv in conversations:
                        is_incoming = conv.get("incoming", False)
                        is_private = conv.get("private", False)
                        created_at = conv.get("created_at", "")
                        
                        role = "고객"
                        if not is_incoming:
                            role = "상담원"
                            if is_private:
                                role = "내부 메모 (상담원)"
                        
                        body = conv.get("body_text", "").strip()
                        if body:
                            # 타임스탬프 포함하여 맥락 제공
                            timestamp_str = f"[{created_at}] " if created_at else ""
                            ticket_parts.append(f"{timestamp_str}[{role}]: {body}")
                
                context_parts.append("\n".join(ticket_parts))
                LOGGER.info("🎫 Ticket context added for ticket: %s (%d conversations)", ticket.get('id'), len(conversations))

            if context_parts:
                context_instruction = "\n\n".join(context_parts)
                system_instruction = f"{SYSTEM_INSTRUCTION}\n\n[현재 컨텍스트]\n{context_instruction}"
                LOGGER.info("📄 Context-aware system instruction added. Length: %d chars", len(system_instruction))
                
                # 너무 긴 경우 경고 (예: 100,000자 이상)
                if len(system_instruction) > 100000:
                    LOGGER.warning("⚠️ System instruction is very long (%d chars). Model might struggle.", len(system_instruction))

        if request.common_product:
            # 메타데이터 필터: 제품명은 시스템 고정 값이므로 그대로 사용
            metadata_filters.append(MetadataFilter(key="product", value=request.common_product.strip(), operator="EQUALS"))

            filter_summaries.append(f"제품={request.common_product}")
            enhanced_query = f"[{request.common_product}] {request.query}"

        # 요청에 맞는 store names 가져오기
        store_names_to_search = self._get_store_names_for_request(request)
        sources_used = [s for s in (request.sources or []) if s in self.store_names] or list(self.store_names.keys())

        try:
            stream_t0 = time.perf_counter()
            first_result_logged = False
            async for event in self.gemini_client.stream_search(
                query=enhanced_query,
                store_names=store_names_to_search,
                metadata_filters=metadata_filters,
                conversation_history=history,
                system_instruction=system_instruction,
            ):
                if not first_result_logged and event.get("event") == "result":
                    first_result_logged = True
                    LOGGER.info(
                        "Gemini stream first_result stores=%s sources=%s ms=%s",
                        len(store_names_to_search),
                        sources_used,
                        int((time.perf_counter() - stream_t0) * 1000),
                    )
                if event["event"] == "result":
                    payload = event["data"]
                    
                    grounding_chunks = payload.get("groundingChunks", [])
                    if grounding_chunks:
                        payload["groundingChunks"] = self._enrich_chunks_with_metadata(grounding_chunks)

                    payload.update(
                        {
                            "ragStoreName": store_names_to_search[0] if store_names_to_search else None,
                            "sources": sources_used,
                            "filters": filter_summaries,
                            "knownContext": {},
                        }
                    )
                    yield {"event": "result", "data": payload}
                else:
                    yield event
        except GeminiClientError as exc:
            yield {
                "event": "error",
                "data": {"message": str(exc) or "잠시 후 다시 시도해 주세요."},
            }



def get_common_chat_handler() -> Optional[CommonChatHandler]:
    settings = get_settings()
    api_key = settings.gemini_api_key or os.getenv("GEMINI_API_KEY")
    
    if not api_key:
        return None
    
    # 모든 사용 가능한 store 수집
    store_names: Dict[str, str] = {}
    
    if settings.gemini_store_tickets:
        store_names["tickets"] = settings.gemini_store_tickets
    if settings.gemini_store_articles:
        store_names["articles"] = settings.gemini_store_articles
    if settings.gemini_store_common:
        store_names["common"] = settings.gemini_store_common
    
    if not store_names:
        LOGGER.warning("No Gemini stores configured")
        return None
    
    LOGGER.info("🏪 Configured stores: %s", store_names)
    
    client = GeminiFileSearchClient(
        api_key=api_key,
        primary_model=settings.gemini_primary_model,
        fallback_model=settings.gemini_fallback_model,
    )
    documents_service = get_common_documents_service()
    return CommonChatHandler(
        gemini_client=client, 
        store_names=store_names,
        documents_service=documents_service
    )
