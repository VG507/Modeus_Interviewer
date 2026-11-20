"""Authenticated HTTP entry points for text interviews."""

import hashlib
import json
from datetime import timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from backend.auth import current_user
from backend.db import CommandReceipt, InterviewSession, KnowledgeCard, Message, Topic
from backend.interview_engine import choose_intent
from backend.interview_schemas import (
    AnswerResult, CaseState, CreateAnswer, CreateInterview, InterviewDetail,
    InterviewView, MessageView, OPEN_TOPIC_ID, TopicSummary, UpdateAnswer, VacancyQuote, VersionedRequest,
)
from backend.interview_worker import advance_case, append_message, complete, messages_for, queue

router = APIRouter(prefix="/api/v1")
Actor = Annotated[dict, Depends(current_user)]
CommandKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def utc(value):
    return value.replace(tzinfo=timezone.utc)


def message_view(message):
    source = next((item for item in message.market_sources or [] if item.get("used")), None)
    return MessageView.model_validate(message, from_attributes=True).model_copy(update={
        "created_at": utc(message.created_at), "updated_at": utc(message.updated_at),
        "assessment": (message.extraction or {}).get("assessment"),
        "vacancy_quote": VacancyQuote(title=source["title"], url=source["url"], quote=source["task_excerpt"]) if source else None,
    })


def interview_view(db, interview):
    card_id = db.scalar(select(KnowledgeCard.id).where(KnowledgeCard.session_id == interview.id))
    topic = db.get(Topic, interview.case_state.get("active_topic_id") or interview.topic_id)
    return InterviewView.model_validate(interview, from_attributes=True).model_copy(update={
        "current_topic_title": interview.case_state.get("discovered_topic") or (topic.title if topic else None),
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


def write_command(request, actor, key, body, execute, interview_id=None):
    def load(db):
        return owned(db, interview_id, actor["id"]) if interview_id else None

    return versioned_command(request, actor, key, body, load, execute)


def versioned_command(request, actor, key, body, load, execute):
    if request.headers.get("origin") != request.app.state.app_origin:
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


def require_question(interview):
    require_active(interview)
    if interview.processing_status in {"queued", "running"}:
        raise HTTPException(409, "PROCESSING_IN_PROGRESS")
    if interview.processing_status != "idle" or not interview.current_question_id:
        raise HTTPException(409, "INVALID_STATE")


def find_draft(db, interview, answer_id):
    require_question(interview)
    answer = db.get(Message, answer_id)
    if answer is None or answer.session_id != interview.id or answer.kind != "answer":
        raise HTTPException(404, "ANSWER_NOT_FOUND")
    if answer.status != "draft" or answer.reply_to_question_id != interview.current_question_id:
        raise HTTPException(409, "INVALID_STATE")
    return answer


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


@router.post("/interviews", status_code=201, response_model=InterviewView)
def create_interview(request: Request, actor: Actor, key: CommandKey, body: CreateInterview):
    def create(db, _):
        topic_id = body.topic_id or OPEN_TOPIC_ID
        if db.get(Topic, topic_id) is None:
            raise HTTPException(404, "TOPIC_NOT_FOUND")
        interview = InterviewSession(owner_id=actor["id"], topic_id=topic_id,
                                     context=body.context.model_dump(),
                                     case_state=CaseState(open_interview=topic_id == OPEN_TOPIC_ID).model_dump())
        queue(interview, "initial_question")
        db.add(interview)
        db.flush()
        return interview_view(db, interview)

    return write_command(request, actor, key, body, create)


@router.get("/interviews")
def list_interviews(request: Request, actor: Actor, limit: PageSize = 20, cursor: str | None = None):
    with request.app.state.sessions() as db:
        query = select(InterviewSession).where(InterviewSession.owner_id == actor["id"])
        if cursor:
            query = query.where(InterviewSession.id < cursor)
        rows = list(db.scalars(query.order_by(InterviewSession.id.desc()).limit(limit + 1)))
        return {"items": [interview_view(db, row) for row in rows[:limit]],
                "next_cursor": rows[limit - 1].id if len(rows) > limit else None}


@router.get("/interviews/{interview_id}", response_model=InterviewDetail)
def read_interview(request: Request, actor: Actor, interview_id: str):
    with request.app.state.sessions() as db:
        interview = owned(db, interview_id, actor["id"])
        return InterviewDetail(**interview_view(db, interview).model_dump(),
                               messages=[message_view(item) for item in messages_for(db, interview.id)])


@router.post("/interviews/{interview_id}/answers", status_code=201, response_model=AnswerResult)
def create_answer(request: Request, actor: Actor, key: CommandKey, interview_id: str, body: CreateAnswer):
    def create(db, interview):
        require_question(interview)
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
        answer = find_draft(db, interview, answer_id)
        answer.text = body.text
        return answer_result(db, interview, answer)

    return write_command(request, actor, key, body, change, interview_id)


@router.post("/interviews/{interview_id}/answers/{answer_id}/submit",
             status_code=202, response_model=AnswerResult)
def submit_answer(request: Request, actor: Actor, key: CommandKey, interview_id: str,
                  answer_id: str, body: VersionedRequest):
    def submit(db, interview):
        answer = find_draft(db, interview, answer_id)
        answer.status = "queued"
        queue(interview, "extraction", answer_id=answer.id,
              current_intent=interview.case_state["asked_intents"][-1])
        return answer_result(db, interview, answer)

    return write_command(request, actor, key, body, submit, interview_id)


@router.post("/interviews/{interview_id}/skip", status_code=202, response_model=InterviewView)
def skip_interview(request: Request, actor: Actor, key: CommandKey, interview_id: str, body: VersionedRequest):
    def skip(db, interview):
        require_question(interview)
        state = interview.case_state
        current = state["asked_intents"][-1]
        interview.case_state = {**state, "declined_intents": list(dict.fromkeys([*state["declined_intents"], current]))}
        append_message(db, interview, kind="control", text="Пропуск вопроса")
        intent = choose_intent(interview.case_state)
        if intent:
            queue(interview, "question", intent=intent)
        else:
            advance_case(db, interview)
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
        job = {field: value for field, value in interview.job.items() if field != "token"}
        if interview.processing_stage == "extraction":
            db.get(Message, job["answer_id"]).status = "queued"
        queue(interview, interview.processing_stage, **job)
        db.flush()
        return interview_view(db, interview)

    return write_command(request, actor, key, body, retry, interview_id)
