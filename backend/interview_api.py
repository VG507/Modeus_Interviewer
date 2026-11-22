"""Authenticated HTTP entry points for text interviews."""

import hashlib
import json
from datetime import timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlalchemy.orm.exc import StaleDataError

from backend.auth import current_user
from backend.db import CommandReceipt, InterviewSession, KnowledgeCard, Message, Topic, User
from backend.injection import injection_attempt
from backend.interview_engine import choose_intent
from backend.interview_schemas import (
    AnswerResult, CaseState, CreateAnswer, CreateInterview, InterviewDetail,
    Identifier, InterviewContext, InterviewView, MessageView, OPEN_TOPIC_ID, SourceQuote, TopicSummary, UpdateAnswer,
    VersionedRequest,
)
from backend.interview_worker import (
    InvalidJob, advance_case, append_message, complete, interview_question, job_answer, job_payload, queue,
    stop_after_attack, valid_job,
)
from backend.pagination import newest_page

router = APIRouter(prefix="/api/v1")
Actor = Annotated[dict, Depends(current_user)]
CommandKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def utc(value):
    return value.replace(tzinfo=timezone.utc)


def message_view(message):
    source = next((item for item in message.market_sources or [] if item.get("used")), None)
    quote = SourceQuote(title=source["title"], url=source["url"], quote=source["task_excerpt"]) if source else None
    return MessageView.model_validate(message, from_attributes=True).model_copy(update={
        "created_at": utc(message.created_at), "updated_at": utc(message.updated_at),
        "assessment": (message.extraction or {}).get("assessment"),
        "vacancy_quote": quote if source and "vacancy_id" in source else None,
        "source_quote": quote,
    })


def interview_view(db, interview):
    card_id = db.scalar(select(KnowledgeCard.id).where(KnowledgeCard.session_id == interview.id))
    state = interview.case_state if isinstance(interview.case_state, dict) else {}
    active_id = state.get("active_topic_id")
    topic = db.get(Topic, active_id) if active_id else None
    if topic is None:
        topic = db.get(Topic, interview.topic_id)
    return interview_summary(interview, card_id, topic.title if topic else None)


def interview_summary(interview, card_id, topic_title):
    state = interview.case_state if isinstance(interview.case_state, dict) else {}
    return InterviewView.model_validate(interview, from_attributes=True).model_copy(update={
        "current_topic_title": state.get("discovered_topic") or topic_title,
        "card_id": card_id, "created_at": utc(interview.created_at), "updated_at": utc(interview.updated_at),
    })


def owned(db, interview_id, owner):
    interview = db.get(InterviewSession, interview_id)
    if interview is None or interview.owner_id != owner:
        raise HTTPException(404, "INTERVIEW_NOT_FOUND")
    return interview


def conflict(version):
    raise HTTPException(409, {
        "code": "VERSION_CONFLICT", "message": "Состояние изменилось. Обновите данные.",
        "current_version": version,
    })


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def write_command(request, actor, key, body, execute, interview_id=None, *, serialize_creation=False):
    def load(db):
        return owned(db, interview_id, actor["id"]) if interview_id else None

    return versioned_command(request, actor, key, body, load, execute, serialize_creation=serialize_creation)


def versioned_command(request, actor, key, body, load, execute, *, serialize_creation=False):
    if request.headers.get("origin") not in request.app.state.app_origins:
        raise HTTPException(403, "ORIGIN_REJECTED")
    if not key.strip():
        raise HTTPException(422, "VALIDATION_ERROR")
    owner = actor["id"]
    sessions = request.app.state.sessions
    receipt_id = digest([owner, request.method, request.url.path, key])
    body_digest = digest(body.model_dump(mode="json"))

    def lookup(db):
        receipt = db.get(CommandReceipt, receipt_id)
        if receipt and receipt.body_digest != body_digest:
            raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
        return receipt

    try:
        with sessions.begin() as db:
            if serialize_creation:
                # Lock the owner before checking both the receipt and the quota.
                db.execute(update(User).where(User.id == owner).values(id=User.id))
            resource = load(db)
            receipt = lookup(db)
            if receipt:
                return receipt.result
            if resource is not None:
                if body.expected_version != resource.version:
                    conflict(resource.version)
                resource.version += 1
                db.flush()
            result = execute(db, resource).model_dump(mode="json")
            db.add(CommandReceipt(id=receipt_id, owner_id=owner, body_digest=body_digest, result=result))
            return result
    except (StaleDataError, IntegrityError):
        with sessions() as db:
            resource = load(db)
            receipt = lookup(db)
            if receipt:
                return receipt.result
            if resource is not None and body.expected_version != resource.version:
                conflict(resource.version)
        raise


def require_active(interview):
    if interview.status != "active":
        raise HTTPException(409, "INVALID_STATE")


def require_question(db, interview):
    require_active(interview)
    if interview.processing_status in {"queued", "running"}:
        raise HTTPException(409, "PROCESSING_IN_PROGRESS")
    if interview.processing_status != "idle" or not interview.current_question_id:
        raise HTTPException(409, "INVALID_STATE")
    if not isinstance(interview.case_state, dict):
        raise HTTPException(409, "INVALID_STATE")
    intents = interview.case_state.get("asked_intents")
    if not isinstance(intents, list) or not intents or not isinstance(intents[-1], str) or not intents[-1]:
        raise HTTPException(409, "INVALID_STATE")
    if interview_question(db, interview, interview.current_question_id) is None:
        raise HTTPException(409, "INVALID_STATE")
    return intents[-1]


def find_draft(db, interview, answer_id):
    current_intent = require_question(db, interview)
    answer = db.get(Message, answer_id)
    if answer is None or answer.session_id != interview.id or answer.kind != "answer":
        raise HTTPException(404, "ANSWER_NOT_FOUND")
    if answer.status != "draft" or answer.reply_to_question_id != interview.current_question_id:
        raise HTTPException(409, "INVALID_STATE")
    return answer, current_intent


def answer_result(db, interview, answer):
    db.flush()
    return AnswerResult(interview=interview_view(db, interview), answer=message_view(answer))


@router.get("/topics")
def topics(request: Request, actor: Actor):
    with request.app.state.sessions() as db:
        return {"items": [
            TopicSummary.model_validate(topic, from_attributes=True)
            for topic in db.scalars(select(Topic).where(Topic.id != OPEN_TOPIC_ID).order_by(Topic.id))
        ]}


def interview_profile(db, owner):
    profile = db.scalar(select(User.interview_profile).where(User.id == owner))
    if profile is not None:
        return profile
    # Users who already interviewed can reuse the most recent questionnaire.
    return db.scalar(select(InterviewSession.context).where(InterviewSession.owner_id == owner)
                     .order_by(InterviewSession.created_at.desc(), InterviewSession.id.desc()).limit(1))


def checked_context(body):
    context = body.model_dump()
    fields = [value for key, value in context.items() if key != "projects"]
    fields.extend(value for project in context.get("projects", []) for value in project.values())
    if any(injection_attempt(value) for value in fields):
        raise HTTPException(422, "INJECTION_DETECTED")
    return context


@router.get("/interview-profile", response_model=InterviewContext | None)
def get_interview_profile(request: Request, actor: Actor):
    with request.app.state.sessions() as db:
        return interview_profile(db, actor["id"])


@router.put("/interview-profile", response_model=InterviewContext)
def save_interview_profile(request: Request, actor: Actor, body: InterviewContext):
    if request.headers.get("origin") not in request.app.state.app_origins:
        raise HTTPException(403, "ORIGIN_REJECTED")
    context = checked_context(body)
    with request.app.state.sessions.begin() as db:
        db.execute(update(User).where(User.id == actor["id"]).values(interview_profile=context))
    return context


@router.post("/interviews", status_code=201, response_model=InterviewView)
def create_interview(request: Request, actor: Actor, key: CommandKey, body: CreateInterview):
    def create(db, _):
        topic_id = body.topic_id or OPEN_TOPIC_ID
        if db.get(Topic, topic_id) is None:
            raise HTTPException(404, "TOPIC_NOT_FOUND")
        active = db.scalar(select(func.count()).select_from(InterviewSession).where(
            InterviewSession.owner_id == actor["id"], InterviewSession.status.in_(("active", "completing")),
        ))
        if active >= 5:
            raise HTTPException(409, "ACTIVE_INTERVIEW_LIMIT")
        profile = body.context or interview_profile(db, actor["id"])
        if profile is None:
            raise HTTPException(422, "INTERVIEW_PROFILE_REQUIRED")
        context = checked_context(InterviewContext.model_validate(profile))
        if body.context is None:
            db.execute(update(User).where(User.id == actor["id"], User.interview_profile.is_(None))
                       .values(interview_profile=context))
        interview = InterviewSession(owner_id=actor["id"], topic_id=topic_id, context=context,
                                     case_state=CaseState(open_interview=topic_id == OPEN_TOPIC_ID).model_dump())
        queue(interview, "initial_question")
        db.add(interview)
        db.flush()
        return interview_view(db, interview)

    return write_command(request, actor, key, body, create, serialize_creation=True)


@router.get("/interviews")
def list_interviews(request: Request, actor: Actor, limit: PageSize = 20, cursor: Identifier | None = None):
    with request.app.state.sessions() as db:
        active = aliased(Topic)
        query = (select(InterviewSession, KnowledgeCard.id, func.coalesce(active.title, Topic.title))
                 .outerjoin(KnowledgeCard, KnowledgeCard.session_id == InterviewSession.id)
                 .outerjoin(Topic, Topic.id == InterviewSession.topic_id)
                 .outerjoin(active, active.id == InterviewSession.case_state["active_topic_id"].as_string())
                 .where(InterviewSession.owner_id == actor["id"]))
        rows, next_cursor = newest_page(db, query, InterviewSession, limit, cursor)
        return {"items": [interview_summary(*row) for row in rows], "next_cursor": next_cursor}


@router.get("/interviews/{interview_id}", response_model=InterviewDetail)
def read_interview(request: Request, actor: Actor, interview_id: str):
    with request.app.state.sessions() as db:
        # Read the current question ID and its message in the same snapshot.
        # A worker can commit a new question while Telegram is polling.
        rows = db.execute(
            select(InterviewSession, Message)
            .outerjoin(Message, Message.session_id == InterviewSession.id)
            .where(InterviewSession.id == interview_id, InterviewSession.owner_id == actor["id"])
            .order_by(Message.sequence)
        ).all()
        if not rows:
            raise HTTPException(404, "INTERVIEW_NOT_FOUND")
        return InterviewDetail(
            **interview_view(db, rows[0][0]).model_dump(),
            messages=[message_view(message) for _, message in rows if message is not None],
        )


@router.post("/interviews/{interview_id}/answers", status_code=201, response_model=AnswerResult)
def create_answer(request: Request, actor: Actor, key: CommandKey, interview_id: str, body: CreateAnswer):
    def create(db, interview):
        require_question(db, interview)
        if interview.current_question_id != body.question_id:
            raise HTTPException(409, "INVALID_STATE")
        draft = db.scalar(select(Message.id).where(Message.session_id == interview.id,
                          Message.reply_to_question_id == body.question_id, Message.status == "draft"))
        if draft:
            raise HTTPException(409, "INVALID_STATE")
        answer = append_message(db, interview, kind="answer", status="draft", text=body.text,
                                reply_to_question_id=body.question_id)
        return answer_result(db, interview, answer)

    return write_command(request, actor, key, body, create, interview_id)


@router.patch("/interviews/{interview_id}/answers/{answer_id}", response_model=AnswerResult)
def update_answer(request: Request, actor: Actor, key: CommandKey, interview_id: str,
                  answer_id: str, body: UpdateAnswer):
    def change(db, interview):
        answer, _ = find_draft(db, interview, answer_id)
        answer.text = body.text
        return answer_result(db, interview, answer)

    return write_command(request, actor, key, body, change, interview_id)


@router.post("/interviews/{interview_id}/answers/{answer_id}/submit",
             status_code=202, response_model=AnswerResult)
def submit_answer(request: Request, actor: Actor, key: CommandKey, interview_id: str,
                  answer_id: str, body: VersionedRequest):
    def submit(db, interview):
        answer, current_intent = find_draft(db, interview, answer_id)
        attack = injection_attempt(answer.text)
        if attack:
            stop_after_attack(db, interview, answer, attack)
            return answer_result(db, interview, answer)
        answer.status = "queued"
        queue(interview, "extraction", answer_id=answer.id,
              current_intent=current_intent)
        return answer_result(db, interview, answer)

    return write_command(request, actor, key, body, submit, interview_id)


@router.post("/interviews/{interview_id}/skip", status_code=202, response_model=InterviewView)
def skip_interview(request: Request, actor: Actor, key: CommandKey, interview_id: str, body: VersionedRequest):
    def skip(db, interview):
        current = require_question(db, interview)
        try:
            state = CaseState.model_validate(interview.case_state)
        except ValidationError:
            raise HTTPException(409, "INVALID_STATE") from None
        if current not in state.declined_intents:
            state.declined_intents.append(current)
        interview.case_state = state.model_dump()
        append_message(db, interview, kind="control", text="Пропуск вопроса")
        intent = choose_intent(interview.case_state)
        if intent:
            queue(interview, "question", intent=intent)
        else:
            try:
                advance_case(db, interview)
            except InvalidJob:
                raise HTTPException(409, "INVALID_STATE") from None
        db.flush()
        return interview_view(db, interview)

    return write_command(request, actor, key, body, skip, interview_id)


@router.post("/interviews/{interview_id}/finish", status_code=202, response_model=InterviewView)
def finish_interview(request: Request, actor: Actor, key: CommandKey, interview_id: str, body: VersionedRequest):
    def finish(db, interview):
        require_active(interview)
        append_message(db, interview, kind="control", text="Завершение интервью")
        complete(db, interview)
        db.flush()
        return interview_view(db, interview)

    return write_command(request, actor, key, body, finish, interview_id)


@router.post("/interviews/{interview_id}/retry", status_code=202, response_model=InterviewView)
def retry_interview(request: Request, actor: Actor, key: CommandKey, interview_id: str, body: VersionedRequest):
    def retry(db, interview):
        if interview.processing_status != "failed":
            raise HTTPException(409, "INVALID_STATE")
        job = {field: value for field, value in job_payload(interview).items() if field != "token"}
        if not valid_job(db, interview, job):
            raise HTTPException(409, "INVALID_STATE")
        if interview.processing_stage == "extraction":
            job_answer(db, interview, job).status = "queued"
        queue(interview, interview.processing_stage, **job)
        db.flush()
        return interview_view(db, interview)

    return write_command(request, actor, key, body, retry, interview_id)
