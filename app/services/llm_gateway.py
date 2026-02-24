from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional, Protocol

from openai import AsyncOpenAI

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMRequest:
    purpose: str
    system_prompt: str
    user_prompt: str
    temperature: float
    json_mode: bool
    timeout_ms: Optional[int] = None


@dataclass(frozen=True)
class LLMResponse:
    content: str
    provider: str
    model: str
    latency_ms: int
    attempts: int
    used_fallback: bool


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, req: LLMRequest) -> str:
        ...


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        name: str,
        api_key: Optional[str],
        base_url: Optional[str],
        model: str,
    ) -> None:
        self.name = name
        self.model = model
        self._api_key = api_key
        self._base_url = base_url
        self._client: Optional[AsyncOpenAI] = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    async def generate(self, req: LLMRequest) -> str:
        client = self._get_client()
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": req.system_prompt},
                {"role": "user", "content": req.user_prompt},
            ],
            temperature=req.temperature,
            response_format={"type": "json_object"} if req.json_mode else None,
        )
        return response.choices[0].message.content


class LLMTimeoutError(RuntimeError):
    pass


class LLMGateway:
    def __init__(
        self,
        *,
        providers: Dict[str, LLMProvider],
        default_route: List[str],
        purpose_routes: Optional[Dict[str, List[str]]] = None,
        local_timeout_ms: Optional[int] = None,
        cloud_timeout_ms_fields_only: Optional[int] = None,
    ) -> None:
        self.providers = providers
        self.default_route = default_route
        self.purpose_routes = purpose_routes or {}
        self.local_timeout_ms = local_timeout_ms
        self.cloud_timeout_ms_fields_only = cloud_timeout_ms_fields_only

    async def generate(self, req: LLMRequest, *, route: Optional[List[str]] = None) -> LLMResponse:
        if route is None:
            route = self.purpose_routes.get(req.purpose, self.default_route)
        used_fallback = False
        attempts = 0
        last_err: Optional[Exception] = None

        for idx, provider_name in enumerate(route):
            attempts += 1
            provider = self.providers.get(provider_name)
            if provider is None:
                last_err = ValueError(f"Unknown provider: {provider_name}")
                continue

            t0 = time.perf_counter()
            try:
                timeout_ms = req.timeout_ms
                if timeout_ms is None:
                    if provider.name == "local" and self.local_timeout_ms is not None:
                        timeout_ms = self.local_timeout_ms
                    elif (
                        provider.name != "local"
                        and req.purpose == "propose_fields_only"
                        and self.cloud_timeout_ms_fields_only is not None
                    ):
                        timeout_ms = self.cloud_timeout_ms_fields_only

                if timeout_ms is not None:
                    content = await asyncio.wait_for(provider.generate(req), timeout=timeout_ms / 1000)
                else:
                    content = await provider.generate(req)

                if req.json_mode:
                    parsed = json.loads(content)
                    if not isinstance(parsed, dict):
                        raise ValueError("LLM JSON mode must return a JSON object")

                latency_ms = int((time.perf_counter() - t0) * 1000)
                if idx > 0:
                    used_fallback = True

                logger.info(
                    "LLM done purpose=%s provider=%s model=%s json_mode=%s sys_chars=%s user_chars=%s ms=%s attempts=%s fallback=%s",
                    req.purpose,
                    provider.name,
                    provider.model,
                    req.json_mode,
                    len(req.system_prompt),
                    len(req.user_prompt),
                    latency_ms,
                    attempts,
                    used_fallback,
                )

                return LLMResponse(
                    content=content,
                    provider=provider.name,
                    model=provider.model,
                    latency_ms=latency_ms,
                    attempts=attempts,
                    used_fallback=used_fallback,
                )

            except asyncio.TimeoutError:
                last_err = LLMTimeoutError(f"Timeout provider={provider_name} purpose={req.purpose}")
            except Exception as exc:
                last_err = exc

        assert last_err is not None
        raise last_err


@lru_cache
def get_llm_gateway() -> LLMGateway:
    settings = get_settings()
    providers: Dict[str, LLMProvider] = {
        "gemini": OpenAICompatProvider(
            name="gemini",
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            model="gemini-2.0-flash",
        ),
        "deepseek": OpenAICompatProvider(
            name="deepseek",
            api_key=settings.deepseek_api_key,
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
        ),
        "openai": OpenAICompatProvider(
            name="openai",
            api_key=settings.openai_api_key,
            base_url=None,
            model="gpt-4o-mini",
        ),
    }

    if settings.llm_local_enabled and settings.llm_local_base_url and settings.llm_local_model:
        providers["local"] = OpenAICompatProvider(
            name="local",
            api_key=settings.llm_local_api_key,
            base_url=settings.llm_local_base_url,
            model=settings.llm_local_model,
        )

    cloud_primary = settings.llm_provider.lower()
    default_route = [cloud_primary]

    purpose_routes: Dict[str, List[str]] = {}
    if "local" in providers:
        for purpose in settings.llm_local_purposes:
            purpose_routes[purpose] = ["local", cloud_primary]

    return LLMGateway(
        providers=providers,
        default_route=default_route,
        purpose_routes=purpose_routes,
        local_timeout_ms=settings.llm_local_timeout_ms,
        cloud_timeout_ms_fields_only=settings.llm_cloud_timeout_ms_fields_only,
    )
