from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Iterable, List, Optional

from app.core.config import get_settings
from app.models.metadata import MetadataFilter

LOGGER = logging.getLogger(__name__)


def get_gemini_client() -> "GeminiClient":
    """싱글톤 GeminiClient 인스턴스 반환."""
    settings = get_settings()
    return GeminiClient(
        api_key=settings.gemini_api_key,
        primary_model=settings.gemini_primary_model,
        fallback_model=settings.gemini_fallback_model,
    )


def _build_metadata_expression(filters: Optional[Iterable[MetadataFilter]]) -> Optional[str]:
    if not filters:
        return None
    operator_map = {
        "EQUALS": "=",
        "GREATER_THAN": ">",
        "LESS_THAN": "<",
    }
    expressions: List[str] = []
    for metadata in filters:
        if not metadata.key or metadata.value is None:
            continue
        if metadata.operator == "IN":
            values = [value.strip() for value in metadata.value.split(",") if value.strip()]
            if not values:
                continue
            if len(values) == 1:
                escaped = values[0].replace('"', '\\"')
                expressions.append(f'{metadata.key} = "{escaped}"')
            else:
                clauses = []
                for value in values:
                    escaped_value = value.replace('"', '\\"')
                    clauses.append(f'{metadata.key} = "{escaped_value}"')
                expressions.append("(" + " OR ".join(clauses) + ")")
            continue
        operator = operator_map.get(metadata.operator or "EQUALS", "=")
        escaped_value = metadata.value.replace('"', '\\"')
        expressions.append(f'{metadata.key} {operator} "{escaped_value}"')

    if not expressions:
        return None
    return " AND ".join(expressions)


class GeminiClientError(RuntimeError):
    pass


class GeminiRetryableError(GeminiClientError):
    """Raised when a Gemini request can be retried safely."""
    pass


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        primary_model: str,
        fallback_model: Optional[str] = None,
    ) -> None:
        if not api_key:
            raise GeminiClientError("Gemini API key is required")
        if not primary_model:
            raise GeminiClientError("Gemini model name is required")

        try:
            # NOTE: google-genai 패키지는 `google.genai` 네임스페이스로 제공됩니다.
            # 환경에 따라 `google` 네임스페이스 패키지 충돌이 발생할 수 있어
            # 모듈 import 시점에 바로 터지지 않도록 지연 import를 사용합니다.
            from google import genai  # type: ignore
        except ImportError as exc:
            raise GeminiClientError(
                "google-genai 패키지가 필요합니다. 가상환경(venv)에서 의존성을 설치한 뒤 다시 실행해 주세요."
            ) from exc

        self.client = genai.Client(api_key=api_key)
        self.models = [primary_model]
        if fallback_model and fallback_model not in self.models:
            self.models.append(fallback_model)

    def search(
        self,
        *,
        query: str,
        store_names: List[str],
        metadata_filters: Optional[List[MetadataFilter]] = None,
        conversation_history: Optional[List[str]] = None,
    ) -> dict[str, Any]:
        if not store_names:
            raise GeminiClientError("At least one file search store name is required")

        history_parts = [
            {
                "role": "user",
                "parts": [{"text": entry}],
            }
            for entry in (conversation_history or [])
            if entry.strip()
        ]
        contents = history_parts + [
            {
                "role": "user",
                "parts": [{"text": query}],
            }
        ]

        last_error: Optional[Exception] = None
        for model_name in self.models:
            try:
                # TODO: File Search 툴 구성은 google-genai 버전에 따라 달라지므로
                # 현재 버전에서는 우선 기본 텍스트 생성만 사용한다.
                # metadata_expression은 추후 시스템 프롬프트 등에 녹이는 방향으로 개선 가능.
                response = self.client.models.generate_content(
                    model=model_name,
                    contents=contents,
                )
                text = getattr(response, "text", None)
                if not text and getattr(response, "candidates", None):
                    candidate = response.candidates[0]
                    parts = getattr(candidate, "content", None)
                    if parts and getattr(parts, "parts", None):
                        first_part = parts.parts[0]
                        text = getattr(first_part, "text", "")
                grounding_chunks: List[Any] = []
                chunks = None
                if getattr(response, "candidates", None):
                    candidate = response.candidates[0]
                    metadata = getattr(candidate, "grounding_metadata", None)
                    chunks = getattr(metadata, "grounding_chunks", None)
                if chunks:
                    for chunk in chunks:
                        if hasattr(chunk, "to_dict"):
                            grounding_chunks.append(chunk.to_dict())
                        else:
                            try:
                                grounding_chunks.append(json.loads(json.dumps(chunk)))
                            except Exception:
                                grounding_chunks.append(str(chunk))
                return {
                    "text": text or "검색 결과를 가져오지 못했습니다. 다시 시도해 주세요.",
                    "grounding_chunks": grounding_chunks,
                    "store_names": store_names,
                    "applied_filters": metadata_filters or [],
                }
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.warning("Gemini model %s failed: %s", model_name, exc)
                last_error = exc
        raise GeminiClientError("Gemini 검색 실패") from last_error

    async def generate_content_stream(
        self,
        *,
        contents: Any,
        config: Optional[dict] = None,
    ) -> AsyncGenerator[Any, None]:
        """스트리밍 콘텐츠 생성."""
        model_name = self.models[0]
        
        try:
            # google-genai의 동기 스트리밍을 비동기로 래핑
            response = self.client.models.generate_content_stream(
                model=model_name,
                contents=contents,
                config=config,
            )
            
            for chunk in response:
                yield chunk
                
        except Exception as exc:
            LOGGER.error("Gemini streaming failed: %s", exc)
            raise GeminiClientError("Gemini 스트리밍 실패") from exc

    def generate_content(
        self,
        *,
        contents: Any,
        config: Optional[dict] = None,
    ) -> Any:
        """동기 콘텐츠 생성."""
        model_name = self.models[0]
        
        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            return response
                
        except Exception as exc:
            LOGGER.error("Gemini generation failed: %s", exc)
            raise GeminiClientError("Gemini 생성 실패") from exc
