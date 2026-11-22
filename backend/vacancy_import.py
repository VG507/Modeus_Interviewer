"""Collect a complete HH sample before publishing it for the interviewer."""

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
from uuid import uuid4

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import load_only
from sqlalchemy.exc import SQLAlchemyError

from backend.db import HeadHunterRequestWindow, VacancyCollection, VacancySnapshot, now
from backend.discovery_fetch import DNS_ERROR, is_dns_failure
from backend.settings import MAX_VACANCY_SAMPLE_LIMIT
from backend.vacancy_samples import vacancy_review
from backend.vacancy_parser import PROFILES, identity, parse_vacancy, role_key, vacancy_preset, vacancy_title_candidate

logger = logging.getLogger(__name__)
REQUEST_INTERVAL = timedelta(milliseconds=334)
RUN_TIMEOUT = timedelta(hours=1)
RUN_LEASE = timedelta(minutes=5)
PAGE_SIZE = 100
PERIOD_DAYS = 30
DETAILS_TTL = timedelta(hours=24)


class ImportFailure(RuntimeError):
    pass


def configured(settings):
    return bool(settings.hh_user_agent and (
        settings.hh_access_token or (settings.hh_client_id and settings.hh_client_secret)
    ))


class HeadHunter:
    def __init__(self, client, sessions, cached_details=None):
        self.client = client
        self.sessions = sessions
        self.deadline = monotonic() + RUN_TIMEOUT.total_seconds()
        self.cached_details = cached_details or {}
        self.details = {}
        self.details_fetched = 0
        self.details_reused = 0

    def request(self, method, path, *, missing_ok=False, **kwargs):
        for attempt in range(3):
            wait_for_request_window(self.sessions, self.deadline)
            delay = 2 ** attempt
            error = "HH_UNAVAILABLE"
            try:
                response = self.client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                if is_dns_failure(exc):
                    raise ImportFailure(DNS_ERROR) from None
            else:
                if response.is_success:
                    body = response.json()
                    if not isinstance(body, dict):
                        raise ImportFailure("HH_INVALID_RESPONSE")
                    return body
                if missing_ok and response.status_code == 404:
                    return None
                if response.status_code in {401, 403}:
                    raise ImportFailure("HH_TOKEN_FAILED" if path == "/token" else "HH_AUTH_FAILED")
                if response.status_code == 429:
                    error = "HH_RATE_LIMITED"
                if response.status_code != 429 and response.status_code < 500:
                    raise ImportFailure("HH_REQUEST_REJECTED")
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = int(retry_after)
            if attempt < 2 and delay <= 30:
                sleep(delay)
            elif delay > 30:
                break
        raise ImportFailure(error)

    def vacancies(self, profession_id, areas, progress=None, search_query=None, profile=None):
        if (profile is not None and vacancy_preset(profession_id, profile) == "ml_engineer"
                and role_key(search_query or "") == role_key(profile.title)):
            search_query = PROFILES["ml_engineer"][1]
        seen = set()
        for page in range(10):
            if progress:
                progress.report("search")
            result = self.request("GET", "/vacancies", params={
                "text": search_query or PROFILES[profession_id][1], "area": areas, "period": PERIOD_DAYS,
                "page": page, "per_page": PAGE_SIZE, "order_by": "publication_time",
            })
            for teaser in result["items"]:
                external_id, url = identity(teaser)
                if not external_id or url in seen:
                    continue
                seen.add(url)
                if teaser.get("archived") is True:
                    continue
                title = teaser.get("name")
                if isinstance(title, str) and title.strip() and not vacancy_title_candidate(profession_id, title, profile):
                    continue
                if progress:
                    progress.report("details")
                entry = self.cached_details.get(external_id)
                marker = {key: teaser[key] for key in ("name", "published_at", "experience") if key in teaser}
                if (entry and entry["marker"] == marker
                        and now() - datetime.fromisoformat(entry["fetched_at"]) < DETAILS_TTL):
                    self.details_reused += 1
                else:
                    self.details_fetched += 1
                    detail = self.request("GET", f"/vacancies/{external_id}", missing_ok=True)
                    if detail is None:
                        if progress:
                            progress.report("details", details_fetched=self.details_fetched)
                        continue
                    if identity(detail)[1] != url:
                        raise ImportFailure("HH_INVALID_RESPONSE")
                    ids = {key: {"id": detail[key].get("id")} for key in ("area", "employer") if detail.get(key)}
                    detail = {key: detail[key] for key in (
                        "id", "alternate_url", "name", "description", "archived", "key_skills", "published_at", "experience",
                    ) if key in detail}
                    entry = {"fetched_at": now().isoformat(), "marker": marker, "detail": {**detail, **ids}}
                self.details[external_id] = entry
                if progress:
                    progress.report("details", details_fetched=self.details_fetched, details_reused=self.details_reused)
                yield {**teaser, **entry["detail"], "snippet": teaser.get("snippet")}
            if page + 1 >= result["pages"]:
                break


def wait_for_request_window(sessions, deadline):
    while monotonic() < deadline:
        with sessions.begin() as db:
            started = now()
            interval = HeadHunterRequestWindow.last_started_at
            claimed = db.execute(update(HeadHunterRequestWindow).where(
                HeadHunterRequestWindow.id == 1,
                or_(interval.is_(None), interval <= started - REQUEST_INTERVAL),
            ).values(last_started_at=started)).rowcount
            if claimed:
                return
            previous = db.get(HeadHunterRequestWindow, 1).last_started_at
        seconds = max(0, (previous + REQUEST_INTERVAL - now()).total_seconds())
        sleep(min(seconds, max(0, deadline - monotonic())))
    raise ImportFailure("HH_TIMEOUT")


class CollectionProgress:
    def __init__(self, sessions, profession_id, token, target=MAX_VACANCY_SAMPLE_LIMIT):
        self.sessions = sessions
        self.profession_id = profession_id
        self.token = token
        self.state = {"stage": "queued", "accepted": 0, "details_fetched": 0, "details_reused": 0, "target": target}
        self.last_report_at = float("-inf")

    def report(self, stage, **counts):
        changed_stage = stage != self.state["stage"]
        self.state.update(stage=stage, **counts)
        if not changed_stage and monotonic() - self.last_report_at < 1:
            return
        with self.sessions.begin() as db:
            stamp = now()
            changed = db.execute(update(VacancyCollection).where(
                VacancyCollection.profession_id == self.profession_id, VacancyCollection.run_id == self.token,
                VacancyCollection.lease_until > stamp,
            ).values(lease_until=stamp + RUN_LEASE,
                     progress={**self.state, "reported_at": stamp.replace(tzinfo=timezone.utc).isoformat()})).rowcount
            if not changed:
                raise ImportFailure("HH_TIMEOUT")
        self.last_report_at = monotonic()


def live_cards(sessions, settings, profile, progress):
    if not configured(settings):
        raise ImportFailure("HH_NOT_CONFIGURED")
    areas = working_areas(settings)
    if not areas or any(not area.isascii() or not area.isdigit() for area in areas):
        raise ImportFailure("HH_INVALID_AREAS")
    with httpx.Client(base_url="https://api.hh.ru", headers={"User-Agent": settings.hh_user_agent},
                      timeout=12, follow_redirects=False) as client:
        hh = HeadHunter(client, sessions, profile.details_cache)
        if not settings.hh_access_token:
            progress.report("token")
        token = settings.hh_access_token or hh.request("POST", "/token", data={
            "grant_type": "client_credentials", "client_id": settings.hh_client_id,
            "client_secret": settings.hh_client_secret,
        })["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        profession_id = profile.profession_id
        cards = prepare_cards(hh.vacancies(profession_id, areas, progress, profile.search_query, profile), profession_id,
                              on_card=lambda count: progress.report("details", accepted=count), profile=profile,
                              limit=settings.vacancy_sample_limit)
        details_cache = {card["id"]: hh.details[card["id"]] for card in cards}
        stats = {"details_fetched": hh.details_fetched, "details_reused": hh.details_reused}
        return cards, details_cache, stats


def prepare_cards(items, profession_id, on_card=None, profile=None, *, limit=MAX_VACANCY_SAMPLE_LIMIT):
    cards = {}
    for item in items:
        card = parse_vacancy(item, profession_id, profile)
        if card:
            cards[card["id"]] = card
            if on_card:
                on_card(len(cards))
        if len(cards) >= limit:
            break
    if not cards:
        raise ImportFailure("HH_EMPTY_SAMPLE")
    return list(cards.values())


def working_areas(settings):
    return [part.strip() for part in settings.hh_areas.split(",") if part.strip()]


def collection_available(current):
    return or_(VacancyCollection.run_id.is_(None), VacancyCollection.lease_until.is_(None),
               VacancyCollection.lease_until <= current)


def claim_collection(sessions, profession_id, scheduled_since=None):
    started = now()
    token = str(uuid4())
    with sessions.begin() as db:
        where = [VacancyCollection.profession_id == profession_id,
                 collection_available(started)]
        if scheduled_since is not None:
            where.append(VacancyCollection.approved.is_(True))
            where.append(or_(VacancyCollection.run_id.is_not(None), VacancyCollection.started_at.is_(None),
                             VacancyCollection.started_at < scheduled_since))
        changed = db.execute(update(VacancyCollection).where(*where).values(
            run_id=token, started_at=started, lease_until=started + RUN_LEASE, error=None, progress=None,
        )).rowcount
    return token if changed else None


def run_collection(sessions, settings, profession_id, token, fixture=None):
    progress = CollectionProgress(sessions, profession_id, token, settings.vacancy_sample_limit)
    with sessions() as db:
        profile = db.get(VacancyCollection, profession_id, options=[load_only(
            VacancyCollection.title, VacancyCollection.search_query, VacancyCollection.aliases,
            VacancyCollection.details_cache, VacancyCollection.config_version, VacancyCollection.run_id,
        )])
        if profile is None or profile.run_id != token:
            return {"status": "superseded"}
    try:
        if fixture is None:
            cards, details_cache, transfer_stats = live_cards(sessions, settings, profile, progress)
        else:
            progress.report("file")
            items = json.loads(fixture.read_text(encoding="utf-8-sig"))
            cards = prepare_cards(items, profession_id, profile=profile, limit=settings.vacancy_sample_limit)
            details_cache, transfer_stats = None, {}
        stats = {**sample_stats(cards, working_areas(settings) if fixture is None else None), **transfer_stats,
                 "config_version": profile.config_version}
        progress.report("saving", accepted=len(cards))
    except ImportFailure as exc:
        error = str(exc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        error = "HH_INVALID_RESPONSE" if fixture is None else "HH_INVALID_FILE"
    else:
        collected = now()
        with sessions.begin() as db:
            row = db.execute(update(VacancyCollection).where(
                VacancyCollection.profession_id == profession_id, VacancyCollection.run_id == token,
                VacancyCollection.lease_until > collected,
            ).values(cards=cards, stats=stats, details_cache=details_cache, updated_at=collected,
                     run_id=None, lease_until=None, progress=None).returning(VacancyCollection.approved)).first()
            if row is None:
                return {"status": "superseded"}
            if fixture is None:
                stats = {**stats, "review": vacancy_review(cards, row.approved)}
                db.get(VacancyCollection, profession_id).stats = stats
            db.add(VacancySnapshot(id=token, profession_id=profession_id, collected_at=collected, stats=stats, cards=cards))
        return {"status": "ok", **stats}
    with sessions.begin() as db:
        db.execute(update(VacancyCollection).where(
            VacancyCollection.profession_id == profession_id, VacancyCollection.run_id == token,
        ).values(run_id=None, lease_until=None, error=error, progress=None))
    return {"status": "failed", "error": error}


def sample_stats(cards, areas):
    employers = {card["employer_id"] for card in cards if card["employer_id"]}
    by_employer = {}
    for card in cards:
        key = card["employer_id"] or f"vacancy:{card['id']}"
        by_employer.setdefault(key, set()).update(card["skills"])
    return {"vacancies": len(cards), "employers": len(employers), "area_ids": areas,
            "period_days": PERIOD_DAYS if areas is not None else None,
            "source": "hh" if areas is not None else "file",
            "skill_counts": dict(Counter(skill for skills in by_employer.values() for skill in skills)),
            "vacancy_skill_counts": dict(Counter(skill for card in cards for skill in card["skills"]))}


def scheduled_sweep(sessions, settings, utc_now):
    moscow = utc_now.astimezone(timezone(timedelta(hours=3)))
    monday = moscow - timedelta(days=moscow.weekday())
    due = monday.replace(hour=9, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    if utc_now >= due:
        with sessions() as db:
            profiles = list(db.scalars(select(VacancyCollection.profession_id).where(VacancyCollection.approved.is_(True))))
        for profession_id in profiles:
            token = claim_collection(sessions, profession_id, due.replace(tzinfo=None))
            if token:
                run_collection(sessions, settings, profession_id, token)


async def schedule_collections(sessions, settings):
    while True:
        try:
            utc_now = now().replace(tzinfo=timezone.utc)
            await asyncio.to_thread(scheduled_sweep, sessions, settings, utc_now)
        except SQLAlchemyError as exc:
            logger.warning("HH collection database error: %s", type(exc).__name__)
        await asyncio.sleep(60)
