"""Collect and read source candidates from the editor interface."""

import logging
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy import select, update

from backend import profession_collection
from backend.db import EscoProfile, OnetProfile, ProfessionCollectionRun, VacancyCollection
from backend.discovery import (
    DiscoveryBusy, InvalidDiscoveryQuery, candidate_output, collect, item_entry, list_candidates,
    save_documentation_review,
)
from backend.discovery_fetch import DNS_ERROR, CollectionError, ProviderRateLimit, is_dns_failure
from backend.documentation import check_watches, collect_documentation, configure_watches, iso_time
from backend.documentation_fetch import TARGETS
from backend.esco import classification_url, concept_uri, localized_skill
from backend.interview_api import Actor
from backend.interview_schemas import Schema, ShortText
from backend.onet import profile_output as onet_profile_output
from backend.source_context import ExcerptText
from backend.vacancy_api import check_profile, require_editor, require_editor_write


router = APIRouter(prefix="/api/v1/discovery")
SearchProvider = Literal["github", "trends"]
Provider = Literal["github", "trends", "docs", "openalex", "skills", "habr"]
SearchTerm = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=140)]
logger = logging.getLogger(__name__)


class DiscoverySearch(Schema):
    profession_id: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    provider: SearchProvider
    values: list[SearchTerm] = Field(min_length=1, max_length=5)
    labels: list[SearchTerm] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=10, ge=1, le=20)


class DocumentationProfile(Schema):
    profession_id: Annotated[str, StringConstraints(min_length=1, max_length=32)]


class CollectionRetry(Schema):
    run_id: Annotated[str, StringConstraints(min_length=36, max_length=36)]
    source: Literal["hh", "trudvsem", "esco", "github", "trends", "docs", "hn", "openalex", "skills", "habr"]


class EscoMapping(Schema):
    target: Annotated[str, StringConstraints(strip_whitespace=True, max_length=180)]
    match: Literal["exact", "broader"]


class DocumentationConfig(DocumentationProfile):
    targets: list[Annotated[str, StringConstraints(max_length=48)]] = Field(max_length=len(TARGETS))


class DocumentationEvidence(Schema):
    task: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    quote: ExcerptText
    detail: Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=200)]
    conditions: list[ShortText] = Field(default_factory=list, max_length=8)


class DocumentationReview(DocumentationProfile):
    digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    decision: Literal["accepted", "observe", "rejected"]
    reason: ShortText
    evidence: DocumentationEvidence | None = None


@router.post("/collect")
def start_collection(request: Request, actor: Actor, search: DiscoverySearch):
    require_editor_write(request, actor)
    sessions = request.app.state.sessions
    with sessions() as db:
        check_profile(db, search.profession_id)
    try:
        values = list(dict.fromkeys(search.values))
        result = collect(sessions, request.app.state.settings, search.provider, search.profession_id,
                         values, 2, search.limit, 100, search.labels, interactive=True)
    except ProviderRateLimit as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        raise HTTPException(429, "DISCOVERY_RATE_LIMIT", headers=headers) from None
    except httpx.TimeoutException:
        raise HTTPException(504, "DISCOVERY_TIMEOUT") from None
    except DiscoveryBusy:
        raise HTTPException(409, "DISCOVERY_BUSY") from None
    except InvalidDiscoveryQuery:
        raise HTTPException(422, "DISCOVERY_QUERY_INVALID") from None
    except CollectionError as exc:
        logger.warning("Discovery collection failed for %s: %s", search.provider, exc)
        raise HTTPException(502, "DISCOVERY_UNAVAILABLE") from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Discovery collection failed for %s: %s", search.provider, type(exc).__name__)
        raise HTTPException(502, DNS_ERROR if is_dns_failure(exc) else "DISCOVERY_UNAVAILABLE") from None
    with sessions.begin() as db:
        db.execute(update(VacancyCollection).where(VacancyCollection.profession_id == search.profession_id)
                   .values(profession_id=search.profession_id))
        profile = db.get(VacancyCollection, search.profession_id)
        profile.discovery_searches = {**(profile.discovery_searches or {}), search.provider: {
            "values": values, "limit": search.limit, "labels": search.labels,
        }}
    return result


@router.get("/collection/{profession_id}")
def collection_status(request: Request, actor: Actor, profession_id: str):
    require_editor(actor)
    with request.app.state.sessions() as db:
        check_profile(db, profession_id)
        run = profession_collection.output(db.get(ProfessionCollectionRun, profession_id))
        return {"run": run, "quality": profession_collection.quality(db, profession_id, request.app.state.settings)}


@router.post("/collection/{profession_id}", status_code=202)
def collect_profession(request: Request, tasks: BackgroundTasks, actor: Actor, profession_id: str,
                       retry: CollectionRetry | None = None):
    require_editor_write(request, actor)
    sessions = request.app.state.sessions
    source = retry.source if retry else None
    with sessions.begin() as db:
        check_profile(db, profession_id)
        run, token = profession_collection.start(db, profession_id, retry.run_id if retry else None, source)
    if token:
        tasks.add_task(profession_collection.refresh_sources, sessions, request.app.state.settings,
                       profession_id, token, source)
    if run is None:
        raise HTTPException(409, "COLLECTION_RETRY_UNAVAILABLE")
    return run


@router.get("/collection/{profession_id}/onet")
def onet_profile(request: Request, actor: Actor, profession_id: str):
    require_editor(actor)
    with request.app.state.sessions() as db:
        check_profile(db, profession_id)
        profile = db.get(OnetProfile, profession_id)
        return onet_profile_output(profile) if profile else None


@router.get("/collection/{profession_id}/esco")
def esco_profile(request: Request, actor: Actor, profession_id: str):
    require_editor(actor)
    with request.app.state.sessions() as db:
        check_profile(db, profession_id)
        snapshot = db.get(EscoProfile, profession_id)
        if snapshot is None:
            raise HTTPException(404, "ESCO_NO_SAMPLE")
        return {
            "occupation": {**snapshot.occupation, "url": classification_url(snapshot.occupation["uri"])},
            "skills": [localized_skill(skill) for skill in snapshot.skills],
            "version": snapshot.version, "checked_at": iso_time(snapshot.collected_at), "match": snapshot.match,
        }


@router.put("/collection/{profession_id}/esco", status_code=204)
def set_esco_mapping(request: Request, actor: Actor, profession_id: str, mapping: EscoMapping):
    require_editor_write(request, actor)
    target = mapping.target
    if "://" in target:
        target = concept_uri(target, "occupation")
        if not target:
            raise HTTPException(422, "ESCO_INVALID_URI")
    with request.app.state.sessions.begin() as db:
        check_profile(db, profession_id)
        db.execute(update(VacancyCollection).where(VacancyCollection.profession_id == profession_id)
                   .values(profession_id=profession_id))
        profile = db.get(VacancyCollection, profession_id)
        searches = dict(profile.discovery_searches or {})
        if target:
            searches["esco"] = {"target": target, "match": mapping.match}
        else:
            searches.pop("esco", None)
        profile.discovery_searches = searches


@router.post("/collection/{profession_id}/esco", status_code=202)
def collect_esco_profile(request: Request, tasks: BackgroundTasks, actor: Actor, profession_id: str):
    return start_selected_sources(request, tasks, actor, profession_id, "esco")


@router.post("/collection/{profession_id}/vacancies", status_code=202)
def collect_vacancy_sources(request: Request, tasks: BackgroundTasks, actor: Actor, profession_id: str):
    return start_selected_sources(request, tasks, actor, profession_id, ("hh", "trudvsem"))


def start_selected_sources(request, tasks, actor, profession_id, sources):
    require_editor_write(request, actor)
    sessions = request.app.state.sessions
    with sessions.begin() as db:
        check_profile(db, profession_id)
        run, token = profession_collection.start(db, profession_id, only=sources)
    if token:
        tasks.add_task(profession_collection.refresh_sources, sessions, request.app.state.settings,
                       profession_id, token, sources)
    elif not all(run["steps"].get(key, {}).get("status") in profession_collection.ACTIVE
                 for key in profession_collection.selected_sources(sources)):
        raise HTTPException(409, "DISCOVERY_BUSY")
    return run


@router.get("/profiles")
def profiles(request: Request, actor: Actor):
    require_editor(actor)
    with request.app.state.sessions() as db:
        rows = db.execute(select(VacancyCollection.profession_id, VacancyCollection.title)
                          .order_by(VacancyCollection.title, VacancyCollection.profession_id))
        return [{"id": id_, "title": title} for id_, title in rows]


@router.get("/documentation")
def documentation_watches(request: Request, actor: Actor,
                          profession_id: Annotated[str, Query(min_length=1, max_length=32)]):
    require_editor(actor)
    with request.app.state.sessions() as db:
        check_profile(db, profession_id)
        return check_watches(db, profession_id)


@router.put("/documentation")
def set_documentation_watches(request: Request, actor: Actor, body: DocumentationConfig):
    require_editor_write(request, actor)
    if set(body.targets) - TARGETS.keys():
        raise HTTPException(422, "DOCUMENTATION_TARGET_INVALID")
    with request.app.state.sessions.begin() as db:
        check_profile(db, body.profession_id)
        return configure_watches(db, body.profession_id, body.targets)


@router.post("/documentation/collect")
def refresh_documentation(request: Request, actor: Actor, body: DocumentationProfile):
    require_editor_write(request, actor)
    sessions = request.app.state.sessions
    with sessions() as db:
        check_profile(db, body.profession_id)
    return collect_documentation(sessions, body.profession_id)


@router.post("/documentation/{item_id}/review")
def review_documentation(request: Request, actor: Actor, item_id: str, body: DocumentationReview):
    require_editor_write(request, actor)
    with request.app.state.sessions.begin() as db:
        item, review = item_entry(db, body.profession_id, item_id)
        if item is None or review is None or item.provider != "docs":
            raise HTTPException(404, "DISCOVERY_ITEM_NOT_FOUND")
        if item.digest != body.digest:
            raise HTTPException(409, "DOCUMENTATION_REVISION_CHANGED")
        try:
            save_documentation_review(db, body.profession_id, item_id, body.digest, body.decision, body.reason,
                        evidence=body.evidence.model_dump() if body.evidence else None)
        except ValidationError:
            raise HTTPException(422, "DOCUMENTATION_QUOTE_INVALID") from None
        except CollectionError as exc:
            status = 409 if "Редакция" in str(exc) else 422
            raise HTTPException(status, {"code": "DOCUMENTATION_REVIEW_INVALID", "message": str(exc)}) from None
        db.refresh(review)
        return candidate_output(db, item, review, include_text=True)


@router.get("")
def candidates(request: Request, actor: Actor, profession_id: Annotated[str, Query(min_length=1, max_length=32)],
               provider: Provider | None = None, offset: Annotated[int, Query(ge=0)] = 0,
               limit: Annotated[int, Query(ge=1, le=50)] = 20):
    require_editor(actor)
    with request.app.state.sessions() as db:
        items = list_candidates(db, profession_id, limit + 1, provider=provider, offset=offset)
        return {"items": items[:limit], "next_offset": offset + limit if len(items) > limit else None}


@router.get("/items/{item_id}")
def candidate(request: Request, actor: Actor, item_id: str,
              profession_id: Annotated[str, Query(min_length=1, max_length=32)]):
    require_editor(actor)
    with request.app.state.sessions() as db:
        item, review = item_entry(db, profession_id, item_id)
        if item is None or review is None:
            raise HTTPException(404, "DISCOVERY_ITEM_NOT_FOUND")
        return candidate_output(db, item, review, include_text=True)
