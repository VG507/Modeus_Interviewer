"""Search published cards by meaning and keep their vectors current.

Each case of a card (its title, task, actions and decisions) is one vector. A stored
vector is reused while the case text and the model stay the same. Duties of fresh
vacancy samples are embedded too, so interview questions can match them by meaning.
"""

import asyncio
import json
import logging

from sqlalchemy import Float, String, cast, delete, literal, select, text, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import load_only

from backend.db import CardEmbedding, KnowledgeCard, VacancyCollection, insert_for, now
from backend.knowledge import SECTIONS, cases_of, published_cards
from backend.vacancy_context import keywords
from backend.vacancy_parser import skill_hits
from backend.vacancy_samples import fresh_vacancy_samples, trudvsem_samples
from backend.vectors import (
    EMBED_BATCH, SIMILAR_MIN_SCORE, CaseVector, Vector, cache_texts, configured, embed, pgvector_version, similarity,
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
# Below this a hit must also share a word with the query, so an off-topic query ("облёт БПЛА" with no such
# card) finds nothing instead of a stray card at 0.52. Wrong cards shown fall from 2.3 to 0.7 per query;
# the cost is 2 of 28 right cards that matched by meaning alone (an English query, "интерфейс лагает").
SEARCH_SURE_SCORE = 0.55
# Hits rank by meaning plus this much per share of the query's words a card names: hit@1 23 -> 24
# of 28 and MRR 0.89 -> 0.91 on the same queries; 0.15 and up loses the gain.
WORD_WEIGHT = 0.1
# Cases ranked by meaning before words rerank them, and how many cases a card may take in a ranking.
SEARCH_POOL = 50
CASES_PER_CARD = 5
# HNSW candidates per query (pgvector's default is 40): 100 gave recall@10 0.98 at 2.7 ms on 20k rows
# in scripts/benchmark_vector_index.py, against 47.7 ms for the exact scan.
INDEX_EF_SEARCH = 100
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


def nearest_cases_statement(model, vector, candidates, limit):
    """The pgvector query for nearest_cases, shaped so the HNSW index of migration 0027 can serve it.

    The index covers `embedding::vector(N)` where `model = '<model>'`, so the query casts the same
    way and names the model as a literal: a bound value would hide the match from a generic plan.
    """
    dimensions = Vector(len(vector))
    distance = cast(CardEmbedding.embedding, dimensions).op("<=>", return_type=Float)(cast(json.dumps(vector), dimensions))
    return (select(CardEmbedding.card_id, CardEmbedding.case_index, 1 - distance)
            .where(CardEmbedding.model == literal(model, String, literal_execute=True),
                   CardEmbedding.card_id.in_(candidates))
            .order_by(distance).limit(limit))


def nearest_cases(db, model, vector, candidates, limit):
    """[(card id, case index, score)] of the cases of candidate cards (a select of ids) closest to vector."""
    if version := pgvector_version(db):
        # The index returns at most ef_search rows; a relaxed scan keeps going past rows the filter drops.
        db.execute(text("select set_config('hnsw.ef_search', :value, true)"), {"value": str(max(INDEX_EF_SEARCH, limit))})
        if version >= (0, 8):
            db.execute(text("select set_config('hnsw.iterative_scan', 'relaxed_order', true)"))
        scored = [tuple(row) for row in db.execute(nearest_cases_statement(model, vector, candidates, limit))]
    else:
        query = select(CardEmbedding.card_id, CardEmbedding.case_index, CardEmbedding.embedding).where(
            CardEmbedding.model == model, CardEmbedding.card_id.in_(candidates))
        scored = [(card_id, index, similarity(vector, json.loads(stored))) for card_id, index, stored in db.execute(query)]
    return sorted(scored, key=lambda item: (-item[2], item[0], item[1]))[:limit]


def best_per_card(cases):
    """{card id: (score, case index)} of each card's first, so closest, case in a ranked case list."""
    best = {}
    for card_id, index, score in cases:
        best.setdefault(card_id, (score, index))
    return best


def nearest(db, model, vector, candidates, limit):
    """[(card id, score)] of the cards closest to vector, best case per card."""
    best = best_per_card(nearest_cases(db, model, vector, candidates, limit * CASES_PER_CARD))
    return sorted(((card_id, score) for card_id, (score, _) in best.items()), key=lambda item: (-item[1], item[0]))[:limit]


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


def word_share(wanted, content):
    """Share of the query's terms that a card names anywhere in its cases."""
    if not wanted:
        return 0.0
    return len(wanted & terms("\n".join(body for _, body in card_texts(content, SECTIONS)))) / len(wanted)


def search_passes(score, share):
    """Whether a card with this similarity and share of the query's words is a search hit."""
    return score >= SEARCH_SURE_SCORE or (score >= SEARCH_MIN_SCORE and share > 0)


def search_cards(db, settings, query, candidates, limit):
    """([(card id, score, case index)], mode), best first.

    By meaning, reranked by shared words, among the cards search_passes lets through; the case index
    names the case that matched. While the embedding model is unavailable, by words alone with no case index.
    """
    wanted = terms(query)
    if configured(settings):
        try:
            vector = embed(settings, [query])[0]
        except RuntimeError as exc:
            logger.warning("Search falls back to words: %s", exc)
        else:
            best = {card_id: hit for card_id, hit in best_per_card(nearest_cases(
                db, settings.embedding_model, vector, candidates, SEARCH_POOL)).items() if hit[0] >= SEARCH_MIN_SCORE}
            contents = dict(db.execute(select(KnowledgeCard.id, KnowledgeCard.content).where(KnowledgeCard.id.in_(best))).all())
            shares = {card_id: word_share(wanted, content) for card_id, content in contents.items()}
            ranked = [(card_id, score + WORD_WEIGHT * shares[card_id], index)
                      for card_id, (score, index) in best.items() if search_passes(score, shares[card_id])]
            return sorted(ranked, key=lambda item: (-item[1], item[0]))[:limit], "meaning"
    scored = [(card_id, share, None) for card_id, content in db.execute(
        select(KnowledgeCard.id, KnowledgeCard.content).where(KnowledgeCard.id.in_(candidates)))
              if (share := word_share(wanted, content))]
    return sorted(scored, key=lambda item: (-item[1], item[0]))[:limit], "words"


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
