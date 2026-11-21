"""Editorial workflow for synthesized knowledge cards."""

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, StrictBool, StringConstraints, model_validator
from sqlalchemy import func, select

from backend.catalog_topics import catalog_topic_columns, profession_for_card
from backend.db import CardEvent, KnowledgeCard, now
from backend.interview_api import Actor, CommandKey, PageSize, utc, versioned_command
from backend.interview_schemas import FactField, Identifier, ShortText, VersionedRequest
from backend.pagination import newest_page

router = APIRouter(prefix="/api/v1")
SECTIONS = ("task", "context", "actions", "decisions", "verification", "errors", "limitations")
CardText = Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
Comment = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
AuthorName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class FactEdit(BaseModel):
    id: Identifier | None = None
    text: Annotated[str, StringConstraints(min_length=1, max_length=8000)]


class CaseEdit(BaseModel):
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


def card_view(card):
    return CardView.model_validate({
        **{field: getattr(card, field) for field in CardView.model_fields},
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
                })
    return content


def reconcile_content(edited, previous, event_id):
    content = reconcile_case(edited, previous, event_id)
    if edited.cases is not None:
        old_cases = previous.get("cases", [])
        content["cases"] = [
            reconcile_case(case, old_cases[index] if index < len(old_cases) else {}, event_id)
            for index, case in enumerate(edited.cases)
        ]
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
        author_name = (body.author_name or "Участник Modeus") if body.show_author else None
        if (card.show_author, card.author_name) != (body.show_author, author_name):
            card.content_version += 1
        card.show_author, card.author_name = body.show_author, author_name
        card.first_submitted_at = card.first_submitted_at or stamp
        card.publication_status, card.review_status = "in_review", "unreviewed"
        card.consent = decision_metadata(card, actor, stamp, show_author=card.show_author, author_name=card.author_name)
        card.review = None
        return "submit_review", card.consent

    return write_card(request, actor, key, card_id, body, "ownership", apply)


@router.post("/cards/{card_id}/review", response_model=CardView)
def review_card(request: Request, actor: Actor, key: CommandKey, card_id: Identifier, body: ReviewCard):
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


def published_cards():
    return select(KnowledgeCard).where(
        KnowledgeCard.publication_status == "published", KnowledgeCard.review_status == "reviewed",
        KnowledgeCard.consent["content_version"].as_integer() == KnowledgeCard.content_version,
        KnowledgeCard.review["content_version"].as_integer() == KnowledgeCard.content_version,
        KnowledgeCard.review["decision"].as_string() == "accept",
    )


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
