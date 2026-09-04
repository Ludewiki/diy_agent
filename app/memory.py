"""Governed cross-session travel memory backed by PostgreSQL and LangGraph Store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import re
from typing import Any, Iterable, Protocol
import uuid

from langgraph.store.base import BaseStore
from langgraph.store.postgres import PostgresStore
from sqlalchemy import func, or_, select
from sqlalchemy.engine import make_url

from .database import Database
from .models import MemoryStatus, MemoryType, TravelMemory, utc_now


logger = logging.getLogger(__name__)
_SENTENCE_SPLIT = re.compile(r"[。！？!?\\n]+")
_WORD_PATTERN = re.compile(r"[a-z0-9]+|[\\u3400-\\u9fff]")


def _postgres_conn_string(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("LangGraph PostgresStore 需要 PostgreSQL DATABASE_URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _namespace(user_id: uuid.UUID) -> tuple[str, ...]:
    return ("users", str(user_id), "travel_memories")


class MemoryStoreBackend(Protocol):
    def setup(self) -> None:
        ...

    def put(self, user_id: uuid.UUID, key: str, value: dict[str, Any]) -> None:
        ...

    def delete(self, user_id: uuid.UUID, key: str) -> None:
        ...

    def search(
        self,
        user_id: uuid.UUID,
        *,
        status: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        ...


class LangGraphPostgresStoreBackend:
    """Open short-lived Store connections while keeping framework details isolated."""

    def __init__(self, database_url: str) -> None:
        self.conn_string = _postgres_conn_string(database_url)

    def setup(self) -> None:
        with PostgresStore.from_conn_string(self.conn_string) as store:
            store.setup()

    def put(self, user_id: uuid.UUID, key: str, value: dict[str, Any]) -> None:
        with PostgresStore.from_conn_string(self.conn_string) as store:
            store.put(_namespace(user_id), key, value, index=False)

    def delete(self, user_id: uuid.UUID, key: str) -> None:
        with PostgresStore.from_conn_string(self.conn_string) as store:
            store.delete(_namespace(user_id), key)

    def search(
        self,
        user_id: uuid.UUID,
        *,
        status: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        with PostgresStore.from_conn_string(self.conn_string) as store:
            return [
                dict(item.value)
                for item in store.search(
                    _namespace(user_id),
                    filter={"status": status},
                    limit=limit,
                )
            ]


class DirectLangGraphStoreBackend:
    """Adapter for InMemoryStore in unit tests and local isolated callers."""

    def __init__(self, store: BaseStore) -> None:
        self.store = store

    def setup(self) -> None:
        return None

    def put(self, user_id: uuid.UUID, key: str, value: dict[str, Any]) -> None:
        self.store.put(_namespace(user_id), key, value, index=False)

    def delete(self, user_id: uuid.UUID, key: str) -> None:
        self.store.delete(_namespace(user_id), key)

    def search(
        self,
        user_id: uuid.UUID,
        *,
        status: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        return [
            dict(item.value)
            for item in self.store.search(
                _namespace(user_id),
                filter={"status": status},
                limit=limit,
            )
        ]


@dataclass(frozen=True)
class MemoryCandidate:
    normalized_key: str
    memory_type: str
    content: str
    confidence: float
    status: str
    metadata: dict[str, Any]


def _clean_content(value: Any) -> str:
    return " ".join(str(value or "").split())[:2000]


def _content_key(prefix: str, content: str) -> str:
    digest = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"[:255]


def _tokens(text: str) -> set[str]:
    return set(_WORD_PATTERN.findall(text.casefold()))


def extract_memory_candidates(
    prompt: str,
    planning_request: dict[str, Any] | None,
) -> list[MemoryCandidate]:
    """Conservatively extract explicit memories without an extra model call."""
    candidates: dict[str, MemoryCandidate] = {}
    request = planning_request or {}

    for interest in request.get("interests") or []:
        content = _clean_content(f"旅行时偏好：{interest}")
        if not content:
            continue
        key = _content_key("interest", content)
        candidates[key] = MemoryCandidate(
            normalized_key=key,
            memory_type=MemoryType.PREFERENCE.value,
            content=content,
            confidence=0.85,
            status=MemoryStatus.CANDIDATE.value,
            metadata={"extraction": "planning_form", "field": "interests"},
        )

    budget = _clean_content(request.get("budget"))
    if budget:
        content = f"常用旅行预算偏好候选：{budget}"
        key = _content_key("budget", content)
        candidates[key] = MemoryCandidate(
            normalized_key=key,
            memory_type=MemoryType.PREFERENCE.value,
            content=content,
            confidence=0.65,
            status=MemoryStatus.CANDIDATE.value,
            metadata={"extraction": "planning_form", "field": "budget"},
        )

    additional = _clean_content(request.get("additional_preferences"))
    if additional:
        content = f"旅行补充偏好候选：{additional}"
        key = _content_key("additional", content)
        candidates[key] = MemoryCandidate(
            normalized_key=key,
            memory_type=MemoryType.PREFERENCE.value,
            content=content,
            confidence=0.7,
            status=MemoryStatus.CANDIDATE.value,
            metadata={
                "extraction": "planning_form",
                "field": "additional_preferences",
            },
        )

    for sentence in _SENTENCE_SPLIT.split(prompt):
        content = _clean_content(sentence)
        if not content:
            continue
        stable = any(
            marker in content
            for marker in ("请记住", "以后", "总是", "一向", "我喜欢", "我不喜欢")
        )
        if not stable:
            continue
        rejected = any(
            marker in content
            for marker in ("我不喜欢", "不要", "避免", "拒绝", "不想")
        )
        memory_type = (
            MemoryType.REJECTED.value
            if rejected
            else MemoryType.PREFERENCE.value
        )
        key = _content_key(memory_type, content)
        candidates[key] = MemoryCandidate(
            normalized_key=key,
            memory_type=memory_type,
            content=content,
            confidence=0.98,
            status=MemoryStatus.CONFIRMED.value,
            metadata={"extraction": "explicit_statement"},
        )

    return list(candidates.values())


class MemoryService:
    def __init__(
        self,
        database: Database,
        backend: MemoryStoreBackend,
        *,
        recall_limit: int = 8,
        auto_extract: bool = True,
    ) -> None:
        self.database = database
        self.backend = backend
        self.recall_limit = recall_limit
        self.auto_extract = auto_extract

    def setup_store(self) -> None:
        self.backend.setup()

    @staticmethod
    def _store_value(memory: TravelMemory) -> dict[str, Any]:
        return {
            "id": str(memory.id),
            "memory_type": memory.memory_type,
            "content": memory.content,
            "confidence": memory.confidence,
            "status": memory.status,
            "source_message_id": (
                str(memory.source_message_id)
                if memory.source_message_id is not None
                else None
            ),
            "source_run_id": (
                str(memory.source_run_id)
                if memory.source_run_id is not None
                else None
            ),
            "expires_at": (
                memory.expires_at.isoformat()
                if memory.expires_at is not None
                else None
            ),
            "updated_at": memory.updated_at.isoformat(),
        }

    def _sync(self, memory: TravelMemory) -> None:
        try:
            if memory.status == MemoryStatus.DELETED.value:
                self.backend.delete(memory.user_id, str(memory.id))
            else:
                self.backend.put(
                    memory.user_id,
                    str(memory.id),
                    self._store_value(memory),
                )
        except Exception:
            logger.warning(
                "long-term memory Store synchronization failed",
                extra={
                    "event": "memory_store_sync_failed",
                    "request_id": str(memory.id),
                },
            )

    def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        include_candidates: bool = True,
        page: int = 1,
        page_size: int = 50,
    ) -> list[TravelMemory]:
        statuses = [MemoryStatus.CONFIRMED.value]
        if include_candidates:
            statuses.append(MemoryStatus.CANDIDATE.value)
        with self.database.session_factory() as session:
            return list(
                session.scalars(
                    select(TravelMemory)
                    .where(
                        TravelMemory.user_id == user_id,
                        TravelMemory.status.in_(statuses),
                    )
                    .order_by(TravelMemory.updated_at.desc(), TravelMemory.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )

    def counts_for_user(self, user_id: uuid.UUID) -> tuple[int, int]:
        with self.database.session_factory() as session:
            rows = session.execute(
                select(TravelMemory.status, func.count(TravelMemory.id))
                .where(
                    TravelMemory.user_id == user_id,
                    TravelMemory.status.in_(
                        [
                            MemoryStatus.CANDIDATE.value,
                            MemoryStatus.CONFIRMED.value,
                        ]
                    ),
                )
                .group_by(TravelMemory.status)
            )
            counts = {status: int(count) for status, count in rows}
        return (
            counts.get(MemoryStatus.CANDIDATE.value, 0),
            counts.get(MemoryStatus.CONFIRMED.value, 0),
        )

    def create_manual(
        self,
        user_id: uuid.UUID,
        content: str,
        memory_type: str,
    ) -> TravelMemory:
        cleaned = _clean_content(content)
        memory = TravelMemory(
            user_id=user_id,
            memory_type=memory_type,
            content=cleaned,
            normalized_key=f"manual:{uuid.uuid4().hex}",
            confidence=1.0,
            status=MemoryStatus.CONFIRMED.value,
            metadata_json={"extraction": "manual"},
        )
        with self.database.session_factory.begin() as session:
            session.add(memory)
            session.flush()
        self._sync(memory)
        return memory

    def extract_from_run(
        self,
        *,
        user_id: uuid.UUID,
        message_id: uuid.UUID,
        run_id: uuid.UUID,
        prompt: str,
        planning_request: dict[str, Any] | None,
    ) -> list[TravelMemory]:
        if not self.auto_extract:
            return []
        candidates = extract_memory_candidates(prompt, planning_request)
        memories: list[TravelMemory] = []
        with self.database.session_factory.begin() as session:
            for candidate in candidates:
                memory = session.scalar(
                    select(TravelMemory).where(
                        TravelMemory.user_id == user_id,
                        TravelMemory.normalized_key == candidate.normalized_key,
                    )
                )
                if memory is None:
                    memory = TravelMemory(
                        user_id=user_id,
                        normalized_key=candidate.normalized_key,
                        memory_type=candidate.memory_type,
                        content=candidate.content,
                        confidence=candidate.confidence,
                        status=candidate.status,
                        source_message_id=message_id,
                        source_run_id=run_id,
                        metadata_json=candidate.metadata,
                    )
                    session.add(memory)
                else:
                    if memory.status == MemoryStatus.DELETED.value:
                        # A user deletion is a tombstone: automatic extraction
                        # must not silently recreate the rejected memory.
                        continue
                    memory.content = candidate.content
                    memory.confidence = max(
                        float(memory.confidence),
                        candidate.confidence,
                    )
                    if candidate.status == MemoryStatus.CONFIRMED.value:
                        memory.status = MemoryStatus.CONFIRMED.value
                    memory.source_message_id = message_id
                    memory.source_run_id = run_id
                    memory.metadata_json = candidate.metadata
                    memory.updated_at = utc_now()
                session.flush()
                memories.append(memory)
        for memory in memories:
            self._sync(memory)
        return memories

    def update(
        self,
        user_id: uuid.UUID,
        memory_id: uuid.UUID,
        *,
        content: str | None,
        status: str | None,
    ) -> TravelMemory | None:
        with self.database.session_factory.begin() as session:
            memory = session.scalar(
                select(TravelMemory).where(
                    TravelMemory.id == memory_id,
                    TravelMemory.user_id == user_id,
                    TravelMemory.status != MemoryStatus.DELETED.value,
                )
            )
            if memory is None:
                return None
            if content is not None:
                memory.content = _clean_content(content)
                memory.confidence = 1.0
                metadata = dict(memory.metadata_json or {})
                metadata["edited_by_user"] = True
                memory.metadata_json = metadata
            if status is not None:
                memory.status = status
            memory.updated_at = utc_now()
            session.flush()
        self._sync(memory)
        return memory

    def delete(self, user_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
        with self.database.session_factory.begin() as session:
            memory = session.scalar(
                select(TravelMemory).where(
                    TravelMemory.id == memory_id,
                    TravelMemory.user_id == user_id,
                    TravelMemory.status != MemoryStatus.DELETED.value,
                )
            )
            if memory is None:
                return False
            memory.status = MemoryStatus.DELETED.value
            memory.updated_at = utc_now()
            session.flush()
        self._sync(memory)
        return True

    def recall(self, user_id: uuid.UUID, query: str) -> list[str]:
        now = utc_now()
        with self.database.session_factory() as session:
            records = list(
                session.scalars(
                    select(TravelMemory)
                    .where(
                        TravelMemory.user_id == user_id,
                        TravelMemory.status == MemoryStatus.CONFIRMED.value,
                        or_(
                            TravelMemory.expires_at.is_(None),
                            TravelMemory.expires_at > now,
                        ),
                    )
                    .order_by(TravelMemory.updated_at.desc())
                    .limit(100)
                )
            )
        for memory in records:
            self._sync(memory)

        values: Iterable[dict[str, Any]]
        try:
            values = self.backend.search(
                user_id,
                status=MemoryStatus.CONFIRMED.value,
                limit=100,
            )
        except Exception:
            logger.warning(
                "long-term memory Store recall failed; using business table",
                extra={
                    "event": "memory_store_recall_failed",
                    "request_id": str(user_id),
                },
            )
            values = [self._store_value(memory) for memory in records]

        query_tokens = _tokens(query)
        ranked: list[tuple[int, str, str]] = []
        for value in values:
            content = _clean_content(value.get("content"))
            if not content:
                continue
            expires_at = value.get("expires_at")
            if expires_at:
                try:
                    expires = datetime.fromisoformat(str(expires_at))
                    comparison_now = now if expires.tzinfo else now.replace(tzinfo=None)
                    if expires <= comparison_now:
                        continue
                except ValueError:
                    continue
            overlap = len(query_tokens & _tokens(content))
            updated_at = str(value.get("updated_at") or "")
            ranked.append((overlap, updated_at, content))
        ranked.sort(reverse=True)
        return [content for _, _, content in ranked[: self.recall_limit]]
