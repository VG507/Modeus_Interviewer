"""Embedding model client and vector storage shared by card search and interview context.

Vectors come from an OpenAI-format /embeddings endpoint, local Ollama by default,
and are stored as JSON text, or in pgvector's vector type where the extension exists.
"""

import hashlib
import json
import math
from typing import NamedTuple
from urllib.parse import urlsplit

import httpx
from sqlalchemy import cast, select, text
from sqlalchemy.types import UserDefinedType

from backend.db import CardEmbedding, TextEmbedding, insert_for, now

EMBED_BATCH = 32
# Two people telling the same kind of problem score at least this (bge-m3, measured with
# scripts/evaluate_embeddings.py): 11/11 twin cases kept, ~0.5 wrong cards above the line.
SIMILAR_MIN_SCORE = 0.6


class Vector(UserDefinedType):
    """pgvector's type, for casting JSON text to a vector in SQL; dimensions fix the size an index needs."""
    cache_ok = True

    def __init__(self, dimensions=None):
        self.dimensions = dimensions

    def get_col_spec(self, **kw):
        return f"vector({self.dimensions})" if self.dimensions else "vector"


def configured(settings):
    return bool(settings.embedding_base_url and settings.embedding_model)


def text_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def unit(vector):
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def similarity(left, right):
    """Cosine similarity of two unit vectors."""
    return sum(a * b for a, b in zip(left, right))


def embed(settings, texts, timeout=None):
    """Unit vectors for texts, in order. Raises RuntimeError("EMBEDDING_...") on failure."""
    if not configured(settings):
        raise RuntimeError("EMBEDDING_NOT_CONFIGURED")
    if not texts:
        return []
    local = urlsplit(settings.embedding_base_url).hostname in {"localhost", "127.0.0.1", "::1"}
    headers = {"Authorization": f"Bearer {settings.embedding_api_key}"} if settings.embedding_api_key else {}
    try:
        with httpx.Client(timeout=timeout or settings.embedding_timeout_seconds, trust_env=not local) as client:
            response = client.post(settings.embedding_base_url.rstrip("/") + "/embeddings", headers=headers,
                                   json={"model": settings.embedding_model, "input": list(texts)})
    except httpx.HTTPError as exc:
        raise RuntimeError("EMBEDDING_UNAVAILABLE") from exc
    if response.status_code != 200:
        raise RuntimeError(f"EMBEDDING_HTTP_ERROR: {response.status_code}")
    try:
        data = sorted(response.json()["data"], key=lambda item: item["index"])
        vectors = [unit([float(value) for value in item["embedding"]]) for item in data]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("EMBEDDING_INVALID_RESPONSE") from None
    if len(vectors) != len(texts):
        raise RuntimeError("EMBEDDING_INVALID_RESPONSE")
    return vectors


_PGVECTOR = {}


def pgvector_version(db):
    """pgvector's version as a tuple when the embedding columns use its type, else None (search runs in Python)."""
    bind = db.get_bind()
    key = str(bind.engine.url)
    if key not in _PGVECTOR:
        _PGVECTOR[key] = None
        if bind.dialect.name == "postgresql" and db.scalar(text(
            "select udt_name from information_schema.columns "
            "where table_name = 'card_embeddings' and column_name = 'embedding'"
        )) == "vector":
            version = db.scalar(text("select extversion from pg_extension where extname = 'vector'"))
            _PGVECTOR[key] = tuple(int(part) for part in version.split(".")[:3] if part.isdigit())
    return _PGVECTOR[key]


def pgvector_column(db):
    """True when the embedding columns are pgvector's type, so similarity can run in SQL."""
    return pgvector_version(db) is not None


def stored_vector(db, vector):
    value = json.dumps(vector)
    return cast(value, Vector()) if pgvector_column(db) else value


def case_vectors(db, model, card_ids):
    """{(card id, case index): vector} stored for these cards."""
    return {(card_id, index): json.loads(value) for card_id, index, value in db.execute(
        select(CardEmbedding.card_id, CardEmbedding.case_index, CardEmbedding.embedding).where(
            CardEmbedding.model == model, CardEmbedding.card_id.in_(card_ids)))}


def cached_vectors(db, model, texts):
    """{text: vector} for the texts whose vector is already stored; others are left out."""
    hashes = {text_hash(value): value for value in texts}
    return {hashes[digest]: json.loads(value) for digest, value in db.execute(
        select(TextEmbedding.text_hash, TextEmbedding.embedding).where(
            TextEmbedding.model == model, TextEmbedding.text_hash.in_(hashes)))}


def cache_texts(sessions, settings, texts):
    """Embed and store the texts that have no stored vector yet; returns how many were embedded."""
    texts = list(dict.fromkeys(texts))
    with sessions() as db:
        stored = cached_vectors(db, settings.embedding_model, texts)
    missing = [value for value in texts if value not in stored]
    for start in range(0, len(missing), EMBED_BATCH):
        batch = missing[start:start + EMBED_BATCH]
        vectors = embed(settings, batch)
        with sessions.begin() as db:
            for value, vector in zip(batch, vectors):
                db.execute(insert_for(db, TextEmbedding).values(
                    model=settings.embedding_model, text_hash=text_hash(value),
                    embedding=stored_vector(db, vector), created_at=now(),
                ).on_conflict_do_nothing(index_elements=["model", "text_hash"]))
    return len(missing)


class CaseVector(NamedTuple):
    """The current interview case as a vector, for matching stored texts and card cases by meaning."""
    model: str
    vector: list

    def scores(self, db, texts):
        """{text: similarity} for the texts that have a stored vector."""
        return {value: similarity(self.vector, stored) for value, stored in cached_vectors(db, self.model, texts).items()}

    def case_scores(self, db, card_ids):
        """{(card id, case index): similarity} for the cases of these cards that have a stored vector."""
        return {key: similarity(self.vector, stored) for key, stored in case_vectors(db, self.model, card_ids).items()}
