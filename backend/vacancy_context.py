"""Find market context that matches an established interview case."""

import re

from sqlalchemy import select
from sqlalchemy.orm import load_only

from backend.db import VacancyCollection, now
from backend.vacancy_import import FRESH_FOR
from backend.vacancy_parser import canonical_skill, level_of, profile_matches, skill_hits


QUESTION_INTENTS = {"action", "decision", "verification", "context"}
STOP_WORDS = {"быстро", "работа", "работы", "работал", "работать", "система", "системе", "задача", "задачи",
              "команда", "команде", "проект", "проекта", "новый", "после", "сделал", "разработка", "компания"}
TASK_SIGNALS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:explain|план\w* выполнени\w*|"
    r"(?:индекс\w*|медл\w*|оптимиз\w*|задерж\w*)[^.!?\n]{0,75}(?:запрос\w*|отч[её]т\w*|postgresql)|"
    r"(?:запрос\w*|отч[её]т\w*)[^.!?\n]{0,75}(?:индекс\w*|медл\w*|тормоз\w*|долго|\d+\s*(?:мс|секунд\w*|минут\w*)))\b",
    r"\b(?:core web vitals|lighthouse|lcp|"
    r"(?:медл\w*|оптимиз\w*|ускор\w*|уменьш\w*|размер\w*)[^.!?\n]{0,75}(?:бандл\w*|загрузк\w* страниц\w*)|"
    r"страниц\w*[^.!?\n]{0,75}(?:долго|медл\w*)\s+(?:откры\w*|загруж\w*))\b",
))


def keywords(text):
    skills = {word.casefold() for _, quote in skill_hits(text) for word in re.findall(r"\w+", quote)}
    words = set(re.findall(r"[^\W\d_]{5,}", text.casefold().replace("ё", "е")))
    return {word[:5] for word in words - STOP_WORDS - skills}


def task_signals(text):
    return {index for index, pattern in enumerate(TASK_SIGNALS) if pattern.search(text)}


def years_fit(experience, band):
    if not band:
        return True
    if re.search(r"\bбез опыта\b", experience, re.IGNORECASE):
        return band == "noExperience"
    match = re.fullmatch(r"\s*(\d{1,2})(?:[,.](\d+))?\s*(?:года?|лет|years?)?\s*", experience, re.IGNORECASE)
    if not match:
        return True
    years = float(f"{match[1]}.{match[2] or 0}")
    lower, upper = {"noExperience": (0, 1), "between1And3": (1, 3),
                    "between3And6": (3, 6), "moreThan6": (6, 100)}[band]
    return lower <= years <= upper


def profession_for_question(db, intake, state, intent):
    if intent not in QUESTION_INTENTS or (intent == "verification" and any(
        fact["field"] == "error" for fact in state["facts"]
    )):
        return None
    role = intake.get("role", "")
    profiles = [row for row in db.scalars(select(VacancyCollection).where(
        VacancyCollection.approved.is_(True),
    ).options(load_only(VacancyCollection.profession_id, VacancyCollection.title, VacancyCollection.aliases)))
                if profile_matches(row.profession_id, role, row)]
    profiles = [row for row in profiles if row.aliases is not None] or profiles
    if len(profiles) == 1 and any(fact["field"] == "task" for fact in state["facts"]):
        return profiles[0].profession_id
    return None


def sources_for_question(db, intake, state, intent, *, profession_id=None):
    profession_id = profession_id or profession_for_question(db, intake, state, intent)
    if profession_id is None:
        return []
    collection = db.get(VacancyCollection, profession_id)
    if not collection or not collection.updated_at or now() - collection.updated_at > FRESH_FOR:
        return []

    task_text = "\n".join(fact["text"][:500] for fact in state["facts"] if fact["field"] == "task")
    case_text = task_text + "\n" + "\n".join(fact["text"][:500] for fact in state["facts"][-14:] if fact["field"] != "task")
    normalized_case = " ".join(case_text.casefold().split())
    task_words, case_words = keywords(task_text), keywords(case_text)
    signals = task_signals(case_text)
    case_skills = {skill for skill, _ in skill_hits(case_text)}
    level = level_of(intake.get("role", ""))
    matches = []
    for card in collection.cards:
        if level and card["level"] and level != card["level"]:
            continue
        if not years_fit(intake.get("experience", ""), card["experience_band"]):
            continue
        matching_skills = sorted(case_skills.intersection(map(canonical_skill, card["skills"])))
        task, source_field, score = "", "", (0, 0, 0)
        task_citations = {item["field_path"]: item for item in card.get("evidence", [])
                          if item["field_path"].startswith("tasks.")}
        for index, candidate in enumerate(card["tasks"]):
            citation = task_citations.get(f"tasks.{index}")
            if not citation or citation["quote"] != candidate:
                continue
            if " ".join(candidate.casefold().split()) in normalized_case:
                continue
            words = keywords(candidate)
            candidate_score = (len(signals & task_signals(candidate)), len(task_words & words), len(case_words & words))
            if candidate_score > score:
                task, score = candidate, candidate_score
                source_field = citation["source_field"]
        signal_overlap, _, case_overlap = score
        if not (signal_overlap or case_overlap >= 2):
            continue
        matches.append(((*score, len(matching_skills)), {
            "vacancy_id": card["id"], "url": card["url"], "snapshot_at": collection.updated_at.isoformat() + "Z",
            "profession_id": collection.profession_id, "title": card["title"], "source_field": source_field,
            "task_excerpt": task[:220], "skills": matching_skills[:6],
        }))
    ranked = sorted(matches, key=lambda pair: pair[0], reverse=True)
    if ranked and ranked[0][0][0]:
        ranked = [pair for pair in ranked if pair[0][0]]
    return [hint for _, hint in ranked[:2]]
