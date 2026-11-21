"""Import reviewed source excerpts and select evidence for a specific follow-up."""

import hashlib
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, TypeAdapter, model_validator
from sqlalchemy import func, or_, select

from backend.db import DocumentationSubscription, DiscoveryCandidate, DiscoveryItem, Source, SourceExcerpt, VacancyCollection, now
from backend.interview_schemas import Identifier, Schema, ShortText
from backend.vacancy_context import keywords, profession_for_question, sources_for_question, task_signals
from backend.vacancy_parser import SKILL_ALIASES, canonical_skill


SOURCE_PURPOSES = {
    "action": ("implementation", "behavior"),
    "decision": ("implementation", "effect", "behavior"),
    "verification": ("effect", "behavior", "implementation"),
    "context": ("behavior", "implementation", "effect"),
}
PREFERRED_KIND = {"behavior": "documentation", "effect": "research", "implementation": "engineering"}
VERSION_PATTERN = r"\d+(?:\.\d+){0,3}"
ExcerptText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


def normalize(text):
    return " ".join(text.casefold().replace("ё", "е").split())


def phrase_pattern(phrase):
    return rf"(?<!\w){re.escape(normalize(phrase))}(?!\w)"


def condition_matches(condition, case):
    pattern = phrase_pattern(condition)
    return re.search(pattern, case) is not None and not re.search(rf"\b(?:не|без|no|without)\s+{pattern}", case)


class ReviewedExcerpt(Schema):
    id: Identifier
    profession_id: Identifier
    title: ShortText
    url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2048)]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200_000)]
    kind: Literal["documentation", "research", "engineering", "discussion"]
    purpose: Literal["behavior", "effect", "implementation"]
    task: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    quote: ExcerptText
    detail: Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=200)]
    technology: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] | None = None
    version: Annotated[str, StringConstraints(pattern=rf"^{VERSION_PATTERN}$")] | None = None
    conditions: list[ShortText] = Field(default_factory=list, max_length=8)
    method: ExcerptText | None = None

    @model_validator(mode="after")
    def verify_evidence(self):
        if any(char.isspace() or char == "\\" or ord(char) < 32 for char in self.url):
            raise ValueError("use an HTTP(S) source URL")
        url = urlsplit(self.url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username is not None:
            raise ValueError("use an HTTP(S) source URL without credentials")
        if self.quote not in self.text or self.detail not in self.quote:
            raise ValueError("quote must occur in the text and detail must occur in the quote")
        if self.version and not self.technology:
            raise ValueError("a version requires a technology")
        if self.kind == "research" and self.purpose == "effect" and (not self.conditions or not self.method):
            raise ValueError("research on an effect requires conditions and a measurement method")
        if self.method and self.method not in self.text:
            raise ValueError("the measurement method must occur in the source text")
        return self


def import_excerpts(db, data):
    excerpts = TypeAdapter(list[ReviewedExcerpt]).validate_python(data)
    if len({excerpt.id for excerpt in excerpts}) != len(excerpts):
        raise ValueError("duplicate excerpt ID in import")
    for profession_id in {excerpt.profession_id for excerpt in excerpts}:
        if db.get(VacancyCollection, profession_id) is None:
            raise ValueError(f"unknown profession: {profession_id}")
    for excerpt in excerpts:
        source_id = hashlib.sha256(f"{excerpt.url}\n{excerpt.text}".encode()).hexdigest()
        if db.get(Source, source_id) is None:
            db.add(Source(id=source_id, url=excerpt.url, text=excerpt.text, retrieved_on=now().date().isoformat()))
            db.flush()
        db.merge(SourceExcerpt(source_id=source_id, **excerpt.model_dump(exclude={"text", "url"})))
    return len(excerpts)


def technology_fits(excerpt, case):
    if not excerpt.technology:
        return True
    skill = canonical_skill(excerpt.technology)
    aliases = SKILL_ALIASES.get(skill, (excerpt.technology,))
    pattern = "(?:" + "|".join(phrase_pattern(alias) for alias in aliases) + ")"
    if not excerpt.version:
        return re.search(pattern, case) is not None
    versions = re.findall(rf"{pattern}\s+(?:(?:верси[ияю]|version)\s+)?v?({VERSION_PATTERN})(?![\w+-]|\.\d)", case)
    expected = excerpt.version.split(".")
    return bool(versions) and all(version.split(".")[:len(expected)] == expected for version in versions)


def sources_for_followup(db, intake, state, intent, answer_text=""):
    profession_id = profession_for_question(db, intake, state, intent)
    if profession_id is None:
        return []

    task_text = "\n".join(fact["text"] for fact in state["facts"] if fact["field"] == "task")
    case_parts = [fact["text"] for fact in state["facts"]]
    case_parts.extend(turn["answer"] for turn in state.get("recent_history", []))
    case_parts.append(answer_text)
    case_text = "\n".join(case_parts)
    case = normalize(case_text)
    task_words, signals = keywords(task_text), task_signals(task_text)
    project_stacks = [project.get("stack", "") for project in intake.get("projects", [])
                      if len(task_words & keywords(project.get("task", ""))) >= 2]
    environment = normalize("\n".join([case_text, *project_stacks]))
    purposes = SOURCE_PURPOSES[intent]
    matches = []
    subscribed = select(DocumentationSubscription.watch_id).where(
        DocumentationSubscription.profession_id == profession_id,
        DocumentationSubscription.watch_id == DiscoveryItem.payload["watch_id"].as_string(),
    ).exists()
    evidence_date = func.coalesce(DiscoveryItem.payload["publication_date"].as_string(),
                                  DiscoveryItem.payload["closed_at"].as_string(),
                                  DiscoveryCandidate.assessment["checked_at"].as_string(), Source.retrieved_on)
    query = (select(
        SourceExcerpt, Source.url, evidence_date, DiscoveryItem.provider, DiscoveryItem.id, DiscoveryItem.digest,
    ).join(Source).where(
        SourceExcerpt.profession_id == profession_id, SourceExcerpt.purpose.in_(purposes),
    ).outerjoin(DiscoveryCandidate, DiscoveryCandidate.excerpt_id == SourceExcerpt.id)
        .outerjoin(DiscoveryItem, DiscoveryItem.id == DiscoveryCandidate.item_id).where(or_(
            DiscoveryCandidate.item_id.is_(None),
            DiscoveryItem.provider.in_(("github", "docs", "openalex", "skills")) & (DiscoveryCandidate.status == "accepted")
            & (DiscoveryCandidate.reviewed_digest == DiscoveryItem.digest)
            & (DiscoveryCandidate.valid_until > now()) & or_(DiscoveryItem.provider != "docs", subscribed),
        )))
    if intent != "verification":
        query = query.where(or_(DiscoveryItem.provider.is_(None), DiscoveryItem.provider != "skills"))
    for excerpt, url, dated, provider, item_id, digest in db.execute(query.order_by(SourceExcerpt.id)):
        shared_signals = signals & task_signals(excerpt.task)
        shared_words = task_words & keywords(excerpt.task)
        if not shared_signals and len(shared_words) < 2:
            continue
        if not technology_fits(excerpt, environment) or not all(
            condition_matches(condition, case) for condition in excerpt.conditions
        ):
            continue
        if re.search(phrase_pattern(excerpt.detail), case) or re.search(phrase_pattern(excerpt.quote), case):
            continue
        score = (-purposes.index(excerpt.purpose), excerpt.kind == PREFERRED_KIND[excerpt.purpose],
                 len(shared_signals), len(shared_words), bool(excerpt.version), len(excerpt.conditions), dated)
        matches.append((score, {
            "source_id": excerpt.source_id, "excerpt_id": excerpt.id, "profession_id": profession_id,
            "kind": excerpt.kind, "purpose": excerpt.purpose, "title": excerpt.title, "url": url,
            "task_excerpt": excerpt.quote, "detail": excerpt.detail, "version": excerpt.version,
            "skills": [cs] if excerpt.technology and (cs := canonical_skill(excerpt.technology)) else [],
            **({"provider": provider, "item_id": item_id, "revision_digest": digest} if provider == "skills" else {}),
        }))
    if not matches:
        return sources_for_question(db, intake, state, intent, profession_id=profession_id)
    ranked = sorted(matches, key=lambda match: match[0], reverse=True)
    hints, seen_details = [], set()
    for _, hint in ranked:
        detail = normalize(hint["detail"])
        if detail not in seen_details:
            hints.append(hint)
            seen_details.add(detail)
        if len(hints) == 2:
            break
    return hints


def skill_sources_current(db, sources):
    for source in sources:
        current = db.scalar(select(DiscoveryItem.id).join(DiscoveryCandidate).join(
            SourceExcerpt, SourceExcerpt.id == DiscoveryCandidate.excerpt_id,
        ).where(
            DiscoveryItem.provider == "skills", DiscoveryItem.id == source["item_id"],
            DiscoveryItem.digest == source["revision_digest"],
            DiscoveryCandidate.profession_id == source["profession_id"], DiscoveryCandidate.status == "accepted",
            DiscoveryCandidate.reviewed_digest == DiscoveryItem.digest, DiscoveryCandidate.valid_until > now(),
            SourceExcerpt.id == source["excerpt_id"], SourceExcerpt.source_id == source["source_id"],
        ).with_for_update())
        if current is None:
            return False
    return True
