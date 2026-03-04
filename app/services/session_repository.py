from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
from typing import Any, Dict, Optional

import redis.asyncio as redis

from app.core.config import get_settings
from app.models.analyzer import AnalyzerResult

logger = logging.getLogger(__name__)


SessionRecord = Dict[str, Any]


class SessionRepository(ABC):
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds

    @abstractmethod
    async def save(self, record: SessionRecord) -> SessionRecord:
        ...

    @abstractmethod
    async def get(self, session_id: str) -> Optional[SessionRecord]:
        ...

    @abstractmethod
    async def append_question(self, session_id: str, question: str) -> Optional[SessionRecord]:
        ...

    @abstractmethod
    async def append_turn(self, session_id: str, question: str, answer: str) -> Optional[SessionRecord]:
        """Append a complete conversation turn (user question + model answer)."""
        ...

    @abstractmethod
    async def record_analyzer_result(self, session_id: str, result: AnalyzerResult) -> None:
        ...

    def normalize(self, payload: Dict[str, Any]) -> SessionRecord:
        now = datetime.now(timezone.utc).isoformat()
        record: SessionRecord = dict(payload)
        record.setdefault("createdAt", now)
        record.setdefault("updatedAt", record.get("createdAt", now))
        record.setdefault("questionHistory", [])
        return record


class InMemorySessionRepository(SessionRepository):
    def __init__(self, ttl_seconds: int) -> None:
        super().__init__(ttl_seconds)
        self._data: Dict[str, SessionRecord] = {}
        self._expires: Dict[str, datetime] = {}

    def _purge(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [session_id for session_id, exp in self._expires.items() if exp <= now]
        for session_id in expired:
            self._data.pop(session_id, None)
            self._expires.pop(session_id, None)

    def _touch(self, session_id: str) -> None:
        self._expires[session_id] = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)

    async def save(self, record: SessionRecord) -> SessionRecord:
        self._purge()
        session_id = record["sessionId"]
        record = self.normalize(record)
        self._data[session_id] = record
        self._touch(session_id)
        return record

    async def get(self, session_id: str) -> Optional[SessionRecord]:
        self._purge()
        record = self._data.get(session_id)
        if record:
            self._touch(session_id)
        return record

    async def append_question(self, session_id: str, question: str) -> Optional[SessionRecord]:
        record = await self.get(session_id)
        if not record:
            return None
        history = record.setdefault("questionHistory", [])
        history.append(question)
        record["updatedAt"] = datetime.now(timezone.utc).isoformat()
        await self.save(record)
        return record

    async def append_turn(self, session_id: str, question: str, answer: str) -> Optional[SessionRecord]:
        record = await self.get(session_id)
        if not record:
            return None
        turns = record.setdefault("conversationHistory", [])
        turns.append({"role": "user", "text": question})
        turns.append({"role": "model", "text": answer})
        # Keep last 10 turns (5 Q&A pairs) to avoid context overflow
        if len(turns) > 10:
            record["conversationHistory"] = turns[-10:]
        record["updatedAt"] = datetime.now(timezone.utc).isoformat()
        await self.save(record)
        return record

    async def record_analyzer_result(self, session_id: str, result: AnalyzerResult) -> None:
        record = await self.get(session_id)
        if not record:
            return
        responses = record.setdefault("analyzerResponses", [])
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "filters": [asdict(filter_) for filter_ in result.filters],
            "summaries": result.summaries,
            "confidence": result.confidence,
            "clarificationNeeded": result.clarification_needed,
            "clarification": asdict(result.clarification) if result.clarification else None,
        }
        responses.append(snapshot)
        record["knownContext"] = result.known_context
        if result.clarification_needed and result.clarification:
            record["clarificationState"] = {
                "clarification": asdict(result.clarification),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
        else:
            record.pop("clarificationState", None)
        await self.save(record)


class RedisSessionRepository(SessionRepository):
    def __init__(self, redis_client: redis.Redis, prefix: str, ttl_seconds: int) -> None:
        super().__init__(ttl_seconds)
        self.client = redis_client
        self.prefix = prefix

    def _key(self, session_id: str) -> str:
        return f"{self.prefix}:{session_id}"

    async def save(self, record: SessionRecord) -> SessionRecord:
        record = self.normalize(record)
        key = self._key(record["sessionId"])
        await self.client.setex(key, self.ttl_seconds, json.dumps(record))
        return record

    async def get(self, session_id: str) -> Optional[SessionRecord]:
        raw = await self.client.get(self._key(session_id))
        if not raw:
            return None
        record = json.loads(raw)
        # touch TTL
        await self.client.expire(self._key(session_id), self.ttl_seconds)
        return record

    async def append_question(self, session_id: str, question: str) -> Optional[SessionRecord]:
        record = await self.get(session_id)
        if not record:
            return None
        history = record.setdefault("questionHistory", [])
        history.append(question)
        record["updatedAt"] = datetime.now(timezone.utc).isoformat()
        await self.save(record)
        return record

    async def append_turn(self, session_id: str, question: str, answer: str) -> Optional[SessionRecord]:
        record = await self.get(session_id)
        if not record:
            return None
        turns = record.setdefault("conversationHistory", [])
        turns.append({"role": "user", "text": question})
        turns.append({"role": "model", "text": answer})
        # Keep last 10 turns (5 Q&A pairs) to avoid context overflow
        if len(turns) > 10:
            record["conversationHistory"] = turns[-10:]
        record["updatedAt"] = datetime.now(timezone.utc).isoformat()
        await self.save(record)
        return record

    async def record_analyzer_result(self, session_id: str, result: AnalyzerResult) -> None:
        record = await self.get(session_id)
        if not record:
            return
        responses = record.setdefault("analyzerResponses", [])
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "filters": [asdict(filter_) for filter_ in result.filters],
            "summaries": result.summaries,
            "confidence": result.confidence,
            "clarificationNeeded": result.clarification_needed,
            "clarification": asdict(result.clarification) if result.clarification else None,
        }
        responses.append(snapshot)
        record["knownContext"] = result.known_context
        if result.clarification_needed and result.clarification:
            record["clarificationState"] = {
                "clarification": asdict(result.clarification),
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
        else:
            record.pop("clarificationState", None)
        await self.save(record)


_repo_instance: Optional[SessionRepository] = None


async def get_session_repository() -> SessionRepository:
    global _repo_instance
    if _repo_instance:
        return _repo_instance

    settings = get_settings()
    ttl_seconds = settings.session_ttl_minutes * 60
    if settings.redis_url:
        try:
            client = redis.from_url(settings.redis_url, decode_responses=True)
            await client.ping()
            _repo_instance = RedisSessionRepository(client, settings.redis_session_prefix, ttl_seconds)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "Redis 연결 실패, 인메모리 세션 저장소 사용: %s (redis_url 설정됨)",
                exc,
                exc_info=False,
            )
            _repo_instance = InMemorySessionRepository(ttl_seconds)
    else:
        _repo_instance = InMemorySessionRepository(ttl_seconds)
    return _repo_instance
