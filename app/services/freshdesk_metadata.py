from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from functools import lru_cache

from fastapi import HTTPException, status
import redis.asyncio as redis

from app.core.config import get_settings
from app.services.freshdesk_client import FreshdeskClient, FreshdeskClientError

logger = logging.getLogger(__name__)


@dataclass
class FreshdeskTicketFieldChoice:
    value: int
    label: str


@dataclass
class FreshdeskTicketField:
    name: str
    choices: Optional[List[FreshdeskTicketFieldChoice]] = None


@dataclass
class FreshdeskMetadataCache:
    status_map: Dict[int, str] = field(default_factory=dict)
    priority_map: Dict[int, str] = field(default_factory=dict)
    source_map: Dict[int, str] = field(default_factory=dict)
    type_map: Dict[str, str] = field(default_factory=dict)
    expires_at: datetime = field(default_factory=lambda: datetime.utcnow())
    category_map: Dict[int, str] = field(default_factory=dict)
    folder_map: Dict[int, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["expires_at"] = self.expires_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> FreshdeskMetadataCache:
        expires_at_str = data.get("expires_at")
        if expires_at_str:
            data["expires_at"] = datetime.fromisoformat(expires_at_str)
        # Ensure integer keys are actually integers (JSON converts keys to strings)
        if "status_map" in data:
            data["status_map"] = {int(k): v for k, v in data["status_map"].items()}
        if "priority_map" in data:
            data["priority_map"] = {int(k): v for k, v in data["priority_map"].items()}
        if "source_map" in data:
            data["source_map"] = {int(k): v for k, v in data["source_map"].items()}
        if "category_map" in data:
            data["category_map"] = {int(k): v for k, v in data["category_map"].items()}
        if "folder_map" in data:
            data["folder_map"] = {int(k): v for k, v in data["folder_map"].items()}
        return cls(**data)


class FreshdeskMetadataService:
    REDIS_KEY = "freshdesk:metadata:cache"

    def __init__(
        self,
        *,
        ttl_hours: int = 24,
        client: Optional[FreshdeskClient] = None,
        redis_client: Optional[redis.Redis] = None,
    ) -> None:
        self.ttl = timedelta(hours=ttl_hours)
        self.cache: Optional[FreshdeskMetadataCache] = None
        self.client = client
        self.redis = redis_client

    async def ensure_loaded(self) -> None:
        # 1. Check local memory cache
        if self.cache and self.cache.expires_at > datetime.utcnow():
            return

        # 2. Check Redis cache
        if self.redis:
            if await self._load_from_redis():
                return

        # 3. Fetch from API
        await self._load_metadata()

    async def _load_from_redis(self) -> bool:
        try:
            data = await self.redis.get(self.REDIS_KEY)
            if not data:
                return False
            cache_dict = json.loads(data)
            cache = FreshdeskMetadataCache.from_dict(cache_dict)
            if cache.expires_at > datetime.utcnow():
                self.cache = cache
                logger.info("Loaded Freshdesk metadata from Redis")
                return True
        except Exception as e:
            logger.warning(f"Failed to load metadata from Redis: {e}")
        return False

    async def _save_to_redis(self, cache: FreshdeskMetadataCache) -> None:
        if not self.redis:
            return
        try:
            data = json.dumps(cache.to_dict())
            # TTL set to match internal expiry + buffer
            ttl_seconds = int(self.ttl.total_seconds())
            await self.redis.setex(self.REDIS_KEY, ttl_seconds, data)
        except Exception as e:
            logger.warning(f"Failed to save metadata to Redis: {e}")

    async def _load_metadata(self) -> None:
        settings = get_settings()
        client = self.client
        if client is None:
            if not settings.freshdesk_domain or not settings.freshdesk_api_key:
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Freshdesk API 설정이 필요합니다")
            client = FreshdeskClient(settings.freshdesk_domain, settings.freshdesk_api_key)
            self.client = client
        logger.info("Loading Freshdesk metadata from API")
        try:
            ticket_fields, categories = await asyncio.gather(
                client.get_ticket_fields(),
                client.get_categories(),
            )
            folders_nested = await asyncio.gather(
                *[client.get_folders(category["id"]) for category in categories]
            )
        except FreshdeskClientError as exc:  # pragma: no cover - network errors
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        cache = FreshdeskMetadataCache()
        for ticket_field in ticket_fields:
            choices = ticket_field.get("choices")
            if not choices:
                continue
            mapping = {int(choice["value"]): choice["label"] for choice in choices}
            if ticket_field.get("name") == "status":
                cache.status_map = mapping
            elif ticket_field.get("name") == "priority":
                cache.priority_map = mapping
        cache.category_map = {int(cat["id"]): cat["name"] for cat in categories}
        for category, folder_list in zip(categories, folders_nested):
            cache.folder_map.update({int(folder["id"]): folder for folder in folder_list})
        cache.expires_at = datetime.utcnow() + self.ttl
        self.cache = cache
        await self._save_to_redis(cache)

    async def resolve_priority_label(self, label: str) -> Optional[int]:
        await self.ensure_loaded()
        normalized = label.strip().lower()
        reverse = {name.lower(): value for value, name in self.cache.priority_map.items()}
        return reverse.get(normalized)

    async def resolve_status_label(self, label: str) -> Optional[int]:
        await self.ensure_loaded()
        normalized = label.strip().lower()
        reverse = {name.lower(): value for value, name in self.cache.status_map.items()}
        return reverse.get(normalized)

    async def list_priority_labels(self) -> List[str]:
        await self.ensure_loaded()
        return list(self.cache.priority_map.values())

    async def list_status_labels(self) -> List[str]:
        await self.ensure_loaded()
        return list(self.cache.status_map.values())

    async def resolve_category_id(self, label: str) -> Optional[int]:
        await self.ensure_loaded()
        normalized = label.strip().lower()
        for category_id, name in self.cache.category_map.items():
            if name.strip().lower() == normalized:
                return category_id
        return None

    async def list_categories(self) -> List[str]:
        await self.ensure_loaded()
        return list(self.cache.category_map.values())

    async def resolve_folder_id(self, label: str, category_id: Optional[int] = None) -> Optional[int]:
        await self.ensure_loaded()
        normalized = label.strip().lower()
        for folder_id, folder in self.cache.folder_map.items():
            if category_id and folder.get("category_id") != category_id:
                continue
            name = folder.get("name", "").strip().lower()
            if name == normalized:
                return folder_id
        return None


@lru_cache
def get_freshdesk_metadata_service() -> FreshdeskMetadataService:
    settings = get_settings()
    redis_client = None
    if settings.redis_url:
        try:
            redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        except Exception as e:
            logger.warning(f"Failed to connect to Redis for metadata service: {e}")
    
    return FreshdeskMetadataService(redis_client=redis_client)
