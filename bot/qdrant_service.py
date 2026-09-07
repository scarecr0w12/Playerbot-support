"""Qdrant vector-store service for RAG knowledge base and learned facts.

Collections are namespaced per guild:
  embeddings_{guild_id}  — knowledge base (web crawl + manual)
  facts_{guild_id}       — adaptive learned facts

Each point payload carries:
  name       str   — human-readable entry name
  text       str   — original chunk text
  source_url str   — origin URL (crawled entries)
  model      str   — embedding model used
  source     str   — "conversation" | "training" | "qa_pair" (facts only)
  approved   int   — 1 = active, 0 = hidden (facts only)
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)

logger = logging.getLogger(__name__)

_DISTANCE = Distance.COSINE
_DEFAULT_VECTOR_SIZE = 1536  # text-embedding-3-small default


def _col_kb(guild_id: int) -> str:
    return f"embeddings_{guild_id}"


def _col_facts(guild_id: int) -> str:
    return f"facts_{guild_id}"


def _new_id() -> str:
    return str(uuid.uuid4())


class QdrantService:
    def __init__(self) -> None:
        url = os.getenv("QDRANT_URL", "http://localhost:6333")
        api_key = os.getenv("QDRANT_API_KEY") or None
        self._client = AsyncQdrantClient(url=url, api_key=api_key)

    # ------------------------------------------------------------------
    # Internal: ensure a collection exists
    # ------------------------------------------------------------------

    async def _ensure_collection(self, name: str, vector_size: int) -> None:
        exists = await self._client.collection_exists(name)
        if not exists:
            await self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=vector_size, distance=_DISTANCE),
            )
            logger.info("Qdrant: created collection %s (dim=%d)", name, vector_size)

    # ------------------------------------------------------------------
    # Knowledge base (embeddings)
    # ------------------------------------------------------------------

    async def upsert_embedding(
        self,
        guild_id: int,
        point_id: str,
        vector: list[float],
        name: str,
        text: str,
        model: str,
        source_url: str = "",
    ) -> None:
        col = _col_kb(guild_id)
        await self._ensure_collection(col, len(vector))
        await self._client.upsert(
            collection_name=col,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "name": name,
                        "text": text,
                        "model": model,
                        "source_url": source_url,
                    },
                )
            ],
        )

    async def search_embeddings(
        self,
        guild_id: int,
        query_vector: list[float],
        top_n: int = 5,
        score_threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        col = _col_kb(guild_id)
        if not await self._client.collection_exists(col):
            return []
        response = await self._client.query_points(
            collection_name=col,
            query=query_vector,
            limit=top_n,
            score_threshold=score_threshold,
            with_payload=True,
        )
        return [
            {"score": r.score, **(r.payload or {})}
            for r in response.points
        ]

    async def delete_embedding(self, guild_id: int, point_id: str) -> None:
        col = _col_kb(guild_id)
        if not await self._client.collection_exists(col):
            return
        await self._client.delete(
            collection_name=col,
            points_selector=PointIdsList(points=[point_id]),
        )

    async def delete_embeddings_by_source(self, guild_id: int, source_url: str) -> None:
        col = _col_kb(guild_id)
        if not await self._client.collection_exists(col):
            return
        await self._client.delete(
            collection_name=col,
            points_selector=Filter(
                must=[FieldCondition(key="source_url", match=MatchValue(value=source_url))]
            ),
        )

    async def list_embeddings(self, guild_id: int, limit: int = 500) -> list[dict[str, Any]]:
        col = _col_kb(guild_id)
        if not await self._client.collection_exists(col):
            return []
        records, _ = await self._client.scroll(
            collection_name=col,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [{"id": str(r.id), **r.payload} for r in records]

    async def reset_embeddings(self, guild_id: int) -> None:
        col = _col_kb(guild_id)
        if await self._client.collection_exists(col):
            await self._client.delete_collection(col)

    # ------------------------------------------------------------------
    # Learned facts
    # ------------------------------------------------------------------

    async def upsert_fact(
        self,
        guild_id: int,
        point_id: str,
        vector: list[float],
        fact: str,
        source: str = "conversation",
        approved: int = 1,
    ) -> None:
        col = _col_facts(guild_id)
        await self._ensure_collection(col, len(vector))
        await self._client.upsert(
            collection_name=col,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "fact": fact,
                        "source": source,
                        "approved": approved,
                    },
                )
            ],
        )

    async def search_facts(
        self,
        guild_id: int,
        query_vector: list[float],
        top_n: int = 5,
        score_threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        col = _col_facts(guild_id)
        if not await self._client.collection_exists(col):
            return []
        response = await self._client.query_points(
            collection_name=col,
            query=query_vector,
            limit=top_n,
            score_threshold=score_threshold,
            with_payload=True,
            query_filter=Filter(
                must=[FieldCondition(key="approved", match=MatchValue(value=1))]
            ),
        )
        return [{"score": r.score, **(r.payload or {})} for r in response.points]

    async def delete_fact(self, guild_id: int, point_id: str) -> None:
        col = _col_facts(guild_id)
        if not await self._client.collection_exists(col):
            return
        await self._client.delete(
            collection_name=col,
            points_selector=PointIdsList(points=[point_id]),
        )

    async def set_fact_approved(self, guild_id: int, point_id: str, approved: int) -> None:
        col = _col_facts(guild_id)
        if not await self._client.collection_exists(col):
            return
        await self._client.set_payload(
            collection_name=col,
            payload={"approved": approved},
            points=[point_id],
        )

    async def list_facts(self, guild_id: int, limit: int = 500) -> list[dict[str, Any]]:
        col = _col_facts(guild_id)
        if not await self._client.collection_exists(col):
            return []
        records, _ = await self._client.scroll(
            collection_name=col,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [{"id": str(r.id), **r.payload} for r in records]

    async def reset_facts(self, guild_id: int) -> None:
        col = _col_facts(guild_id)
        if await self._client.collection_exists(col):
            await self._client.delete_collection(col)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def count_embeddings(self, guild_id: int) -> int:
        col = _col_kb(guild_id)
        if not await self._client.collection_exists(col):
            return 0
        info = await self._client.get_collection(col)
        return info.points_count or 0

    async def count_facts(self, guild_id: int) -> int:
        col = _col_facts(guild_id)
        if not await self._client.collection_exists(col):
            return 0
        info = await self._client.get_collection(col)
        return info.points_count or 0
