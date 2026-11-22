"""Collect and normalize vacancies from the public Work in Russia API."""

import logging
import re
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import update
from sqlalchemy.orm import load_only

from backend.db import VacancyCollection, VacancySnapshot, VacancySourceSample
from backend.discovery_fetch import DNS_ERROR, is_dns_failure
from backend.settings import MAX_VACANCY_SAMPLE_LIMIT
from backend.vacancy_import import sample_stats
from backend.vacancy_parser import build_vacancy, clean_html, role_key, vacancy_preset, with_sections
from backend.vacancy_samples import vacancy_review


logger = logging.getLogger(__name__)
API = "https://opendata.trudvsem.ru/api/v1/vacancies"
PAGE_SIZE = 100
MAX_PAGES = 3
MAX_TERMS = 4
REQUEST_TIMEOUT = 8
COLLECTION_BUDGET = 45
EXTERNAL_ID = re.compile(r"[a-zA-Z0-9_-]{1,80}")
DEFAULT_TERMS = {
    "python_backend": ("Python", "Python backend", "Django", "FastAPI"),
    "frontend": ("Фронтенд", "Frontend", "React"),
    "qa": ("Тестировщик", "Инженер по тестированию", "QA", "SDET"),
    "devops": ("DevOps", "SRE", "DevSecOps", "Инженер платформы"),
    "ml_engineer": ("ML инженер", "ML engineer", "машинного обучения", "machine learning"),
}


class TrudvsemError(RuntimeError):
    pass


def search_terms(profile):
    preset = vacancy_preset(profile.profession_id, profile)
    if preset in DEFAULT_TERMS:
        return DEFAULT_TERMS[preset]
    terms = {}
    for term in (profile.title, *(profile.aliases or [])):
        if key := role_key(term):
            terms.setdefault(key, term)
    return tuple(terms.values())[:MAX_TERMS]


def vacancy_url(value, external_id, company_code):
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        if (parsed and parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {"trudvsem.ru", "www.trudvsem.ru"}
                and parsed.path.startswith("/vacancy/card/") and parsed.path.rstrip("/").endswith(f"/{external_id}")):
            return urlunsplit(("https", "trudvsem.ru", parsed.path, "", ""))
    except ValueError:
        pass
    if company_code:
        return f"https://trudvsem.ru/vacancy/card/{company_code}/{external_id}"
    return None


def parse_vacancy(item, profile):
    external_id = item.get("id")
    if not isinstance(external_id, str) or not EXTERNAL_ID.fullmatch(external_id):
        return None
    company = item.get("company")
    company = company if isinstance(company, dict) else {}
    code = company.get("companycode")
    company_code = code if isinstance(code, str) and EXTERNAL_ID.fullmatch(code) else None
    url = vacancy_url(item.get("vac_url"), external_id, company_code)
    title, duty = item.get("job-name"), item.get("duty")
    if not url or not isinstance(title, str) or not isinstance(duty, str):
        return None
    requirement = item.get("requirement")
    education = requirement.get("education") if isinstance(requirement, dict) else None
    requirements = item.get("requirements")
    card = build_vacancy({
        "name": title, "description": duty,
        "snippet": {"requirement": requirements if isinstance(requirements, str) else ""},
        "published_at": item.get("creation-date") if isinstance(item.get("creation-date"), str) else None,
        "employer": {"id": f"trudvsem:{company_code}"} if company_code else {},
    }, profile.profession_id, profile, f"trudvsem:{external_id}", url)
    if card is None:
        return None

    sections = {"tasks": [], "requirements": [], "conditions": []}
    for field, source, raw in (
        ("tasks", "duty", duty),
        ("requirements", "requirements", requirements),
        ("requirements", "requirement.education", education),
        ("conditions", "employment", item.get("employment")),
        ("conditions", "schedule", item.get("schedule")),
    ):
        if not isinstance(raw, str):
            continue
        value = card["description"] if source == "duty" else clean_html(raw)
        items = sections[field]
        for line in value.splitlines():
            if len(items) == 4:
                break
            quote = line[:500]
            if all(quote != existing for existing, _ in items):
                items.append((quote, source))
    card = with_sections(card, sections)
    source_fields = {"description": "duty", "snippet.requirement": "requirements", "name": "job-name"}
    for evidence in card["evidence"]:
        evidence["source_field"] = source_fields.get(evidence["source_field"], evidence["source_field"])
    return {**card, "source": "trudvsem"}


def fetch_page(client, term, page, *, deadline):
    params = {"text": term, "offset": page, "limit": PAGE_SIZE}
    for attempt in range(2):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TrudvsemError("TRUDVSEM_COLLECTION_TIMEOUT")
        try:
            response = client.get(API, params=params, timeout=min(REQUEST_TIMEOUT, remaining))
            break
        except httpx.TimeoutException:
            if monotonic() >= deadline:
                raise TrudvsemError("TRUDVSEM_COLLECTION_TIMEOUT") from None
            if attempt == 1:
                raise TrudvsemError("TRUDVSEM_REQUEST_TIMEOUT") from None
        except httpx.RequestError as exc:
            raise TrudvsemError(DNS_ERROR if is_dns_failure(exc) else "TRUDVSEM_UNAVAILABLE") from None
    if monotonic() >= deadline:
        raise TrudvsemError("TRUDVSEM_COLLECTION_TIMEOUT")
    if response.status_code == 429:
        raise TrudvsemError("TRUDVSEM_RATE_LIMITED")
    if not response.is_success:
        raise TrudvsemError("TRUDVSEM_UNAVAILABLE")
    try:
        data = response.json()
        status = data["status"]
        if str(status) == "429":
            raise TrudvsemError("TRUDVSEM_RATE_LIMITED")
        if str(status) != "200":
            raise TrudvsemError("TRUDVSEM_UNAVAILABLE")
        results = data.get("results")
        records = results.get("vacancies") if isinstance(results, dict) else None
        if records is None and str(data.get("meta", {}).get("total")) == "0":
            return []
        if not isinstance(records, list) or not all(isinstance(record.get("vacancy"), dict) for record in records):
            raise TrudvsemError("TRUDVSEM_INVALID_RESPONSE")
    except (ValueError, AttributeError, KeyError, TypeError):
        raise TrudvsemError("TRUDVSEM_INVALID_RESPONSE") from None
    return records


def gather_cards(client, profile, *, limit=MAX_VACANCY_SAMPLE_LIMIT):
    deadline = monotonic() + COLLECTION_BUDGET
    terms = list(search_terms(profile))
    cards = {}
    for page in range(MAX_PAGES):
        next_terms = []
        for term in terms:
            records = fetch_page(client, term, page, deadline=deadline)
            for record in records:
                item = record["vacancy"]
                if f"trudvsem:{item.get('id')}" in cards:
                    continue
                card = parse_vacancy(item, profile)
                if card:
                    cards.setdefault(card["id"], card)
                    if len(cards) == limit:
                        return list(cards.values())
            if len(records) >= PAGE_SIZE:
                next_terms.append(term)
        if not next_terms:
            break
        terms = next_terms
    return list(cards.values())


def collect_trudvsem(sessions, settings, profession):
    if not settings.trudvsem_collection_enabled:
        return {"status": "skipped", "code": "TRUDVSEM_DISABLED"}
    with sessions() as db:
        profile = db.get(VacancyCollection, profession, options=[load_only(
            VacancyCollection.title, VacancyCollection.aliases, VacancyCollection.config_version,
        )])
    try:
        with httpx.Client(follow_redirects=False) as client:
            cards = gather_cards(client, profile, limit=settings.vacancy_sample_limit)
    except TrudvsemError as exc:
        logger.warning("Work in Russia collection failed for %s: %s", profession, exc)
        return {"status": "failed", "code": str(exc)}
    if not cards:
        return {"status": "failed", "code": "TRUDVSEM_EMPTY_SAMPLE"}

    stats = {**sample_stats(cards, None), "source": "trudvsem", "config_version": profile.config_version}
    return {"status": "ok", "collected": stats["vacancies"], "employers": stats["employers"],
            "sample": (cards, stats)}


def lock_current_profile(db, profession, version):
    # Follow the same lock order as starting a run. An edit invalidates the pending sample.
    return bool(db.execute(update(VacancyCollection).where(
        VacancyCollection.profession_id == profession, VacancyCollection.config_version == version,
    ).values(profession_id=profession)).rowcount)


def save_sample(db, profession, run_id, collected, cards, stats):
    profile = db.get(VacancyCollection, profession)
    stats = {**stats, "review": vacancy_review(cards, profile.approved)}
    db.add(VacancySnapshot(id=run_id, profession_id=profession, collected_at=collected, stats=stats, cards=cards))
    db.flush()
    pointer = db.get(VacancySourceSample, (profession, "trudvsem"))
    if pointer is None:
        db.add(VacancySourceSample(profession_id=profession, source="trudvsem", snapshot_id=run_id))
    else:
        pointer.snapshot_id = run_id
