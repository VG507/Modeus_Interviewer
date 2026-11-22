"""Collect versioned ESCO occupation profiles and match a small Russian vocabulary."""

import json
import re
from time import monotonic
from urllib.parse import urlencode

import httpx

from backend.db import EscoProfile, VacancyCollection
from backend.discovery_fetch import DNS_ERROR, is_dns_failure
from backend.textnorm import SKILL_ALIASES, clean_key
from backend.vacancy_context import profession_for_question
from backend.vacancy_parser import plain


API = "https://ec.europa.eu/esco/api/"
CONCEPT_URI = re.compile(
    r"https?://data\.europa\.eu/esco/(occupation|skill)/"
    r"([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})", re.IGNORECASE,
)
DEFAULT_TARGETS = {
    "python_backend": "software developer",
    "frontend": "web developer",
    "qa": "software tester",
    "devops": "ICT system administrator",
}
RELATIONS = {"hasEssentialSkill": "essential", "hasOptionalSkill": "optional"}
# Local labels and phrases. Keep technology names and ESCO's original term alongside them.
RUSSIAN_SKILLS = {
    "python computer programming": ("Программирование на Python", (
        *SKILL_ALIASES["python"], "питоне", "питоном",
    )),
    "javascript": ("Программирование на JavaScript", SKILL_ALIASES["javascript"]),
    "javascript computer programming": ("Программирование на JavaScript", SKILL_ALIASES["javascript"]),
    "typescript": ("Программирование на TypeScript", SKILL_ALIASES["typescript"]),
    "sql": ("SQL", SKILL_ALIASES["sql"]),
    "perform software unit testing": ("Модульное тестирование", (
        "модульное тестирование", "модульные тесты", "юнит-тесты", "unit tests", "unit testing",
    )),
    "perform software tests": ("Тестирование ПО", ("тестирование по", "тестирование api", "тестировали api")),
    "perform software testing": ("Тестирование ПО", ("тестирование по", "тестирование api", "тестировали api")),
    "debug software": ("Отладка ПО", ("отладка", "отладку", "отлаживали")),
    "use software design patterns": ("Применение паттернов проектирования", ("паттерны проектирования",)),
}


class EscoError(RuntimeError):
    pass


def target_for(profile):
    override = (profile.discovery_searches or {}).get("esco")
    if override:
        return override["target"], override["match"]
    target = DEFAULT_TARGETS.get(profile.profession_id)
    return (target, "broader") if target else ("", "exact")


def concept_uri(value, kind):
    match = CONCEPT_URI.fullmatch(value) if isinstance(value, str) else None
    if not match or match[1].lower() != kind:
        return None
    return f"http://data.europa.eu/esco/{kind}/{match[2].lower()}"


def classification_url(uri):
    page = "skills" if "/skill/" in uri else "occupation"
    return f"https://esco.ec.europa.eu/en/classification/{page}?{urlencode({'uri': uri})}"


def get_json(client, path, deadline, **params):
    if monotonic() >= deadline:
        raise EscoError("ESCO_TIMEOUT")
    try:
        with client.stream("GET", path, params=params) as response:
            if response.status_code == 404:
                raise EscoError("ESCO_NOT_FOUND")
            if response.status_code == 429:
                raise EscoError("ESCO_RATE_LIMITED")
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                if monotonic() >= deadline:
                    raise EscoError("ESCO_TIMEOUT")
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise EscoError("ESCO_INVALID_RESPONSE")
        result = json.loads(body)
    except httpx.TimeoutException:
        raise EscoError("ESCO_TIMEOUT") from None
    except httpx.DecodingError:
        raise EscoError("ESCO_INVALID_RESPONSE") from None
    except httpx.HTTPError as exc:
        raise EscoError(DNS_ERROR if is_dns_failure(exc) else "ESCO_UNAVAILABLE") from None
    except (ValueError, RecursionError):
        raise EscoError("ESCO_INVALID_RESPONSE") from None
    if not isinstance(result, dict):
        raise EscoError("ESCO_INVALID_RESPONSE")
    return result


def section(entry, key):
    value = entry.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EscoError("ESCO_INVALID_RESPONSE")
    return value


def title(entry):
    value = entry.get("title") or section(entry, "preferredLabel").get("en")
    return value.strip()[:240] if isinstance(value, str) else ""


def entry_uri(entry, kind):
    return concept_uri(entry.get("uri") or section(section(entry, "_links"), "self").get("uri"), kind)


def find_occupation(client, target, deadline):
    uri = concept_uri(target, "occupation")
    if uri:
        return uri
    payload = get_json(client, "search", deadline, text=target, type="occupation", full="false", limit=50)
    results = section(payload, "_embedded").get("results")
    if results is None and payload.get("total") == 0:
        results = []
    if not isinstance(results, list) or any(not isinstance(entry, dict) for entry in results):
        raise EscoError("ESCO_INVALID_RESPONSE")
    matches = set()
    for entry in results:
        if clean_key(title(entry)) == clean_key(target):
            uri = entry_uri(entry, "occupation")
            if not uri:
                raise EscoError("ESCO_INVALID_RESPONSE")
            matches.add(uri)
    if len(matches) != 1:
        raise EscoError("ESCO_AMBIGUOUS" if matches else "ESCO_NOT_FOUND")
    return matches.pop()


def description(entry):
    value = entry.get("description")
    if isinstance(value, dict):
        value = value.get("en")
    if isinstance(value, dict):
        value = value.get("literal")
    return plain(value)[:4000] if isinstance(value, str) else ""


def occupation_skills(entry):
    links, embedded = section(entry, "_links"), section(entry, "_embedded")
    skills = {}
    for relation, kind in RELATIONS.items():
        refs = embedded.get(relation, links.get(relation, []))
        if isinstance(refs, dict):
            refs = [refs]
        if not isinstance(refs, list):
            raise EscoError("ESCO_INVALID_RESPONSE")
        for ref in refs:
            if not isinstance(ref, dict):
                raise EscoError("ESCO_INVALID_RESPONSE")
            uri, name = entry_uri(ref, "skill"), title(ref)
            if not uri or not name:
                raise EscoError("ESCO_INVALID_RESPONSE")
            skill = skills.setdefault(uri, {"uri": uri, "title": name, "relations": []})
            if kind not in skill["relations"]:
                skill["relations"].append(kind)
    if not skills:
        raise EscoError("ESCO_NO_SKILLS")
    return list(skills.values())


def fetch_profile(target, version):
    deadline = monotonic() + 20
    with httpx.Client(base_url=API, timeout=6, follow_redirects=True, max_redirects=2,
                      headers={"Accept": "application/json", "User-Agent": "Modeus-Interviewer/1.0"},
                      params={"language": "en", "selectedVersion": version, "viewObsolete": "false"}) as client:
        uri = find_occupation(client, target, deadline)
        entry = get_json(client, "resource/occupation", deadline, uri=uri)
    name = title(entry)
    if entry_uri(entry, "occupation") != uri or not name:
        raise EscoError("ESCO_INVALID_RESPONSE")
    if entry.get("status") in ("obsolete", "deprecated") or entry.get("obsolete") is True:
        raise EscoError("ESCO_NOT_FOUND")
    aliases = section(entry, "alternativeLabel").get("en", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    return {
        "uri": uri, "title": name, "description": description(entry),
        "code": entry["code"][:64] if isinstance(entry.get("code"), str) else None,
        "aliases": [alias[:240] for alias in aliases[:100] if isinstance(alias, str)] if isinstance(aliases, list) else [],
    }, occupation_skills(entry)


def collect_esco(sessions, settings, profession):
    if not settings.esco_collection_enabled:
        return {"status": "skipped", "code": "ESCO_DISABLED"}
    with sessions() as db:
        target, match = target_for(db.get(VacancyCollection, profession))
    if not target:
        return {"status": "skipped", "code": "ESCO_NOT_CONFIGURED"}
    try:
        occupation, skills = fetch_profile(target, settings.esco_version)
    except EscoError as exc:
        return {"status": "failed", "code": str(exc)}
    return {"status": "ok", "collected": len(skills), "profile": {
        "target": target, "match": match, "version": settings.esco_version, "occupation": occupation, "skills": skills,
    }}


def localized_skill(skill):
    translation = RUSSIAN_SKILLS.get(clean_key(skill["title"]))
    return {**skill, "url": classification_url(skill["uri"]), "title_ru": translation[0] if translation else None}


def hints_for_question(db, settings, intake, state, intent, answer):
    if not settings.esco_collection_enabled:
        return []
    profession = profession_for_question(db, intake, state, intent)
    if not profession:
        return []
    profile = db.get(VacancyCollection, profession)
    snapshot = db.get(EscoProfile, profession)
    if not snapshot or snapshot.version != settings.esco_version or (snapshot.target, snapshot.match) != target_for(profile):
        return []
    case = clean_key(" ".join([answer[:1200], *(fact["text"][:500] for fact in state["facts"][-12:])]))
    matches = []
    for skill in snapshot.skills:
        translation = RUSSIAN_SKILLS.get(clean_key(skill["title"]))
        if translation and any(re.search(rf"(?<!\w){re.escape(clean_key(alias))}(?!\w)", case)
                               for alias in translation[1]):
            if translation[0] not in matches:
                matches.append(translation[0])
            if len(matches) == 3:
                break
    return matches
