"""Ground private card links and select bounded context for follow-up questions."""

import re

from sqlalchemy import select

from backend.db import KnowledgeCard
from backend.demo_auth import card_owners
from backend.vacancy_context import keywords, profession_for_question
from backend.vacancy_parser import SKILL_PATTERN, canonical_skill, overlaps_task, task_key
from backend.vectors import SIMILAR_MIN_SCORE


SECTIONS = ("task", "context", "actions", "decisions", "verification", "errors", "limitations")
PRACTICE_SECTIONS = ("actions", "verification")
USE = re.compile(
    r"\b(?:использовал\w*|применял\w*|применил\w*|настроил\w*|запустил\w*|"
    r"проверил\w*|проверял\w*|тестировал\w*|развернул\w*|добавил\w*|used|ran|configured|tested|deployed)\b",
    re.IGNORECASE,
)
QUALIFIED = re.compile(
    r"\b(?:не|без|вместо|рассматривал\w*|планировал\w*|попробовал\w*|коллег\w*|"
    r"чуж\w*|мог\w*|можно|если|бы|только\s+план\w*|not|without|instead|would|could|if|considered)\b",
    re.IGNORECASE,
)
CHECK = re.compile(r"^(?:провер|тест|tested)", re.IGNORECASE)
TOOL_MARKER = re.compile(r"\b(?:в|через|с помощью|with|using)\s+$", re.IGNORECASE)
SAME_PROJECT = re.compile(
    r"^\s*(?:да[, ]+)?(?:в том же проекте|это тот же проект|тот же проект|"
    r"in the same project|this was the same project)\b",
    re.IGNORECASE,
)
DIFFERENT_PROJECT = re.compile(r"\b(?:не|друг\w+|нов\w+|отдельн\w+)\s+проект\w*\b|\b(?:different|another|new) project\b",
                               re.IGNORECASE)
OLDER_CASE = re.compile(r"\b(?:перв\w+|начальн\w+|first)\s+(?:кейс\w*|случа\w*|case)\b", re.I)
CONDITIONS = {
    "тестовая среда": r"\b(?:тестов\w+\s+(?:стенд\w*|окружени\w*|сред\w*)|staging)\b",
    "нагрузка и объём данных": r"\b(?:нагрузк\w*|объ[её]м\w*\s+данн\w*|data volume)\b",
    "ограничения доступа": r"\b(?:доступ\w*|разрешени\w*|permissions)\b",
    "версии инструментов": r"\b(?:верси\w*\s+\d|version\s+\d)\b",
}


def fact_skills(fact, section):
    if (section not in PRACTICE_SECTIONS or fact.get("source", "interview") != "interview"
            or not fact.get("supporting_message_ids")
            or (section == "actions" and fact.get("responsibility") not in {"personal", "shared"})):
        return []
    skills = set()
    for sentence in re.split(r"[!?;\n]|\.(?=\s|$)", fact["text"]):
        if QUALIFIED.search(sentence) or any(mark in sentence for mark in ('"', "«", "»")):
            continue
        for match in SKILL_PATTERN.finditer(sentence):
            prefix = sentence[:match.start()]
            use = list(USE.finditer(prefix))
            if (use and re.search(r"(?:^\s*|\b(?:я|мы|i|we)\s+)$", prefix[:use[-1].start()], re.I)
                    and re.fullmatch(r"(?:\s+\w+){0,5}\s+", prefix[use[-1].end():])
                    and (not CHECK.match(use[-1].group()) or TOOL_MARKER.search(prefix[use[-1].end():]))):
                skills.add(canonical_skill(match.group()))
    return sorted(skills)


def cases_of(content):
    yield content
    yield from content.get("cases") or ()


def published_cards():
    return select(KnowledgeCard).where(
        KnowledgeCard.owner_id.in_(card_owners(None)),
        KnowledgeCard.publication_status == "published", KnowledgeCard.review_status == "reviewed",
        KnowledgeCard.consent["content_version"].as_integer() == KnowledgeCard.content_version,
        KnowledgeCard.review["content_version"].as_integer() == KnowledgeCard.content_version,
        KnowledgeCard.review["decision"].as_string() == "accept",
    )


def prior_case_context(state, answer, previous_answers=()):
    past = state.get("past_cases", [])
    if not past:
        return []
    references = [answer, *previous_answers]
    latest = next((text for text in references if SAME_PROJECT.match(text) or DIFFERENT_PROJECT.search(text)), "")
    if (not SAME_PROJECT.match(latest) or DIFFERENT_PROJECT.search(latest)
            or (len(past) > 1 and OLDER_CASE.search(latest))):
        return []
    return [fact["text"][:240] for fact in past[-1]["state"]["facts"]
            if fact["field"] in {"context", "limitation"}][-3:]


def published_context(db, interview, state, intent, case_vector=None):
    if intent not in {"action", "decision", "verification", "context"}:
        return {}, {}
    profession = profession_for_question(db, interview.context, state, intent)
    if not profession:
        return {}, {}
    tasks = [fact["text"] for fact in state["facts"] if fact["field"] == "task"]
    current_skills = {skill for fact in state["facts"] for skill in fact_skills(
        fact, "actions" if fact["field"] == "action" else fact["field"],
    )}
    if not current_skills:
        return {}, {}
    candidates = list(db.scalars(published_cards().where(
        KnowledgeCard.origin == "human", KnowledgeCard.catalog_profession_id == profession,
        KnowledgeCard.owner_id != interview.owner_id, KnowledgeCard.session_id != interview.id,
    ).order_by(KnowledgeCard.updated_at.desc(), KnowledgeCard.id).limit(24)))
    # Another person's case of the same kind of problem counts even when it shares no words with this one.
    meaning = case_vector.case_scores(db, [card.id for card in candidates]) if case_vector else {}
    current_text = "\n".join(fact["text"] for fact in state["facts"])
    conditions_to_check = {label: pattern for label, pattern in CONDITIONS.items()
                           if not re.search(pattern, current_text, re.I)}
    matches, gap_skills, conditions = {}, set(), set()
    for card in candidates:
        for index, case in enumerate(cases_of(card.content)):
            if intent not in case.get("unknown_fields", ()):
                continue
            case_skills = {skill for section in PRACTICE_SECTIONS for fact in case.get(section, ())
                           for skill in fact_skills(fact, section)}
            shared = case_skills & current_skills
            grounded = [other["text"] for other in case.get("task", ())
                        if other.get("source", "interview") == "interview" and other.get("supporting_message_ids")]
            similar = meaning.get((card.id, index), 0.0) >= SIMILAR_MIN_SCORE
            if not shared or not grounded or (not similar and not any(
                len(keywords(task) & keywords(other)) >= 2 and overlaps_task(task_key(task), task_key(other))
                for task in tasks for other in grounded
            )):
                continue
            matches[card.id] = card.version
            gap_skills.update(shared)
            condition_text = "\n".join(fact["text"] for section in ("context", "limitations")
                                       for fact in case.get(section, ())
                                       if fact.get("source", "interview") == "interview"
                                       and fact.get("supporting_message_ids"))
            conditions.update(label for label, pattern in conditions_to_check.items()
                              if re.search(pattern, condition_text, re.I))
        if len(matches) == 3:
            break
    return ({"aspect": intent, "shared_skills": sorted(gap_skills)[:3],
             "conditions_to_clarify": sorted(conditions)[:2]}, matches) if matches else ({}, {})


def publication_versions_current(db, selected_versions):
    confirmed = db.execute(published_cards().with_only_columns(KnowledgeCard.id, KnowledgeCard.version).where(
        KnowledgeCard.id.in_(selected_versions),
    ).order_by(KnowledgeCard.id).with_for_update()).all()
    return dict(confirmed) == selected_versions
