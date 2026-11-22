"""Editorial workflow for synthesized knowledge cards."""

import re
from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel, Field, StrictBool, StringConstraints, model_validator
from sqlalchemy import func, select

from backend.catalog_topics import catalog_topic_columns, profession_for_card
from backend.db import CardEvent, KnowledgeCard, Message, now
from backend.embeddings import (
    SEARCH_POOL, SIMILAR_MIN_SCORE, configured as embeddings_configured, refresh_index, search_cards, similar_cards,
)
from backend.http_errors import MESSAGES
from backend.interview_api import Actor, CommandKey, PageSize, utc, versioned_command
from backend.interview_engine import GAPS_PREFIX, LABELS
from backend.interview_schemas import FactField, Identifier, ShortText, VersionedRequest
from backend.knowledge import SECTIONS, cases_of, fact_skills, published_cards
from backend.pagination import newest_page
from backend.taxonomy import TAXONOMY
from backend.themes import THEME_MIN_SCORE, case_categories, catalog_themes, named_skills

router = APIRouter(prefix="/api/v1")
MIN_REVIEW_ITEMS = 2
REVIEW_HEADINGS = {label.casefold() for label in LABELS.values()}
CardText = Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
Comment = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
AuthorName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
SearchLimit = Annotated[int, Field(ge=1, le=20)]
ThemeThreshold = Annotated[float, Field(ge=0.5, le=0.95)]
SIMILAR_LIMIT = 3


class FactEdit(BaseModel):
    id: Identifier | None = None
    text: Annotated[str, StringConstraints(min_length=1, max_length=8000)]


class CaseEdit(BaseModel):
    case_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    title: ShortText
    task: list[FactEdit] = Field(default_factory=list, max_length=50)
    context: list[FactEdit] = Field(default_factory=list, max_length=50)
    actions: list[FactEdit] = Field(default_factory=list, max_length=50)
    decisions: list[FactEdit] = Field(default_factory=list, max_length=50)
    verification: list[FactEdit] = Field(default_factory=list, max_length=50)
    errors: list[FactEdit] = Field(default_factory=list, max_length=50)
    limitations: list[FactEdit] = Field(default_factory=list, max_length=50)
    unknown_fields: list[FactField] = Field(default_factory=list, max_length=7)


class ContentEdit(CaseEdit):
    cases: list[CaseEdit] | None = Field(default=None, max_length=20)


class EditCard(VersionedRequest):
    title: ShortText | None = None
    content: ContentEdit | None = None
    text: CardText

    @model_validator(mode="after")
    def check_edit(self):
        if not self.text.strip() or (self.title is not None and self.content is not None):
            raise ValueError("Invalid edit")
        return self


class SubmitCard(VersionedRequest):
    consent: StrictBool
    show_author: StrictBool
    author_name: AuthorName | None = None
    # Preserve request compatibility and idempotency for older clients.
    share_evidence: StrictBool = Field(default=False, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def check_consent(self):
        if not self.consent or (self.author_name is not None and not self.show_author):
            raise ValueError("Explicit consent and consistent authorship are required")
        return self


class ReviewCard(VersionedRequest):
    decision: Literal["accept", "return", "keep_private"]
    comment: Comment = ""

    @model_validator(mode="after")
    def check_comment(self):
        if self.decision == "return" and not self.comment:
            raise ValueError("A reason is required")
        return self


class UnpublishCard(VersionedRequest):
    comment: Annotated[Comment, Field(min_length=1)]


class CardView(BaseModel):
    id: str
    session_id: str
    owner_id: str
    origin: str
    version: int
    content_version: int
    publication_status: str
    review_status: str
    content: dict
    text: str
    show_author: bool
    author_name: str | None
    consent: dict | None
    review: dict | None
    review_blocker: str | None = None
    created_at: datetime
    updated_at: datetime


class CardSnapshot(BaseModel):
    content: dict
    text: str


class CardEventView(BaseModel):
    id: str
    actor_id: str
    action: str
    version: int
    content_version: int
    created_at: datetime
    details: dict


class CardDetail(CardView):
    original: CardSnapshot
    history: list[CardEventView]


class PublicSummary(BaseModel):
    id: str
    title: str
    profession_id: str
    topic_title: str
    reviewed_at: datetime
    author_name: str | None = None


class PublicCard(PublicSummary):
    content: dict
    text: str


class SearchMatch(BaseModel):
    title: str
    task: str
    skills: list[str]


class SearchHit(PublicSummary):
    score: float
    match: SearchMatch | None = None


class SearchPage(BaseModel):
    items: list[SearchHit]
    mode: Literal["meaning", "words"]


class ThemeSkill(BaseModel):
    skill: str
    cases: int


class ThemeCell(BaseModel):
    phi: float
    cases: int


class Theme(BaseModel):
    title: str
    card_id: str
    cases: int
    cards: list[PublicSummary]
    skills: list[ThemeSkill]
    matrix: list[ThemeCell]


class ThemesPage(BaseModel):
    threshold: float
    cases: int
    unthemed: int
    categories: list[str]
    themes: list[Theme]


Item = TypeVar("Item")


class CardPage(BaseModel, Generic[Item]):
    items: list[Item]
    next_cursor: str | None


def editor_only(actor):
    if not actor["is_editor"]:
        raise HTTPException(403, "EDITOR_REQUIRED")


def accessible_card(db, card_id, actor, access="read"):
    if access == "editorship":
        editor_only(actor)
    card = db.get(KnowledgeCard, card_id)
    if card is not None:
        is_owner = card.owner_id == actor["id"]
        editorial_access = actor["is_editor"] and card.first_submitted_at is not None
        if (is_owner and access != "editorship") or (editorial_access and access != "ownership"):
            return card
    raise HTTPException(404, "CARD_NOT_FOUND")


def normalized_detail(text):
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def has_review_content(content, text):
    if "cases" in content or any(section in content for section in SECTIONS):
        for case in [content, *(content.get("cases") or [])]:
            sections = [{normalized_detail(fact["text"]) for fact in case.get(section, [])} - {""}
                        for section in SECTIONS]
            if (sum(bool(details) for details in sections) >= MIN_REVIEW_ITEMS
                    and len(set().union(*sections)) >= MIN_REVIEW_ITEMS):
                return True
        return False

    # Text-only revisions may retain headings and gap notices from the generated card.
    ignored = REVIEW_HEADINGS | {normalized_detail(content["title"]), ""}
    lines = [line.strip() for line in text.splitlines()]
    details = set()
    for index, line in enumerate(lines):
        detail = normalized_detail(line)
        if detail in ignored or line.casefold().startswith(GAPS_PREFIX.casefold()):
            continue
        next_section = lines[index + 1:index + 3]
        if (not line.startswith("• ") and len(next_section) == 2 and not next_section[0]
                and (next_section[1].casefold() in REVIEW_HEADINGS or next_section[1].startswith(GAPS_PREFIX))):
            continue
        details.add(detail)
    return len(details) >= MIN_REVIEW_ITEMS


def card_view(card):
    return CardView.model_validate({
        **{field: getattr(card, field) for field in CardView.model_fields if field != "review_blocker"},
        "review_blocker": MESSAGES["CARD_INCOMPLETE"] if card.publication_status == "draft"
        and not has_review_content(card.content, card.text) else None,
        "created_at": utc(card.created_at), "updated_at": utc(card.updated_at),
    })


@router.get("/cards", response_model=CardPage[CardView])
def list_cards(request: Request, response: Response, actor: Actor, limit: PageSize = 20, cursor: Identifier | None = None,
               scope: Literal["own", "review", "published"] = "own"):
    response.headers["Cache-Control"] = "no-store"
    query = select(KnowledgeCard)
    if scope == "own":
        query = query.where(KnowledgeCard.owner_id == actor["id"])
    else:
        editor_only(actor)
        status = "in_review" if scope == "review" else "published"
        query = query.where(KnowledgeCard.first_submitted_at.is_not(None), KnowledgeCard.publication_status == status)
    with request.app.state.sessions() as db:
        rows, next_cursor = newest_page(db, query, KnowledgeCard, limit, cursor)
        return {"items": [card_view(card) for (card,) in rows], "next_cursor": next_cursor}


@router.get("/cards/{card_id}", response_model=CardDetail)
def read_card(request: Request, response: Response, actor: Actor, card_id: Identifier):
    response.headers["Cache-Control"] = "no-store"
    with request.app.state.sessions() as db:
        card = accessible_card(db, card_id, actor)
        events = list(db.scalars(select(CardEvent).where(CardEvent.card_id == card.id).order_by(CardEvent.version)))
        first_edit = next((event for event in events if event.action == "edit"), None)
        original = first_edit.details["previous"] if first_edit else {"content": card.content, "text": card.text}
        return {
            **card_view(card).model_dump(), "original": original,
            "history": [{
                "id": event.id, "actor_id": event.actor_id, "action": event.action,
                "version": event.version, "content_version": event.content_version,
                "created_at": utc(event.created_at), "details": event.details,
            } for event in events],
        }


def find_fact(content, fact_id):
    return next(((fact, section) for case in cases_of(content) for section in SECTIONS
                 for fact in case.get(section, ()) if fact.get("id") == fact_id), (None, None))


def evidence_source(question):
    for source in question.market_sources or ():
        if not source.get("used"):
            continue
        title, url, quote = (source.get(key) for key in ("title", "url", "task_excerpt"))
        if not all(isinstance(value, str) and value for value in (title, url, quote)):
            continue
        try:
            address = urlsplit(url)
            if (address.scheme in {"https", "http"} and address.hostname and address.username is None
                    and not any(char.isspace() or char == "\\" for char in url)):
                return {"title": title, "url": url, "quote": quote}
        except ValueError:
            continue
    return None


@router.get("/cards/{card_id}/facts/{fact_id}/evidence")
def read_fact_evidence(request: Request, response: Response, actor: Actor, card_id: Identifier, fact_id: Identifier):
    response.headers["Cache-Control"] = "no-store"
    with request.app.state.sessions() as db:
        card = accessible_card(db, card_id, actor)
        owner = card.owner_id == actor["id"]
        if not owner and not (card.publication_status in {"in_review", "published"}
                              and matches_version(card.consent, card) and card.consent.get("share_evidence") is True):
            raise HTTPException(404, "CARD_NOT_FOUND")
        fact, section = find_fact(card.content, fact_id)
        history = []
        if (owner and fact is None) or (fact and fact.get("derived_from_fact_id")):
            snapshots = db.scalars(select(CardEvent.details).where(
                CardEvent.card_id == card.id, CardEvent.action == "edit",
            ).order_by(CardEvent.version.desc()))
            history = [snapshot["previous"]["content"] for snapshot in snapshots]
        if owner and fact is None:
            for snapshot in history:
                fact, section = find_fact(snapshot, fact_id)
                if fact is not None:
                    break
        if fact is None:
            raise HTTPException(404, "CARD_NOT_FOUND")
        original = fact if fact.get("source", "interview") == "interview" else None
        original_id = fact.get("derived_from_fact_id")
        if original is None and original_id:
            for snapshot in history:
                prior, prior_section = find_fact(snapshot, original_id)
                if prior and prior_section == section and prior.get("source", "interview") == "interview":
                    original = prior
                    break

        turns = []
        if original:
            ids = original.get("supporting_message_ids", ())
            answers = db.scalars(select(Message).where(
                Message.session_id == card.session_id, Message.id.in_(ids),
                Message.kind == "answer", Message.status == "processed",
            ).order_by(Message.sequence))
            for answer in answers:
                quote = original["text"]
                if " ".join(quote.split()) not in " ".join((answer.text or "").split()):
                    continue
                if answer.extraction is not None and not any(
                    item.get("id") == original.get("id") and item.get("text") == quote
                    for item in answer.extraction.get("facts", ())
                ):
                    continue
                question = db.get(Message, answer.reply_to_question_id) if answer.reply_to_question_id else None
                if (question is None or question.session_id != card.session_id or question.kind != "question"
                        or question.sequence >= answer.sequence or not question.text):
                    continue
                turns.append({"question_id": question.id if owner else None,
                              "answer_id": answer.id if owner else None, "question": question.text,
                              "answer_quote": quote, "source": evidence_source(question)})
        return {"original_text": original["text"] if original and original is not fact else None,
                "skills": fact_skills(original, section) if turns and original is fact else [], "turns": turns}


def write_card(request, actor, key, card_id, body, access, apply):
    def load(db):
        return accessible_card(db, card_id, actor, access)

    def execute(db, card):
        stamp = now()
        event_id = str(uuid4())
        action, details = apply(card, stamp, event_id)
        if action == "review" and card.publication_status == "published":
            card.catalog_profession_id = profession_for_card(db, card.id)
        card.updated_at = stamp
        db.add(CardEvent(id=event_id, card_id=card.id, actor_id=actor["id"], action=action,
                         version=card.version, content_version=card.content_version, details=details, created_at=stamp))
        return card_view(card)

    return versioned_command(request, actor, key, body, load, execute)


def reconcile_case(edited, previous, event_id):
    content = {"title": edited.title, "unknown_fields": edited.unknown_fields}
    for name in SECTIONS:
        previous_facts = {fact["id"]: fact for fact in previous.get(name, []) if fact.get("id")}
        content[name] = []
        for fact in getattr(edited, name):
            prior = previous_facts.pop(fact.id, None)
            if prior and prior["text"] == fact.text:
                content[name].append({**prior, "source": prior.get("source", "interview")})
            else:
                content[name].append({
                    "id": str(uuid4()), "text": fact.text, "source": "owner_edit",
                    "supporting_message_ids": [], "edit_id": event_id,
                    **({"derived_from_fact_id": prior["id"] if prior.get("source", "interview") == "interview"
                        else prior["derived_from_fact_id"]}
                       if prior and (prior.get("source", "interview") == "interview"
                                     or prior.get("derived_from_fact_id")) else {}),
                })
    return content


def reconcile_content(edited, previous, event_id):
    content = reconcile_case(edited, previous, event_id)
    if edited.cases is not None:
        old_cases = previous.get("cases", [])
        by_id = {case["case_id"]: case for case in old_cases if case.get("case_id")}
        legacy = {index: case for index, case in enumerate(old_cases) if not case.get("case_id")}
        legacy_facts = {(section, fact["id"]): index for index, case in legacy.items()
                        for section in SECTIONS for fact in case.get(section, ()) if fact.get("id")}
        content["cases"] = []
        for case in edited.cases:
            prior = by_id.pop(case.case_id, None)
            if prior is None and case.case_id is None:
                matches = {legacy_facts[key] for section in SECTIONS for fact in getattr(case, section)
                           if (key := (section, fact.id)) in legacy_facts}
                if len(matches) == 1:
                    prior = legacy.pop(matches.pop(), None)
            content["cases"].append({**reconcile_case(case, prior or {}, event_id),
                                     "case_id": (prior or {}).get("case_id") or str(uuid4())})
    return content


@router.patch("/cards/{card_id}", response_model=CardView)
def edit_card(request: Request, actor: Actor, key: CommandKey, card_id: Identifier, body: EditCard):
    def apply(card, stamp, event_id):
        if card.publication_status == "published":
            raise HTTPException(409, "INVALID_STATE")
        previous = {"content": card.content, "text": card.text}
        if body.content is not None:
            content = reconcile_content(body.content, card.content, event_id)
        else:
            title = body.title or card.content["title"]
            # Retain removed source facts in private history only.
            content = {"title": title} if body.text != card.text else {**card.content, "title": title}
        if content == card.content and body.text == card.text:
            raise HTTPException(409, "CARD_UNCHANGED")
        card.content = content
        card.text = body.text
        card.content_version += 1
        card.publication_status, card.review_status = "draft", "unreviewed"
        card.consent = card.review = None
        return "edit", {"previous": previous, "text_source": "owner_edit" if body.text != previous["text"] else None}

    return write_card(request, actor, key, card_id, body, "ownership", apply)


def decision_metadata(card, actor, stamp, **details):
    return {"content_version": card.content_version, "actor_id": actor["id"], "at": utc(stamp).isoformat(), **details}


def matches_version(record, card):
    return record is not None and record.get("content_version") == card.content_version


@router.post("/cards/{card_id}/submit-review", response_model=CardView)
def submit_card(request: Request, actor: Actor, key: CommandKey, card_id: Identifier, body: SubmitCard):
    def apply(card, stamp, event_id):
        if card.publication_status != "draft":
            raise HTTPException(409, "INVALID_STATE")
        if not has_review_content(card.content, card.text):
            raise HTTPException(422, "CARD_INCOMPLETE")
        author_name = (body.author_name or "Участник Modeus") if body.show_author else None
        if (card.show_author, card.author_name) != (body.show_author, author_name):
            card.content_version += 1
        card.show_author, card.author_name = body.show_author, author_name
        card.first_submitted_at = card.first_submitted_at or stamp
        card.publication_status, card.review_status = "in_review", "unreviewed"
        card.consent = decision_metadata(card, actor, stamp, show_author=card.show_author,
                                         author_name=card.author_name, share_evidence=True)
        card.review = None
        return "submit_review", card.consent

    return write_card(request, actor, key, card_id, body, "ownership", apply)


@router.post("/cards/{card_id}/review", response_model=CardView)
def review_card(request: Request, tasks: BackgroundTasks, actor: Actor, key: CommandKey, card_id: Identifier,
                body: ReviewCard):
    settings = request.app.state.settings
    if body.decision == "accept" and embeddings_configured(settings):
        # Runs after the response; the periodic index round picks the card up if this fails.
        tasks.add_task(refresh_index, request.app.state.sessions, settings, [card_id])

    def apply(card, stamp, event_id):
        if card.publication_status != "in_review" or not matches_version(card.consent, card):
            raise HTTPException(409, "INVALID_STATE")
        card.review = decision_metadata(card, actor, stamp, decision=body.decision, comment=body.comment)
        if body.decision == "accept":
            card.publication_status, card.review_status = "published", "reviewed"
        else:
            card.publication_status = "draft"
            card.review_status = "needs_changes" if body.decision == "return" else "unreviewed"
            card.consent = None
        return "review", card.review

    return write_card(request, actor, key, card_id, body, "editorship", apply)


@router.post("/cards/{card_id}/unpublish", response_model=CardView)
def unpublish_card(request: Request, actor: Actor, key: CommandKey, card_id: Identifier, body: UnpublishCard):
    def apply(card, stamp, event_id):
        if card.publication_status != "published":
            raise HTTPException(409, "INVALID_STATE")
        card.publication_status, card.review_status = "draft", "needs_changes"
        card.consent = None
        card.review = decision_metadata(card, actor, stamp, decision="unpublish", comment=body.comment)
        return "unpublish", card.review

    return write_card(request, actor, key, card_id, body, "editorship", apply)


def public_summary(card, topic_title, topic_profession):
    title = card.content["title"]
    return PublicSummary(
        id=card.id, title=title, profession_id=card.catalog_profession_id or topic_profession or "unknown",
        topic_title=topic_title or title,
        reviewed_at=card.review["at"], author_name=card.author_name if card.show_author else None,
    )


def public_content(content):
    safe = {"title": content["title"]}
    if "unknown_fields" in content:
        safe["unknown_fields"] = content["unknown_fields"]
    for name in SECTIONS:
        if name in content:
            safe[name] = [{"text": fact["text"]} for fact in content[name]]
    if "cases" in content:
        safe["cases"] = [public_content(case) for case in content["cases"]]
    return safe


@router.get("/catalog", response_model=CardPage[PublicSummary])
def catalog(request: Request, response: Response, limit: PageSize = 20, cursor: Identifier | None = None,
            topic_id: Identifier | None = None, profession_id: Identifier | None = None,
            topic_title: ShortText | None = None):
    response.headers["Cache-Control"] = "no-store"
    with request.app.state.sessions() as db:
        query = catalog_topic_columns(published_cards())
        if topic_id:
            query = query.where(query.selected_columns.topic_id == topic_id)
        if topic_title:
            query = query.where(func.coalesce(
                query.selected_columns.topic_title, KnowledgeCard.content["title"].as_string(),
            ) == topic_title)
        if profession_id:
            query = query.where(func.coalesce(
                KnowledgeCard.catalog_profession_id, query.selected_columns.topic_profession, "unknown",
            ) == profession_id)
        rows, next_cursor = newest_page(db, query, KnowledgeCard, limit, cursor)
        return {"items": [public_summary(card, title, profession) for card, _, title, profession in rows],
                "next_cursor": next_cursor}


@router.get("/catalog/topics")
def catalog_topics(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    query = catalog_topic_columns(published_cards())
    title = func.coalesce(query.selected_columns.topic_title, KnowledgeCard.content["title"].as_string())
    with request.app.state.sessions() as db:
        return {"items": list(db.scalars(query.with_only_columns(title).distinct().order_by(title)))}


def case_match(content, index):
    """The case a search hit matched (or the card's first case with facts), as shown under the hit."""
    cases = list(cases_of(content))
    case = cases[index] if index is not None and index < len(cases) else next(
        (case for case in cases if case.get("task")), cases[0])
    skills, _ = named_skills(case)
    task = next((fact["text"] for fact in case.get("task") or ()), "")
    return SearchMatch(title=case.get("title") or content["title"], task=task[:240], skills=sorted(skills)[:5])


def summaries(db, hits):
    """Public summaries of (card id, score[, matched case index]) hits, in the order given."""
    rows = {card.id: (public_summary(card, title, profession), card.content)
            for card, _, title, profession in db.execute(catalog_topic_columns(published_cards()).where(
                KnowledgeCard.id.in_([hit[0] for hit in hits])))}
    return [SearchHit(**rows[hit[0]][0].model_dump(), score=round(hit[1], 3),
                      match=case_match(rows[hit[0]][1], hit[2]) if len(hit) > 2 else None)
            for hit in hits if hit[0] in rows]


@router.get("/catalog/search", response_model=SearchPage)
def search_catalog(request: Request, response: Response, q: ShortText, limit: SearchLimit = 10,
                   category: Identifier | None = None):
    response.headers["Cache-Control"] = "no-store"
    if category is not None and category not in TAXONOMY:
        raise HTTPException(422, "UNKNOWN_CATEGORY")
    with request.app.state.sessions() as db:
        hits, mode = search_cards(db, request.app.state.settings, q,
                                  published_cards().with_only_columns(KnowledgeCard.id), SEARCH_POOL)
        if category:
            contents = dict(db.execute(select(KnowledgeCard.id, KnowledgeCard.content).where(
                KnowledgeCard.id.in_([hit[0] for hit in hits]))).all())
            hits = [hit for hit in hits if any(category in found for _, found in case_categories(contents[hit[0]]).values())]
        return {"items": summaries(db, hits[:limit]), "mode": mode}


@router.get("/catalog/themes", response_model=ThemesPage)
def catalog_theme_matrix(request: Request, response: Response, threshold: ThemeThreshold = THEME_MIN_SCORE):
    response.headers["Cache-Control"] = "no-store"
    with request.app.state.sessions() as db:
        result = catalog_themes(db, request.app.state.settings.embedding_model, threshold)
        cards = {hit.id: hit for hit in summaries(db, [(card_id, 1.0) for theme in result["themes"]
                                                       for card_id in theme["card_ids"]])}
        for theme in result["themes"]:
            theme["cards"] = [cards[card_id] for card_id in theme.pop("card_ids") if card_id in cards]
        return result


@router.get("/catalog/{card_id}/similar", response_model=SearchPage)
def similar_catalog_cards(request: Request, response: Response, card_id: Identifier):
    response.headers["Cache-Control"] = "no-store"
    settings = request.app.state.settings
    with request.app.state.sessions() as db:
        if db.scalar(published_cards().with_only_columns(KnowledgeCard.id).where(KnowledgeCard.id == card_id)) is None:
            raise HTTPException(404, "CARD_NOT_FOUND", headers={"Cache-Control": "no-store"})
        hits = similar_cards(db, settings.embedding_model, card_id,
                             published_cards().with_only_columns(KnowledgeCard.id), SIMILAR_LIMIT)
        return {"items": summaries(db, [hit for hit in hits if hit[1] >= SIMILAR_MIN_SCORE]), "mode": "meaning"}


@router.get("/catalog/{card_id}", response_model=PublicCard)
def catalog_card(request: Request, response: Response, card_id: Identifier):
    response.headers["Cache-Control"] = "no-store"
    with request.app.state.sessions() as db:
        row = db.execute(catalog_topic_columns(published_cards()).where(KnowledgeCard.id == card_id)).first()
        if row is None:
            raise HTTPException(404, "CARD_NOT_FOUND", headers={"Cache-Control": "no-store"})
        card, _, title, profession = row
        return PublicCard(**public_summary(card, title, profession).model_dump(),
                          content=public_content(card.content), text=card.text)
