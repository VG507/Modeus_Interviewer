"""Run the existing collectors for one profession and report their distinct outcomes."""

import logging
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import load_only

from backend.agent_skills import collect_skills
from backend.db import (
    DiscoveryCandidate, DiscoveryItem, DocumentationSubscription, EscoProfile, ProfessionCollectionRun,
    VacancyCollection, now,
)
from backend.discovery import ACTIVE_PROVIDERS, DEFAULT_REPOS, DiscoveryBusy, candidate_status, collect
from backend.discovery_fetch import DNS_ERROR, CollectionError, ProviderRateLimit, is_dns_failure
from backend.documentation import collect_documentation, iso_time
from backend.esco import classification_url, collect_esco, target_for
from backend.habr import collect_habr
from backend.hn import HackerNewsUnavailable, collect_hn, profile_terms
from backend.openalex import collect_research
from backend.trudvsem import collect_trudvsem, lock_current_profile, save_sample
from backend.vacancy_import import claim_collection, configured, run_collection
from backend.vacancy_samples import current_sample, sample_is_fresh, trudvsem_samples


logger = logging.getLogger(__name__)
SOURCES = ("hh", "trudvsem", "esco", "github", "trends", "docs", "hn", "openalex", "skills", "habr")
ACTIVE = {"queued", "running"}
RETRYABLE = {"partial", "failed", "busy", "interrupted", "skipped"}
VACANCY_SOURCES = {"hh", "trudvsem"}
VACANCY_DEPENDENTS = {"hn", "openalex"}
LEASE = timedelta(minutes=2)
GITHUB_DEFAULTS = {
    "python_backend": (DEFAULT_REPOS, ("python", "fastapi", "sqlalchemy")),
    "frontend": (("facebook/react", "vitejs/vite"), ("react", "frontend", "vite")),
    "qa": (("pytest-dev/pytest", "microsoft/playwright"), ("test-automation", "playwright", "pytest")),
    "devops": (("kubernetes/kubernetes", "ansible/ansible"), ("kubernetes", "devops", "ansible")),
}


def output(run):
    if run is None:
        return None
    running = run.lease_until is not None and run.lease_until > now()
    steps = {
        source: {"status": "interrupted"} if not running and step["status"] in ACTIVE else step
        for source, step in run.steps.items()
    }
    return {"id": run.run_id, "started_at": iso_time(run.started_at), "running": running, "steps": steps}


def selected_sources(only):
    if only is None:
        return SOURCES
    return (only,) if isinstance(only, str) else only


def start(db, profession, retry_id=None, source=None, *, only=None):
    # Lock the profession row so two clicks cannot enqueue overlapping runs.
    db.execute(update(VacancyCollection).where(VacancyCollection.profession_id == profession)
               .values(profession_id=profession))
    run = db.get(ProfessionCollectionRun, profession)
    current = output(run)
    if source:
        if (current is None or current["running"] or current["id"] != retry_id
                or current["steps"].get(source, {}).get("status") not in RETRYABLE):
            return None, None
        steps = {**current["steps"], source: {"status": "queued"}}
    else:
        if current and current["running"]:
            return current, None
        steps = {**(current["steps"] if only and current else {}),
                 **{key: {"status": "queued"} for key in selected_sources(only)}}
    if run is None:
        run = ProfessionCollectionRun(profession_id=profession)
        db.add(run)
    run.run_id = str(uuid4())
    run.started_at = now()
    run.lease_until = run.started_at + LEASE
    run.steps = steps
    return output(run), run.run_id


def change_steps(sessions, profession, run_id, changes):
    with sessions.begin() as db:
        trudvsem = changes.get("trudvsem", {})
        sample = trudvsem.get("sample")
        if sample:
            cards, stats = sample
            step = {key: value for key, value in trudvsem.items() if key != "sample"}
            if not lock_current_profile(db, profession, stats["config_version"]):
                sample = None
                step = {"status": "failed", "code": "TRUDVSEM_SUPERSEDED"}
            changes = {**changes, "trudvsem": step}
        esco = changes.get("esco", {})
        esco_profile = esco.get("profile")
        if esco_profile:
            db.execute(update(VacancyCollection).where(VacancyCollection.profession_id == profession)
                       .values(profession_id=profession))
            profile = db.get(VacancyCollection, profession)
            step = {key: value for key, value in esco.items() if key != "profile"}
            if (esco_profile["target"], esco_profile["match"]) != target_for(profile):
                esco_profile = None
                step = {"status": "failed", "code": "ESCO_SUPERSEDED"}
            changes = {**changes, "esco": step}
        run = db.get(ProfessionCollectionRun, profession)
        current = now()
        if run is None or run.run_id != run_id or run.lease_until is None or run.lease_until <= current:
            return False
        steps = {**run.steps, **changes}
        lease_until = current + LEASE if any(value["status"] in ACTIVE for value in steps.values()) else None
        changed = db.execute(update(ProfessionCollectionRun).where(
            ProfessionCollectionRun.profession_id == profession, ProfessionCollectionRun.run_id == run_id,
            ProfessionCollectionRun.lease_until > current,
        ).values(steps=steps, lease_until=lease_until)).rowcount
        if changed and sample:
            save_sample(db, profession, run_id, current, cards, stats)
        if changed and esco_profile:
            db.merge(EscoProfile(profession_id=profession, collected_at=current, **esco_profile))
        return bool(changed)


def refresh_sources(sessions, settings, profession, run_id, only=None):
    sources = selected_sources(only)
    # These collectors read the saved vacancy samples. Leave them queued while those samples are writing.
    waiting = VACANCY_DEPENDENTS if only is None else set()
    ready = [source for source in sources if source not in waiting]
    if not change_steps(sessions, profession, run_id, {source: {"status": "running"} for source in ready}):
        return

    def run(source):
        try:
            return refresh_source(sessions, settings, profession, source)
        except DiscoveryBusy:
            return {"status": "busy"}
        except ProviderRateLimit:
            return {"status": "failed", "code": "DISCOVERY_RATE_LIMIT"}
        except httpx.TimeoutException:
            return {"status": "failed", "code": "DISCOVERY_TIMEOUT"}
        except HackerNewsUnavailable as exc:
            logger.warning("Hacker News collection unavailable: %s", exc)
            return {"status": "failed", "code": DNS_ERROR if exc.dns_failure else "HN_UNAVAILABLE"}
        except (httpx.RequestError, OSError) as exc:
            return {"status": "failed", "code": DNS_ERROR if is_dns_failure(exc) else "DISCOVERY_UNAVAILABLE"}
        except Exception:
            logger.exception("Profession collection failed for %s (%s)", profession, source)
            return {"status": "failed", "code": "DISCOVERY_UNAVAILABLE"}

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        pending = {pool.submit(run, source): source for source in ready}
        while pending:
            finished, _ = wait(pending, timeout=20, return_when=FIRST_COMPLETED)
            changes = {pending.pop(task): task.result() for task in finished}
            if not change_steps(sessions, profession, run_id, changes):
                return
            if waiting and VACANCY_SOURCES.isdisjoint(pending.values()):
                if not change_steps(sessions, profession, run_id, {source: {"status": "running"} for source in waiting}):
                    return
                pending.update({pool.submit(run, source): source for source in waiting})
                waiting = set()


def refresh_source(sessions, settings, profession, source):
    if source == "trudvsem":
        return collect_trudvsem(sessions, settings, profession)
    if source == "esco":
        return collect_esco(sessions, settings, profession)
    if source == "skills":
        return collect_skills(sessions, settings, profession)
    if source == "openalex":
        return collect_research(sessions, settings, profession)
    if source == "habr":
        return collect_habr(sessions, settings, profession)
    if source == "hh":
        if not configured(settings):
            return {"status": "skipped", "code": "HH_NOT_CONFIGURED"}
        token = claim_collection(sessions, profession)
        if not token:
            return {"status": "busy"}
        result = run_collection(sessions, settings, profession, token)
        if result["status"] == "ok":
            return {"status": "ok", "collected": result["vacancies"], "employers": result["employers"]}
        if result["status"] == "superseded":
            return {"status": "interrupted", "code": "HH_SUPERSEDED"}
        return {"status": "failed", "code": result.get("error", "HH_TIMEOUT")}

    with sessions() as db:
        if source == "docs":
            if not db.scalar(select(DocumentationSubscription.watch_id).where(
                DocumentationSubscription.profession_id == profession,
            ).limit(1)):
                return {"status": "skipped", "code": "DOCS_NOT_CONFIGURED"}
        elif source == "hn":
            if not settings.hn_collection_enabled:
                return {"status": "skipped", "code": "HN_DISABLED"}
            try:
                profile_terms(db, profession, (), now())
            except CollectionError:
                return {"status": "skipped", "code": "HN_NOT_CONFIGURED"}
        else:
            profile = db.get(VacancyCollection, profession, options=[load_only(VacancyCollection.discovery_searches)])
            searches = profile.discovery_searches or {}
            repos, topics = GITHUB_DEFAULTS.get(profession, ((), ()))
            search = searches.get(source, {"values": repos if source == "github" else topics})
            if not search["values"]:
                return {"status": "skipped", "code": "GITHUB_NOT_CONFIGURED"}

    if source == "docs":
        result = collect_documentation(sessions, profession)
        status = "ok"
        if result["errors"] or result["busy"]:
            status = "partial" if result["collected"] else "failed" if result["errors"] else "busy"
        return {"status": status, **result}
    if source == "hn":
        result = collect_hn(sessions, profession)
        return {"status": "partial" if result["partial"] else "ok", **result}
    result = collect(sessions, settings, source, profession, search["values"], 2, search.get("limit", 10),
                     100, search.get("labels"))
    return {"status": "partial" if result["partial"] else "ok", **result}


def quality(db, profession, settings):
    current = now()
    profile = db.get(VacancyCollection, profession, options=[load_only(
        VacancyCollection.updated_at, VacancyCollection.stats, VacancyCollection.approved,
        VacancyCollection.config_version, VacancyCollection.discovery_searches,
    )])
    stamp = profile.updated_at
    result = {"hh": {
        "checked_at": iso_time(stamp),
        "vacancies": profile.stats.get("vacancies", 0),
        "employers": profile.stats.get("employers", 0),
        "fresh": sample_is_fresh(stamp, current),
        "approved": profile.approved,
        "current_config": current_sample(profile.stats, profile),
        "source": profile.stats.get("source"),
        "review": profile.stats.get("review"),
    }, "automatic_check_enabled": bool(settings.llm_base_url and settings.llm_model)}
    target, match = target_for(profile)
    snapshot = db.get(EscoProfile, profession)
    result["esco"] = {
        "enabled": settings.esco_collection_enabled, "target": target, "match": match, "sample": None,
    }
    if snapshot:
        result["esco"]["sample"] = {
            "checked_at": iso_time(snapshot.collected_at), "version": snapshot.version,
            "title": snapshot.occupation["title"], "url": classification_url(snapshot.occupation["uri"]),
            "skills": len(snapshot.skills), "match": snapshot.match,
            "current_config": (snapshot.target, snapshot.match, snapshot.version) == (target, match, settings.esco_version),
        }
    trudvsem = trudvsem_samples(db, profession, include_cards=False).get(profession)
    if trudvsem:
        result["trudvsem"] = {
            "checked_at": iso_time(trudvsem.collected_at),
            "vacancies": trudvsem.stats["vacancies"],
            "employers": trudvsem.stats["employers"],
            "fresh": sample_is_fresh(trudvsem.collected_at, current),
            "approved": profile.approved,
            "current_config": current_sample(trudvsem.stats, profile),
            "source": "trudvsem",
            "review": trudvsem.stats.get("review"),
        }
    # Read only evidence state, irrespective of the current page or list filter.
    subscribed = select(DocumentationSubscription.watch_id).where(
        DocumentationSubscription.profession_id == profession,
        DocumentationSubscription.watch_id == DiscoveryItem.payload["watch_id"].as_string(),
    ).exists()
    fields = (DiscoveryItem.provider, DiscoveryCandidate.status, DiscoveryCandidate.reviewed_digest == DiscoveryItem.digest,
              DiscoveryCandidate.valid_until <= current, DiscoveryCandidate.valid_until.is_(None),
              DiscoveryItem.payload["reason"].as_string().is_not(None),
              DiscoveryItem.payload["previous_digest"].as_string().is_not(None),
              DiscoveryItem.payload["kind"].as_string(), subscribed)
    counts = {key: {} for key in ACTIVE_PROVIDERS}
    documentation_pending = 0
    query = select(*fields, func.count()).join(DiscoveryCandidate).where(
        DiscoveryCandidate.profession_id == profession, DiscoveryItem.provider.in_(ACTIVE_PROVIDERS),
    ).group_by(*fields)
    for provider, stored, reviewed, expired, no_expiry, unavailable, revised, kind, watched, count in db.execute(query):
        status = candidate_status(provider, stored, reviewed, expired or no_expiry, unavailable, revised)
        totals = counts[provider]
        totals[status] = totals.get(status, 0) + count
        if provider == "docs" and kind == "documentation" and watched and status in {"new", "review", "expired"}:
            documentation_pending += count
    return {**result, "materials": counts, "documentation_pending": documentation_pending}
