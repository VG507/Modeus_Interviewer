"""Refresh the tracked official sources and enqueue new documentation revisions."""

import asyncio
import hashlib
import logging
from datetime import timedelta, timezone
from uuid import uuid4

import httpx
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from backend.db import (
    DocumentationRevision, DocumentationSubscription, DocumentationWatch, DiscoveryCandidate, DiscoveryItem,
    VacancyCollection, now,
)
from backend.discovery import collect_records, insert_row, item_digest
from backend.discovery_fetch import Collected, CollectionError, is_dns_failure
from backend.documentation_fetch import TARGETS, document_sections, release_notes


logger = logging.getLogger(__name__)


def check_watches(db, profession):
    enabled = set(db.scalars(select(DocumentationSubscription.watch_id).where(
        DocumentationSubscription.profession_id == profession,
    )))
    rows = {row.id: row for row in db.scalars(select(DocumentationWatch))}
    return [{
        "id": id_, "title": target.title, "url": target.url, "enabled": id_ in enabled,
        "checked_at": iso_time(rows[id_].checked_at), "attempted_at": iso_time(rows[id_].attempted_at),
        "error": rows[id_].error,
    } for id_, target in TARGETS.items() if id_ in rows]


def iso_time(value):
    return value.isoformat() + "Z" if value else None


def configure_watches(db, profession, selected):
    if set(selected) - TARGETS.keys():
        raise CollectionError("Неизвестный источник документации.")
    # Serialize concurrent updates to a profile's subscription set.
    db.execute(update(VacancyCollection).where(VacancyCollection.profession_id == profession)
               .values(profession_id=VacancyCollection.profession_id))
    removed = list(db.scalars(delete(DocumentationSubscription).where(
        DocumentationSubscription.profession_id == profession,
        DocumentationSubscription.watch_id.not_in(selected),
    ).returning(DocumentationSubscription.watch_id)))
    if removed:
        items = select(DiscoveryItem.id).where(
            DiscoveryItem.provider == "docs", DiscoveryItem.payload["watch_id"].as_string().in_(removed),
        )
        db.execute(update(DiscoveryCandidate).where(
            DiscoveryCandidate.profession_id == profession, DiscoveryCandidate.item_id.in_(items),
            DiscoveryCandidate.status == "accepted",
        ).values(status="review", reason="Наблюдение за источником было приостановлено."))
    for id_ in sorted(set(selected)):
        insert_row(db, DocumentationSubscription, {"watch_id": id_, "profession_id": profession},
                   ["watch_id", "profession_id"])
    known = db.scalars(select(DiscoveryItem.id).where(
        DiscoveryItem.provider == "docs", DiscoveryItem.payload["watch_id"].as_string().in_(selected),
        DiscoveryItem.payload["reason"].as_string().is_(None),
    ))
    for item_id in known:
        insert_row(db, DiscoveryCandidate, {"item_id": item_id, "profession_id": profession, "status": "new"},
                   ["item_id", "profession_id"])
    return check_watches(db, profession)


def collect_documentation(sessions, profession=None, *, due=None):
    with sessions() as db:
        query = select(DocumentationSubscription.watch_id).distinct()
        if profession is not None:
            query = query.where(DocumentationSubscription.profession_id == profession)
        enabled = set(db.scalars(query))
    result = {"collected": 0, "changed": 0, "errors": {}, "busy": 0}
    # These hosts and paths come exclusively from the registry. There are no redirects or credentials.
    with httpx.Client(timeout=7, follow_redirects=False, headers={
        "User-Agent": "Modeus-Interviewer", "Accept": "text/html",
    }) as client:
        for id_, target in TARGETS.items():
            if id_ not in enabled:
                continue
            token = claim_watch(sessions, id_, due)
            if token is None:
                result["busy"] += int(due is None)
                continue
            error = None
            try:
                if target.section:
                    sections, missing = document_sections(client, target)
                else:
                    version, url, text, published = release_notes(client, target)
                    sections = {"root": (f"{target.technology} {version} · заметки о релизе", url, {
                        "text": text, "version": version, "published": published,
                    })}
                    missing = None
                count, changes = finish_watch(sessions, id_, token, sections, missing)
                result["collected"] += count
                result["changed"] += changes
                if missing and not sections:
                    result["errors"][id_] = missing
            except CollectionError as exc:
                error = str(exc)
            except httpx.HTTPStatusError as exc:
                error = f"Официальный сайт вернул HTTP {exc.response.status_code}."
            except httpx.HTTPError as exc:
                error = ("Сервер не смог разрешить имя сайта документации (DNS)." if is_dns_failure(exc)
                         else "Сетевая ошибка при загрузке официальной документации.")
            except (UnicodeError, ValueError):
                error = "Не удалось разобрать ответ официального сайта."
            if error:
                result["errors"][id_] = error
                with sessions.begin() as db:
                    db.execute(update(DocumentationWatch).where(
                        DocumentationWatch.id == id_, DocumentationWatch.token == token,
                    ).values(token=None, lease_until=None, error=error))
    return result


def claim_watch(sessions, id_, due):
    time = now()
    token = str(uuid4())
    claim = update(DocumentationWatch).where(
        DocumentationWatch.id == id_,
        or_(DocumentationWatch.lease_until.is_(None), DocumentationWatch.lease_until <= time),
    )
    if due is not None:
        claim = claim.where(
            or_(DocumentationWatch.checked_at.is_(None), DocumentationWatch.checked_at < due),
            or_(DocumentationWatch.attempted_at.is_(None), DocumentationWatch.attempted_at < due,
                DocumentationWatch.attempted_at <= time - timedelta(minutes=30)),
        )
    with sessions.begin() as db:
        claimed = db.execute(claim.values(attempted_at=time, token=token,
                                          lease_until=time + timedelta(minutes=2))).rowcount
    return token if claimed else None


def finish_watch(sessions, id_, token, sections, missing):
    time = now()
    target = TARGETS[id_]
    with sessions.begin() as db:
        if not db.execute(update(DocumentationWatch).where(
            DocumentationWatch.id == id_, DocumentationWatch.token == token,
        ).values(checked_at=time, error=missing if not sections else None, token=None, lease_until=None)).rowcount:
            return 0, 0
        professions = list(db.scalars(select(DocumentationSubscription.profession_id).where(
            DocumentationSubscription.watch_id == id_,
        ).order_by(DocumentationSubscription.profession_id)))
        if not professions:
            return 0, 0
        previous = {item.id: item for item in db.scalars(select(DiscoveryItem).where(
            DiscoveryItem.id.startswith(f"doc:{id_}:"),
        ))}
        items = []
        for key, (title, url, info) in sections.items():
            stable_id = f"doc:{id_}:{hashlib.sha256(key.encode()).hexdigest()[:24]}"
            payload = {key: value for key, value in info.items() if key != "text"}
            payload.update(watch_id=id_, heading=title, technology=target.technology,
                           kind="documentation" if target.section else "release",
                           fragments={"section": {"url": url, "text": info["text"]}})
            items.append(Collected(stable_id, title, url, payload))
        seen = {item.id for item in items}
        for old in previous.values():
            if old.id not in seen and not old.payload.get("reason") and missing:
                payload = {key: value for key, value in old.payload.items() if key != "previous_digest"}
                items.append(Collected(old.id, old.title, old.url, {**payload, "reason": missing, "fragments": {}}))

        changed = 0
        for item in items:
            digest = item_digest(item)
            old = previous.get(item.id)
            is_new = old is None or old.digest != digest
            if is_new:
                if old is not None:
                    item.payload["previous_digest"] = old.digest
                collect_records(db, "docs", professions[0], [item], time)
                insert_row(db, DocumentationRevision,
                           {"item_id": item.id, "digest": digest, "payload": item.payload}, ["item_id", "digest"])
                db.execute(update(DiscoveryCandidate).where(
                    DiscoveryCandidate.item_id == item.id, DiscoveryCandidate.reviewed_digest.is_not(None),
                ).values(status="review", reason=None))
                changed += 1
            for profession in professions[1:] if is_new else professions:
                insert_row(db, DiscoveryCandidate, {"item_id": item.id, "profession_id": profession, "status": "new"},
                           ["item_id", "profession_id"])
        return len(sections), changed


def weekly_due(utc_now):
    moscow = utc_now.astimezone(timezone(timedelta(hours=3)))
    monday = (moscow - timedelta(days=moscow.weekday())).replace(hour=9, minute=0, second=0, microsecond=0)
    if moscow < monday:
        monday -= timedelta(days=7)
    return monday.astimezone(timezone.utc).replace(tzinfo=None)


async def schedule_documentation(sessions):
    while True:
        await asyncio.sleep(60)
        try:
            due = weekly_due(now().replace(tzinfo=timezone.utc))
            await asyncio.to_thread(collect_documentation, sessions, due=due)
        except SQLAlchemyError as exc:
            logger.warning("Documentation refresh database error: %s", type(exc).__name__)
