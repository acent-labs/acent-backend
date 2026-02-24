from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional, AsyncGenerator

import httpx

from app.models.metadata import MetadataFilter
from app.services.gemini_client import (
    GeminiClientError,
    GeminiRetryableError,
    _build_metadata_expression,
)

LOGGER = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 503}


class GeminiFileSearchClient:
    """Gemini File Search REST client with retry & streaming support."""

    def __init__(
        self,
        api_key: str,
        primary_model: str,
        fallback_model: Optional[str] = None,
        *,
        timeout: float = 30.0,
        max_attempts_per_model: int = 2,
        retry_backoff_seconds: float = 1.5,
    ) -> None:
        if not api_key:
            raise GeminiClientError("Gemini API key is required")
        if not primary_model:
            raise GeminiClientError("Gemini model name is required")
        self.api_key = api_key
        self.models: List[str] = [primary_model]
        if fallback_model and fallback_model not in self.models:
            self.models.append(fallback_model)
        self.timeout = timeout
        self.max_attempts_per_model = max(1, max_attempts_per_model)
        self.retry_backoff_seconds = max(0.5, retry_backoff_seconds)

    async def search(
        self,
        *,
        query: str,
        store_names: List[str],
        metadata_filters: Optional[List[MetadataFilter]] = None,
        conversation_history: Optional[List[dict]] = None,
        system_instruction: Optional[str] = None,
    ) -> dict[str, Any]:
        metadata_expression = _build_metadata_expression(metadata_filters)
        contents = self._build_contents(query, conversation_history)
        LOGGER.info("Built contents with %d turns (history: %d)", len(contents), len(conversation_history or []))

        last_error: Optional[Exception] = None
        for model_name in self.models:
            for attempt in range(self.max_attempts_per_model):
                try:
                    data = await self._execute_request(
                        model_name, contents, metadata_expression, store_names, system_instruction
                    )
                    return self._build_response_payload(data, store_names, metadata_filters)
                except GeminiRetryableError as exc:
                    last_error = exc
                    if attempt + 1 < self.max_attempts_per_model:
                        await self._sleep_backoff(attempt)
                        continue
                except GeminiClientError as exc:
                    last_error = exc
                    LOGGER.warning("Gemini model %s failed: %s", model_name, exc)
                    break

        raise GeminiClientError("Gemini 검색 실패") from last_error

    async def stream_search(
        self,
        *,
        query: str,
        store_names: List[str],
        metadata_filters: Optional[List[MetadataFilter]] = None,
        conversation_history: Optional[List[str]] = None,
        system_instruction: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        metadata_expression = _build_metadata_expression(metadata_filters)
        contents = self._build_contents(query, conversation_history)

        yield {"event": "status", "data": {"message": "최적의 답변을 찾고 있습니다..."}}

        for model_name in self.models:
            yield {
                "event": "status",
                "data": {"message": "최적의 답변을 찾고 있습니다...", "model": model_name},
            }
            for attempt in range(self.max_attempts_per_model):
                if attempt > 0:
                    yield {
                        "event": "status",
                        "data": {
                            "message": "잠시만 기다려 주세요...",
                            "model": model_name,
                            "attempt": attempt + 1,
                        },
                    }
                try:
                    data = await self._execute_request(
                        model_name, contents, metadata_expression, store_names, system_instruction
                    )
                    payload = self._build_response_payload(data, store_names, metadata_filters)
                    yield {"event": "result", "data": payload}
                    return
                except GeminiRetryableError as exc:
                    LOGGER.warning("Gemini model %s retryable error: %s", model_name, exc)
                    if attempt + 1 < self.max_attempts_per_model:
                        await self._sleep_backoff(attempt)
                        continue
                except GeminiClientError as exc:
                    LOGGER.warning("Gemini model %s failed: %s", model_name, exc)
                    yield {
                        "event": "status",
                        "data": {"message": str(exc), "model": model_name},
                    }
                    break

        yield {"event": "error", "data": {"message": "잠시 후 다시 시도해 주세요."}}

    async def _execute_request(
        self,
        model_name: str,
        contents: List[Dict[str, Any]],
        metadata_expression: Optional[str],
        store_names: List[str],
        system_instruction: Optional[str],
    ) -> Dict[str, Any]:
        if not store_names:
            raise GeminiClientError("At least one file search store name is required")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        body: Dict[str, Any] = {
            "contents": contents,
            "tools": [
                {
                    "fileSearch": {
                        "fileSearchStoreNames": store_names,
                    }
                }
            ],
        }
        if metadata_expression:
            body["tools"][0]["fileSearch"]["metadataFilter"] = metadata_expression

        if system_instruction:
            body["system_instruction"] = {"parts": [{"text": system_instruction}]}

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise GeminiRetryableError("Gemini REST 요청이 타임아웃되었습니다.") from exc
        except httpx.HTTPError as exc:
            raise GeminiRetryableError("Gemini REST 요청 중 네트워크 오류가 발생했습니다.") from exc

        if response.status_code >= 400:
            message = response.text
            if response.status_code in RETRYABLE_STATUS_CODES:
                raise GeminiRetryableError(f"Gemini REST error {response.status_code}: {message}")
            raise GeminiClientError(f"Gemini REST error {response.status_code}: {message}")

        return response.json()

    async def _execute_request(
        self,
        model_name: str,
        contents: List[Dict[str, Any]],
        metadata_expression: Optional[str],
        store_names: List[str],
        system_instruction: Optional[str],
    ) -> Dict[str, Any]:
        if not store_names:
            raise GeminiClientError("At least one file search store name is required")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        body: Dict[str, Any] = {
            "contents": contents,
            "tools": [
                {
                    "fileSearch": {
                        "fileSearchStoreNames": store_names,
                    }
                }
            ],
        }
        if metadata_expression:
            body["tools"][0]["fileSearch"]["metadataFilter"] = metadata_expression

        if system_instruction:
            body["system_instruction"] = {"parts": [{"text": system_instruction}]}

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise GeminiRetryableError("Gemini REST 요청이 타임아웃되었습니다.") from exc
        except httpx.HTTPError as exc:
            raise GeminiRetryableError("Gemini REST 요청 중 네트워크 오류가 발생했습니다.") from exc

        if response.status_code >= 400:
            message = response.text
            if response.status_code in RETRYABLE_STATUS_CODES:
                raise GeminiRetryableError(f"Gemini REST error {response.status_code}: {message}")
            raise GeminiClientError(f"Gemini REST error {response.status_code}: {message}")

        return response.json()

    def _build_contents(self, query: str, conversation_history: Optional[List[dict]] = None) -> List[Dict[str, Any]]:
        """Build contents with proper user/model turn alternation.
        
        Args:
            query: Current user query
            conversation_history: List of dicts with 'role' (user/model) and 'text' keys
        """
        contents: List[Dict[str, Any]] = []
        
        # Add conversation history with proper roles
        for turn in (conversation_history or []):
            if isinstance(turn, dict) and "role" in turn and "text" in turn:
                role = turn["role"]
                text = turn["text"]
                if role in ("user", "model") and text and text.strip():
                    contents.append({
                        "role": role,
                        "parts": [{"text": text}],
                    })
            elif isinstance(turn, str) and turn.strip():
                # Fallback for legacy string-only history (treat as user)
                contents.append({
                    "role": "user",
                    "parts": [{"text": turn}],
                })
        
        # Add current query
        contents.append({
            "role": "user",
            "parts": [{"text": query}],
        })
        
        LOGGER.info("📝 Conversation history: %d turns", len(contents) - 1)
        for i, c in enumerate(contents):
            role = c["role"]
            text = c["parts"][0]["text"][:80].replace("\n", " ")
            LOGGER.info("  [%d] %s: %s...", i, role, text)
        
        return contents

    def _build_response_payload(
        self,
        data: Dict[str, Any],
        store_names: List[str],
        metadata_filters: Optional[List[MetadataFilter]],
    ) -> Dict[str, Any]:
        text = self._extract_text(data)
        grounding_chunks = self._extract_grounding_chunks(data)
        return {
            "text": text or "검색 결과를 가져오지 못했습니다. 다시 시도해 주세요.",
            "grounding_chunks": grounding_chunks,
            "store_names": store_names,
            "applied_filters": [asdict(f) for f in (metadata_filters or [])],
        }

    async def _sleep_backoff(self, attempt: int) -> None:
        sleep_seconds = min(5.0, self.retry_backoff_seconds * (attempt + 1))
        try:
            await asyncio.sleep(sleep_seconds)
        except Exception:  # pragma: no cover
            pass

    def _extract_text(self, payload: dict[str, Any]) -> str:
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text
        candidates = payload.get("candidates") or []
        if not candidates:
            return ""
        first = candidates[0] or {}
        content = first.get("content") or {}
        parts = content.get("parts") or []
        if parts and isinstance(parts[0], dict):
            return str(parts[0].get("text") or "")
        try:
            return json.dumps(first)
        except Exception:  # pragma: no cover
            return ""

    def _extract_grounding_chunks(self, payload: dict[str, Any]) -> List[Any]:
        candidates = payload.get("candidates") or []
        if not candidates:
            return []
        first = candidates[0] or {}
        metadata = first.get("groundingMetadata") or first.get("grounding_metadata") or {}
        chunks = (
            metadata.get("groundingChunks")
            or metadata.get("grounding_chunks")
            or []
        )
        
        safe_chunks: List[Any] = []
        for chunk in chunks:
            # Handle Web Source (URI/Title)
            if isinstance(chunk, dict) and "web" in chunk:
                web = chunk.get("web", {})
                safe_chunks.append({
                    "uri": web.get("uri"),
                    "title": web.get("title"),
                    "type": "web"
                })
                continue

            if isinstance(chunk, (dict, list, str, int, float, bool)) or chunk is None:
                safe_chunks.append(chunk)
                continue
            try:
                safe_chunks.append(json.loads(json.dumps(chunk)))
            except Exception:  # pragma: no cover
                safe_chunks.append(str(chunk))
        return safe_chunks
