"""Search published cards by meaning and keep their vectors current.

Each case of a card (its title, task, actions and decisions) is one vector. A stored
vector is reused while the case text and the model stay the same. Duties of fresh
vacancy samples are embedded too, so interview questions can match them by meaning.
"""

import asyncio
import json
import logging

from sqlalchemy import Float, cast, delete, func, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import load_only

from backend.db import CardEmbedding, KnowledgeCard, VacancyCollection, insert_for, now
from backend.knowledge import cases_of, published_cards
from backend.vacancy_context import keywords
from backend.vacancy_parser import skill_hits
from backend.vacancy_samples import fresh_vacancy_samples, trudvsem_samples
from backend.vectors import (
    EMBED_BATCH, SIMILAR_MIN_SCORE, CaseVector, Vector, cache_texts, configured, embed, pgvector_column, similarity,
    stored_vector, text_hash, unit,
)

logger = logging.getLogger(__name__)
EMBEDDED_SECTIONS = ("task", "actions", "decisions")
INDEX_EVERY_SECONDS = 300
# The interview waits at most this long for the case vector, then matches by words only.
CASE_VECTOR_TIMEOUT_SECONDS = 5
# Lowest similarity of a search hit, measured for bge-m3 with scripts/evaluate_embeddings.py:
# the right card stays for 26/28 queries with ~2 wrong cards above the line.
SEARCH_MIN_SCORE = 0.5
FACT_SECTIONS = {"task": "task", "action": "actions", "decision": "decisions"}


def case_text(case, sections=EMBEDDED_SECTIONS):
    """Title and facts of one case, or "" when the case has no facts (a multi-case wrapper)."""
    facts = [fact["text"].strip() for section in sections for fact in case.get(section) or () if fact["text"].strip()]
    return "\n".join([case.get("title", "").strip(), *facts]).strip() if facts else ""


def card_texts(content, sections=EMBEDDED_SECTIONS):
    """(case index, text) for every case of a card that has facts; index 0 is the card itself."""
    return [(index, text) for index, case in enumerate(cases_of(content)) if (text := case_text(case, sections))]


def interview_case(title, facts):
    """The current interview case shaped like a card case, so its text matches what cards store."""
    case = {"title": title}
    for fact in facts:
        if fact["field"] in FACT_SECTIONS:
            case.setdefault(FACT_SECTIONS[fact["field"]], []).append({"text": fact["text"]})
    return case


def case_vector(settings, title, facts):
    """The interview case so far as a CaseVector, or None when there is no task yet or the model is unavailable."""
    if not configured(settings) or not any(fact["field"] == "task" for fact in facts):
        return None
    try:
        vector = embed(settings, [case_text(interview_case(title, facts))], timeout=CASE_VECTOR_TIMEOUT_SECONDS)[0]
        return CaseVector(settings.embedding_model, vector)
    except RuntimeError as exc:
        logger.warning("Interview context falls back to words: %s", exc)
        return None


def index_cards(sessions, settings, card_ids=None):
    """Embed published cases whose text or model changed and drop vectors of removed cases.

    Returns the number of cases embedded. Raises RuntimeError("EMBEDDING_...") when the model fails.
    """
    with sessions() as db:
        query = published_cards().with_only_columns(KnowledgeCard.id, KnowledgeCard.content)
        if card_ids is not None:
            query = query.where(KnowledgeCard.id.in_(card_ids))
        cards = db.execute(query).all()
        stored = {(row.card_id, row.case_index): (row.model, row.text_hash) for row in db.execute(
            select(CardEmbedding.card_id, CardEmbedding.case_index, CardEmbedding.model, CardEmbedding.text_hash)
            .where(CardEmbedding.card_id.in_([card_id for card_id, _ in cards]))
        )}
    current = {(card_id, index): body for card_id, content in cards for index, body in card_texts(content)}
    pending = [(key, body) for key, body in current.items()
               if stored.get(key) != (settings.embedding_model, text_hash(body))]
    removed = [key for key in stored if key not in current]
    vectors = []
    for start in range(0, len(pending), EMBED_BATCH):
        vectors += embed(settings, [body for _, body in pending[start:start + EMBED_BATCH]])
    with sessions.begin() as db:
        if removed:
            db.execute(delete(CardEmbedding).where(tuple_(CardEmbedding.card_id, CardEmbedding.case_index).in_(removed)))
        for ((card_id, index), body), vector in zip(pending, vectors):
            values = {"model": settings.embedding_model, "text_hash": text_hash(body),
                      "embedding": stored_vector(db, vector), "created_at": now()}
            db.execute(insert_for(db, CardEmbedding).values(card_id=card_id, case_index=index, **values)
                       .on_conflict_do_update(index_elements=["card_id", "case_index"], set_=values))
    return len(pending)


def index_vacancy_duties(sessions, settings):
    """Embed the duties of fresh vacancy samples of approved professions; returns how many were new."""
    duties = []
    with sessions() as db:
        collections = db.scalars(select(VacancyCollection).where(VacancyCollection.approved.is_(True)).options(load_only(
            VacancyCollection.profession_id, VacancyCollection.cards, VacancyCollection.stats,
            VacancyCollection.updated_at, VacancyCollection.config_version,
        )))
        current = now()
        for collection in collections:
            trudvsem = trudvsem_samples(db, collection.profession_id).get(collection.profession_id)
            for cards, _ in fresh_vacancy_samples(collection, trudvsem, current):
                duties += [task for card in cards for task in card["tasks"]]
    return cache_texts(sessions, settings, duties)


def nearest(db, model, vector, candidates, limit):
    """[(card id, score)] of the cards among candidates (a select of ids) closest to vector, best case per card."""
    query = select(CardEmbedding.card_id).where(CardEmbedding.model == model, CardEmbedding.card_id.in_(candidates))
    if pgvector_column(db):
        distance = CardEmbedding.embedding.op("<=>", return_type=Float)(cast(json.dumps(vector), Vector()))
        score = func.max(1 - distance)
        return [tuple(row) for row in db.execute(
            query.add_columns(score).group_by(CardEmbedding.card_id).order_by(score.desc(), CardEmbedding.card_id)
            .limit(limit)
        )]
    best = {}
    for card_id, stored in db.execute(query.add_columns(CardEmbedding.embedding)):
        best[card_id] = max(best.get(card_id, -1.0), similarity(vector, json.loads(stored)))
    return sorted(best.items(), key=lambda item: (-item[1], item[0]))[:limit]


def similar_cards(db, model, card_id, candidates, limit):
    """Cards closest to any case of card_id, from its stored vectors; [] when it has none."""
    own = [json.loads(value) for value in db.scalars(select(CardEmbedding.embedding).where(
        CardEmbedding.card_id == card_id, CardEmbedding.model == model))]
    best = {}
    for vector in own:
        for other, score in nearest(db, model, vector, candidates.where(KnowledgeCard.id != card_id), limit):
            best[other] = max(best.get(other, -1.0), score)
    return sorted(best.items(), key=lambda item: (-item[1], item[0]))[:limit]


def terms(text):
    """Word stems and canonical skills: what search by words compares."""
    return keywords(text) | {skill for skill, _ in skill_hits(text)}


def word_matches(db, query, candidates, limit):
    wanted = terms(query)
    scored = []
    for card_id, body in db.execute(select(KnowledgeCard.id, KnowledgeCard.text).where(KnowledgeCard.id.in_(candidates))):
        if overlap := len(wanted & terms(body)):
            scored.append((card_id, overlap / len(wanted)))
    return sorted(scored, key=lambda item: (-item[1], item[0]))[:limit]


def search_cards(db, settings, query, candidates, limit):
    """([(card id, score)], mode): by meaning, or by words while the embedding model is unavailable."""
    if configured(settings):
        try:
            vector = embed(settings, [query])[0]
        except RuntimeError as exc:
            logger.warning("Search falls back to words: %s", exc)
        else:
            return [(card_id, score) for card_id, score in nearest(db, settings.embedding_model, vector, candidates, limit)
                    if score >= SEARCH_MIN_SCORE], "meaning"
    return word_matches(db, query, candidates, limit), "words"


def refresh_index(sessions, settings, card_ids=None):
    try:
        index_cards(sessions, settings, card_ids)
        if card_ids is None:
            index_vacancy_duties(sessions, settings)
    except (RuntimeError, SQLAlchemyError) as exc:
        logger.warning("Embedding index failed: %s", exc if isinstance(exc, RuntimeError) else type(exc).__name__)


async def schedule_card_index(sessions, settings):
    """Embed newly published cards and fresh vacancy duties every few minutes; a model that is down is retried."""
    while True:
        await asyncio.to_thread(refresh_index, sessions, settings)
        await asyncio.sleep(INDEX_EVERY_SECONDS)
