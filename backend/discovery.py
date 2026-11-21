"""Collect GitHub candidates and manage official documentation review."""

import hashlib
import json
import re
import time
from contextlib import contextmanager
from datetime import timedelta
from difflib import unified_diff
from itertools import islice
from uuid import uuid4

import httpx
from sqlalchemy import case, delete, or_, select, update

from backend.db import (
    DiscoveryWindow, DocumentationRevision, DocumentationSubscription, DiscoveryCandidate, DiscoveryItem,
    DiscoveryRevision, RepositoryObservation, VacancyCollection, insert_for, now,
)
from backend.discovery_fetch import CollectionError, REPO_NAME, github_issues, repository_trends
from backend.source_context import ReviewedExcerpt, import_excerpts
from backend.vacancy_parser import canonical_skill, skill_hits


DEFAULT_REPOS = ("sqlalchemy/sqlalchemy", "celery/celery")
DEFAULT_TOPICS = ("llm", "rag", "vector-database")
ACTIVE_PROVIDERS = ("github", "trends", "docs", "openalex", "skills")


class InvalidDiscoveryQuery(CollectionError):
    pass


class DiscoveryBusy(CollectionError):
    pass


def insert_row(db, model, values, keys, *, changes=None, condition=None):
    dialect = db.get_bind().dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise CollectionError("Discovery supports SQLite and PostgreSQL")
    stmt = insert_for(db, model).values(**values)
    if changes is None:
        stmt = stmt.on_conflict_do_nothing(index_elements=keys)
    else:
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=changes, where=condition)
    db.execute(stmt)


def item_digest(item):
    evidence = {key: value for key, value in item.payload.items()
                if key not in {"stars", "forks", "pushed_at", "previous_digest"}}
    source = item.url
    if item.id.startswith("skills:"):
        evidence.pop("commit", None)
        source = item.id
    return hashlib.sha256(json.dumps([item.title, source, evidence], sort_keys=True).encode()).hexdigest()


def collect_records(db, provider, profession, items, observed_at):
    for item in sorted(items, key=lambda item: item.id):
        state = {"title": item.title, "url": item.url, "digest": item_digest(item),
                 "payload": item.payload, "observed_at": observed_at}
        replace = DiscoveryItem.observed_at <= observed_at
        insert_row(db, DiscoveryItem, {"id": item.id, "provider": provider, **state}, ["id"],
                   changes=state, condition=replace)
        if provider in {"github", "trends", "openalex", "skills"}:
            insert_row(db, DiscoveryRevision, {"item_id": item.id, **state}, ["item_id", "digest"])
        insert_row(db, DiscoveryCandidate, {"item_id": item.id, "profession_id": profession,
                                            "status": "observe" if provider == "trends" else "new"},
                   ["item_id", "profession_id"])
        if provider == "trends" and not item.payload.get("unavailable"):
            snapshot = {"stars": item.payload["stars"], "observed_at": observed_at}
            insert_row(db, RepositoryObservation,
                       {"item_id": item.id, "day": observed_at.date().isoformat(), **snapshot},
                       ["item_id", "day"], changes=snapshot, condition=RepositoryObservation.observed_at <= observed_at)


@contextmanager
def collection_lease(sessions, provider):
    token = str(uuid4())
    stamp = now()
    with sessions.begin() as db:
        lease = insert_for(db, DiscoveryWindow).values(
            provider=provider, token=token, available_at=stamp + timedelta(minutes=15),
        )
        claimed = db.execute(lease.on_conflict_do_update(
            index_elements=[DiscoveryWindow.provider],
            set_={"token": lease.excluded.token, "available_at": lease.excluded.available_at},
            where=or_(DiscoveryWindow.available_at.is_(None), DiscoveryWindow.available_at <= stamp),
        ).returning(DiscoveryWindow.provider)).first()
    if claimed is None:
        raise DiscoveryBusy("Collection already running")
    try:
        yield
    finally:
        with sessions.begin() as db:
            db.execute(delete(DiscoveryWindow).where(DiscoveryWindow.provider == provider, DiscoveryWindow.token == token))


def collect(sessions, settings, provider, profession, values, pages, limit, min_stars, labels=None,
            *, interactive=False):
    if provider not in {"github", "trends"}:
        raise InvalidDiscoveryQuery("Unknown GitHub source")
    if not values and profession != "python_backend":
        raise InvalidDiscoveryQuery("Specify repositories or topics for this profession")
    with collection_lease(sessions, provider):
        return collect_provider(sessions, settings, provider, profession, values, pages, limit, min_stars, labels,
                                interactive=interactive)


def collect_provider(sessions, settings, provider, profession, values, pages, limit, min_stars, labels,
                     *, interactive):
    with sessions() as db:
        if not db.get(VacancyCollection, profession):
            raise CollectionError("Unknown profession")
        tracked = []
        if provider == "trends" and not interactive:
            tracked = list(db.scalars(select(DiscoveryItem).join(DiscoveryCandidate).where(
                DiscoveryItem.provider == provider, DiscoveryCandidate.profession_id == profession,
                DiscoveryItem.payload["unavailable"].as_boolean().is_not(True),
            ).order_by(DiscoveryItem.observed_at, DiscoveryItem.id).limit(limit + 1)))
        elif provider == "github":
            tracked = list(db.scalars(select(DiscoveryItem).join(DiscoveryCandidate).where(
                DiscoveryItem.provider == provider, DiscoveryCandidate.profession_id == profession,
                DiscoveryItem.payload["repo"].as_string().in_([repo.casefold() for repo in (values or DEFAULT_REPOS)]),
                DiscoveryCandidate.status == "accepted",
                DiscoveryCandidate.valid_until > now(),
            ).order_by(DiscoveryItem.observed_at, DiscoveryItem.id).limit(limit + 1)))
    observed_at = now()
    headers = {"User-Agent": "Modeus-Interviewer", "Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    deadline = time.monotonic() + (50 if interactive else 12 * 60)

    def check_time(request):
        if time.monotonic() >= deadline:
            raise httpx.TimeoutException("Discovery request budget reached")

    with httpx.Client(timeout=10, headers=headers, follow_redirects=False,
                      event_hooks={"request": [check_time]}) as client:
        if provider == "github":
            repos = list(dict.fromkeys(repo.casefold() for repo in (values or DEFAULT_REPOS)))
            if not all(REPO_NAME.fullmatch(repo) and repo.split("/")[1] not in {".", ".."} for repo in repos):
                raise InvalidDiscoveryQuery("Repository must use owner/name")
            wanted = {label.casefold().strip() for label in (labels or ("bug", "performance", "database", "regression"))}
            items, partial = github_issues(client, repos, pages, limit, observed_at - timedelta(days=730), wanted, tracked)
        else:
            topics = values or DEFAULT_TOPICS
            if not all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", topic) for topic in topics):
                raise InvalidDiscoveryQuery("Invalid GitHub topic")
            items, partial = repository_trends(client, topics, pages, limit,
                (observed_at - timedelta(days=183)).date(), (observed_at - timedelta(days=30)).date(), min_stars, tracked)
    with sessions.begin() as db:
        collect_records(db, provider, profession, items, observed_at)
    return {"collected": len(items), "partial": partial}


def item_entry(db, profession, item_id):
    item = db.get(DiscoveryItem, item_id)
    if item is None or item.provider not in ACTIVE_PROVIDERS:
        return None, None
    return item, db.get(DiscoveryCandidate, (item_id, profession))


def vacancy_overlap(profile, item):
    fragments = item.payload.get("fragments", {}).values()
    technologies = {skill for skill, _ in skill_hits(item.title + "\n" + "\n".join(f["text"] for f in fragments))}
    return {skill: sum(skill in set(map(canonical_skill, card["skills"])) for card in profile.cards)
            for skill in sorted(technologies)}


def candidate_status(provider, status, reviewed, expired, unavailable, revised):
    status = status if reviewed else "new"
    if provider == "github" and status in {"new", "review"}:
        status = "automatic_check"
    if provider == "trends":
        status = "observe"
    if unavailable:
        return "unavailable" if provider == "docs" else "rejected"
    if status == "accepted" and expired:
        status = "expired"
    if provider == "docs" and status == "new" and revised:
        status = "review"
    return status


def candidate_output(db, item, candidate, *, include_text=False):
    status = candidate_status(item.provider, candidate.status, candidate.reviewed_digest == item.digest,
                              candidate.valid_until is None or candidate.valid_until <= now(),
                              bool(item.payload.get("reason")), bool(item.payload.get("previous_digest")))
    reason = item.payload.get("reason") or (candidate.reason if candidate.reviewed_digest == item.digest else None)
    if item.provider == "docs":
        if status == "review" and not reason:
            reason = "Официальный источник изменился и ждёт повторной проверки."
    output = {"id": item.id, "provider": item.provider, "title": item.title, "url": item.url,
              "observed_at": item.observed_at.isoformat() + "Z",
              "digest": item.digest, "status": status, "reason": reason,
              "assessment": candidate.assessment if candidate.reviewed_digest == item.digest else None}
    if item.provider == "trends":
        observed = item.observed_at.date()
        target = (observed - timedelta(days=7)).isoformat()
        previous = db.scalar(select(RepositoryObservation).where(
            RepositoryObservation.item_id == item.id,
            RepositoryObservation.day.between((observed - timedelta(days=8)).isoformat(),
                                              (observed - timedelta(days=6)).isoformat()),
        ).order_by(case((RepositoryObservation.day == target, 0), else_=1),
                   RepositoryObservation.day.desc()).limit(1))
        output["weekly_star_change"] = item.payload["stars"] - previous.stars if previous and not item.payload.get("unavailable") else None
    if include_text:
        profile = db.get(VacancyCollection, candidate.profession_id)
        output["vacancy_sample"] = {"size": len(profile.cards), "updated_at": profile.updated_at,
                                    "technology_mentions": vacancy_overlap(profile, item)}
        output["payload"] = ({key: value for key, value in item.payload.items() if key != "sections"}
                             if item.provider == "openalex" else item.payload)
        if item.provider == "docs":
            output["documentation_diff"] = documentation_diff(db, item, candidate)
    return output


def documentation_diff(db, item, candidate):
    baseline = candidate.reviewed_digest
    if not baseline or baseline == item.digest:
        baseline = item.payload.get("previous_digest")
    revision = db.get(DocumentationRevision, (item.id, baseline)) if baseline and baseline != item.digest else None
    if revision is None:
        return None

    def lines(payload):
        section = payload.get("fragments", {}).get("section", {})
        version = f"Версия {payload['version']}"
        if payload.get("supported") is not None:
            version += " · Поддерживается" if payload["supported"] else " · Поддержка завершена"
        return [payload.get("heading", ""), version, *section.get("text", payload.get("reason", "")).splitlines()]

    # Use bounded lines so one long paragraph cannot dominate the review screen.
    diff = unified_diff(lines(revision.payload), lines(item.payload), lineterm="", n=2)
    return [line[:2000] for line in islice(diff, 100)]


def list_candidates(db, profession, limit, *, provider=None, offset=0):
    query = select(DiscoveryItem, DiscoveryCandidate).join(DiscoveryCandidate).where(
        DiscoveryCandidate.profession_id == profession, DiscoveryItem.provider.in_(ACTIVE_PROVIDERS),
    )
    if provider:
        query = query.where(DiscoveryItem.provider == provider)
    rows = db.execute(query.order_by(DiscoveryItem.observed_at.desc(), DiscoveryItem.id).offset(offset).limit(limit))
    return [candidate_output(db, item, candidate) for item, candidate in rows]


def save_documentation_review(db, profession, item_id, digest, status, reason, *, evidence=None):
    item, candidate = item_entry(db, profession, item_id)
    if item is None or candidate is None or item.provider != "docs" or item.digest != digest:
        raise CollectionError("Candidate revision changed or is unavailable")
    values = {"status": status, "reason": reason, "reviewed_digest": digest, "assessment": None}
    db.execute(update(VacancyCollection).where(VacancyCollection.profession_id == profession)
               .values(profession_id=VacancyCollection.profession_id))
    # Serialize this decision with collection's update of the current source revision.
    if not db.execute(update(DiscoveryItem).where(DiscoveryItem.id == item_id, DiscoveryItem.digest == digest)
                      .values(digest=digest)).rowcount:
        raise CollectionError("Редакция источника изменилась. Загрузите её заново.")
    if status == "accepted":
        reviewed = approved_documentation(db, item, profession, evidence)
        import_excerpts(db, [reviewed.model_dump()])
        db.flush()
        checked = now()
        year = checked.year + 3
        try:
            until = checked.replace(year=year)
        except ValueError:  # February 29
            until = checked.replace(year=year, day=28)
        values.update(excerpt_id=reviewed.id, valid_until=until,
                      assessment={"checked_at": checked.isoformat() + "Z"})
    db.execute(update(DiscoveryCandidate).where(
        DiscoveryCandidate.item_id == item_id, DiscoveryCandidate.profession_id == profession,
    ).values(**values))


def approved_documentation(db, item, profession, evidence):
    payload = item.payload
    if payload["kind"] != "documentation" or payload.get("reason") or not evidence:
        raise CollectionError("Для интервью требуется действующая выдержка из официального руководства.")
    watch_id = payload["watch_id"]
    if db.get(DocumentationSubscription, (watch_id, profession)) is None:
        raise CollectionError("Включите отслеживание этого источника для профессии.")
    if payload.get("supported") is False and not evidence.get("conditions"):
        raise CollectionError("Укажите условия применимости для версии, вышедшей из поддержки.")
    fragment = payload["fragments"]["section"]
    excerpt_id = hashlib.sha256(f"discovery:{profession}:{item.id}".encode()).hexdigest()
    return ReviewedExcerpt(
        id=excerpt_id, profession_id=profession, title=item.title[:200], url=fragment["url"], text=fragment["text"],
        kind="documentation", purpose="behavior", technology=payload["technology"], version=payload["version"],
        **{key: evidence[key] for key in ("task", "quote", "detail", "conditions") if key in evidence},
    )
