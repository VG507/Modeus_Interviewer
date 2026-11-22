"""Editor access to vacancy collections and manual HH refresh."""

from collections import Counter
from datetime import timedelta
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only

from backend.db import InterviewSession, VacancyCollection, now
from backend.interview_api import Actor, utc
from backend.taxonomy import skill_breakdown
from backend.vacancy_import import claim_collection, collection_available, configured, run_collection
from backend.vacancy_parser import profile_matches, role_key
from backend.vacancy_samples import FRESH_FOR, current_sample, sample_is_fresh, trudvsem_samples

router = APIRouter(prefix="/api/v1/vacancies")
Experience = Literal["noExperience", "between1And3", "between3And6", "moreThan6"]
Level = Literal["junior", "middle", "senior", "lead"]
VacancySource = Literal["hh", "file", "trudvsem"]
RoleText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ProfileInput(BaseModel):
    title: RoleText
    search_query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    aliases: list[RoleText] = Field(default_factory=list, max_length=16)

    @field_validator("aliases")
    @classmethod
    def unique_aliases(cls, aliases):
        return list({role_key(alias): alias for alias in aliases if role_key(alias)}.values())


class ProfileReview(BaseModel):
    approved: bool


def require_editor(actor):
    if not actor["is_editor"]:
        raise HTTPException(403, "EDITOR_REQUIRED")


def require_editor_write(request, actor):
    require_editor(actor)
    if request.headers.get("origin") not in request.app.state.app_origins:
        raise HTTPException(403, "ORIGIN_REJECTED")


def check_profile(db, profession_id):
    if not db.scalar(select(VacancyCollection.profession_id).where(VacancyCollection.profession_id == profession_id)):
        raise HTTPException(404, "PROFILE_NOT_FOUND")


def collection_status(collection, current):
    expired = bool(collection.run_id and (collection.lease_until is None or collection.lease_until <= current))
    running = bool(collection.run_id and not expired)
    return {
        "running": running,
        "error": "HH_TIMEOUT" if expired else collection.error,
        "started_at": utc(collection.started_at) if collection.started_at else None,
        "updated_at": utc(collection.updated_at) if collection.updated_at else None,
        "stale": bool(collection.updated_at and current - collection.updated_at > FRESH_FOR),
        "progress": collection.progress if running else None,
    }


@router.get("/overview")
def overview(request: Request, actor: Actor):
    require_editor(actor)
    with request.app.state.sessions() as db:
        samples = trudvsem_samples(db, include_cards=False)
        rows = db.scalars(select(VacancyCollection).options(load_only(
            VacancyCollection.profession_id, VacancyCollection.updated_at, VacancyCollection.started_at,
            VacancyCollection.run_id, VacancyCollection.lease_until, VacancyCollection.error,
            VacancyCollection.progress, VacancyCollection.stats,
            VacancyCollection.title, VacancyCollection.search_query, VacancyCollection.aliases,
            VacancyCollection.approved, VacancyCollection.config_version,
        )).order_by(VacancyCollection.profession_id))
        current = now()
        return {
            "configured": configured(request.app.state.settings),
            "profiles": [{"id": row.profession_id, "title": row.title, "search_query": row.search_query,
                          "aliases": row.aliases, "approved": row.approved,
                          "review_ready": review_ready(row, current, samples.get(row.profession_id)),
                          **collection_status(row, current),
                          "trudvsem": trudvsem_status(samples.get(row.profession_id), row, current),
                          "stats": {field: value for field, value in row.stats.items()
                                    if field in {"vacancies", "employers", "source", "details_fetched", "details_reused"}}}
                         for row in rows],
        }


def trudvsem_status(sample, profile, current):
    if sample is None:
        return None
    return {"updated_at": utc(sample.collected_at), "vacancies": sample.stats["vacancies"],
            "employers": sample.stats["employers"], "stale": not sample_is_fresh(sample.collected_at, current),
            "current_config": current_sample(sample.stats, profile)}


def review_ready(row, current, trudvsem=None):
    status = collection_status(row, current)
    return not status["running"] and (
        bool(row.stats.get("vacancies") and sample_is_fresh(row.updated_at, current) and current_sample(row.stats, row))
        or bool(trudvsem and trudvsem.stats.get("vacancies") and sample_is_fresh(trudvsem.collected_at, current)
                and current_sample(trudvsem.stats, row))
    )


@router.get("/candidates")
def candidates(request: Request, actor: Actor):
    require_editor(actor)
    with request.app.state.sessions() as db:
        profiles = list(db.scalars(select(VacancyCollection).options(load_only(
            VacancyCollection.profession_id, VacancyCollection.title, VacancyCollection.aliases,
        ))))
        counts = Counter()
        labels = {}
        role_value = InterviewSession.context["role"].as_string()
        occurrences = func.count()
        roles = db.execute(select(role_value, occurrences).where(
            InterviewSession.origin == "human", InterviewSession.created_at >= now() - timedelta(days=90),
            role_value.is_not(None),
        ).group_by(role_value).order_by(occurrences.desc(), role_value).limit(1000))
        for role, count in roles:
            if not isinstance(role, str) or not (key := role_key(role)) or len(role) > 120:
                continue
            if any(profile_matches(item.profession_id, role, item) for item in profiles):
                continue
            counts[key] += count
            labels.setdefault(key, role.strip())
    return {"candidates": [{"role": labels[key], "interviews": count} for key, count in counts.most_common(50)]}


@router.post("/profiles", status_code=201)
def create_profile(body: ProfileInput, request: Request, actor: Actor):
    require_editor_write(request, actor)
    key = role_key(body.title)
    if not key:
        raise HTTPException(422, "PROFILE_INVALID_TITLE")
    profession_id = uuid4().hex
    try:
        with request.app.state.sessions.begin() as db:
            db.add(VacancyCollection(profession_id=profession_id, title_key=key, **body.model_dump()))
    except IntegrityError as exc:
        raise HTTPException(409, "PROFILE_EXISTS") from exc
    return {"id": profession_id}


@router.put("/profiles/{profession_id}")
def edit_profile(profession_id: str, body: ProfileInput, request: Request, actor: Actor):
    require_editor_write(request, actor)
    key = role_key(body.title)
    if not key:
        raise HTTPException(422, "PROFILE_INVALID_TITLE")
    try:
        with request.app.state.sessions.begin() as db:
            check_profile(db, profession_id)
            changed = db.execute(update(VacancyCollection).where(
                VacancyCollection.profession_id == profession_id, collection_available(now()),
            ).values(**body.model_dump(), title_key=key, approved=False,
                      config_version=VacancyCollection.config_version + 1, run_id=None, lease_until=None,
                      progress=None, error=None)).rowcount
            if not changed:
                raise HTTPException(409, "HH_ALREADY_RUNNING")
    except IntegrityError as exc:
        raise HTTPException(409, "PROFILE_EXISTS") from exc
    return {"id": profession_id}


@router.post("/profiles/{profession_id}/review")
def review_profile(profession_id: str, body: ProfileReview, request: Request, actor: Actor):
    require_editor_write(request, actor)
    with request.app.state.sessions.begin() as db:
        row = db.get(VacancyCollection, profession_id, options=[load_only(
            VacancyCollection.stats, VacancyCollection.config_version, VacancyCollection.updated_at,
            VacancyCollection.started_at, VacancyCollection.run_id, VacancyCollection.lease_until,
            VacancyCollection.error, VacancyCollection.progress,
        )])
        if row is None:
            raise HTTPException(404, "PROFILE_NOT_FOUND")
        if body.approved:
            trudvsem = trudvsem_samples(db, profession_id, include_cards=False).get(profession_id)
            if not review_ready(row, now(), trudvsem):
                raise HTTPException(409, "PROFILE_REVIEW_NOT_READY")
        statement = update(VacancyCollection).where(VacancyCollection.profession_id == profession_id)
        if body.approved:
            statement = statement.where(VacancyCollection.config_version == row.config_version,
                                        VacancyCollection.updated_at == row.updated_at,
                                        VacancyCollection.run_id == row.run_id)
        if not db.execute(statement.values(approved=body.approved)).rowcount:
            raise HTTPException(409, "PROFILE_REVIEW_NOT_READY")
    return {"approved": body.approved}


@router.get("")
def vacancies(request: Request, actor: Actor, profession_id: str = "python_backend",
              experience_band: Experience | None = None, level: Level | None = None,
              source: VacancySource | None = None):
    require_editor(actor)
    with request.app.state.sessions() as db:
        collection = db.get(VacancyCollection, profession_id, options=[load_only(
            VacancyCollection.cards, VacancyCollection.stats, VacancyCollection.updated_at, VacancyCollection.started_at,
            VacancyCollection.run_id, VacancyCollection.lease_until, VacancyCollection.error, VacancyCollection.progress,
        )])
        if collection is None:
            raise HTTPException(404, "PROFILE_NOT_FOUND")
        original_source = collection.stats.get("source") or "hh"
        cards = [(card, original_source) for card in collection.cards if source in {None, original_source}]
        if source in {None, "trudvsem"}:
            sample = trudvsem_samples(db, profession_id).get(profession_id)
            if sample:
                cards.extend((card, "trudvsem") for card in sample.cards or [])
        shown = [(card, provider) for card, provider in cards
                 if (experience_band is None or card["experience_band"] == experience_band)
                 and (level is None or card["level"] == level)]
        return {
            "configured": configured(request.app.state.settings),
            "profiles": [{"id": key, "title": title} for key, title in db.execute(select(
                VacancyCollection.profession_id, VacancyCollection.title
            ).order_by(VacancyCollection.title))],
            **collection_status(collection, now()),
            "stats": collection.stats,
            "total": len(cards),
            "cards": [{**{key: value for key, value in card.items() if key != "description"}, "source": provider}
                      for card, provider in shown],
            "skill_categories": skill_breakdown(card["skills"] for card, _ in shown),
        }


@router.post("/{profession_id}/collect", status_code=202)
def collect(request: Request, tasks: BackgroundTasks, actor: Actor, profession_id: str):
    require_editor_write(request, actor)
    settings = request.app.state.settings
    if not configured(settings):
        raise HTTPException(409, "HH_NOT_CONFIGURED")
    sessions = request.app.state.sessions
    with sessions() as db:
        check_profile(db, profession_id)
    token = claim_collection(sessions, profession_id)
    if not token:
        raise HTTPException(409, "HH_ALREADY_RUNNING")
    tasks.add_task(run_collection, sessions, settings, profession_id, token)
    return {"status": "accepted"}
