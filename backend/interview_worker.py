"""Persisted processing stages for the interview API."""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError

from backend.db import InterviewSession, KnowledgeCard, Message, Topic, now
from backend.interview_engine import (
    InvalidModelOutput,
    apply_extraction,
    choose_intent,
    extract,
    formulate,
    render_card,
    synthesize,
)
from backend.interview_schemas import CaseState, OPEN_TOPIC_ID

logger = logging.getLogger(__name__)


def queue(interview, stage, **payload):
    interview.processing_stage = stage
    interview.processing_status = "queued"
    interview.error = None
    interview.job = payload
    interview.lease_until = None


def clear_job(interview):
    interview.processing_stage = None
    interview.processing_status = "idle"
    interview.job = None
    interview.lease_until = None


def messages_for(db, interview_id):
    return list(db.scalars(
        select(Message).where(Message.session_id == interview_id).order_by(Message.sequence)
    ))


def recent_history(db, interview, current_answer):
    start = interview.case_state.get("case_start_sequence")
    if start is None and not interview.case_state.get("active_topic_id"):
        start = 1
    if start is None or current_answer is None:
        return []
    previous = db.scalars(select(Message).where(
        Message.session_id == interview.id, Message.kind == "answer", Message.status == "processed",
        Message.sequence >= start, Message.sequence < current_answer.sequence,
    ).order_by(Message.sequence.desc()).limit(6))
    turns = []
    for reply in previous:
        record = reply.extraction or {}
        if record.get("assessment") != "relevant" and not (
            record.get("assessment") is None and record.get("facts")
        ):
            continue
        question = db.get(Message, reply.reply_to_question_id)
        turns.append({"question": (question.text or "")[:400] if question else "",
                      "answer": (reply.text or "")[:1000]})
        if len(turns) == 3:
            break
    return list(reversed(turns))


def append_message(db, interview, *, kind, text, status="processed", reply_to_question_id=None):
    previous = messages_for(db, interview.id)
    message = Message(
        id=str(uuid4()), session_id=interview.id, sequence=previous[-1].sequence + 1 if previous else 1,
        kind=kind, text=text, status=status, reply_to_question_id=reply_to_question_id,
    )
    db.add(message)
    return message


def complete(db, interview):
    for message in messages_for(db, interview.id):
        if message.kind == "answer" and message.status == "queued":
            message.status = "failed"
    interview.status = "completing"
    interview.current_question_id = None
    queue(interview, "synthesis", completion_version=interview.version)


def advance_case(db, interview):
    state = dict(interview.case_state)
    original = db.get(Topic, interview.topic_id)
    current = db.get(Topic, state.pop("active_topic_id", None) or original.id)
    past = state.pop("past_cases", [])
    open_interview = original.id == OPEN_TOPIC_ID
    if open_interview:
        following = original
    else:
        topics = list(db.scalars(select(Topic).where(
            Topic.profession_id == original.profession_id, Topic.id != OPEN_TOPIC_ID,
        ).order_by(Topic.id)))
        position = next(index for index, item in enumerate(topics) if item.id == current.id)
        following = topics[(position + 1) % len(topics)]
    if state.get("facts"):
        past = [*past, {"title": state.get("discovered_topic") or current.title, "state": state}]
    interview.case_state = CaseState(active_topic_id=following.id, past_cases=past,
                                     open_interview=open_interview).model_dump()
    interview.current_question_id = None
    queue(interview, "initial_question", new_case=True, same_topic=following.id == current.id)


def claim(sessions, timeout):
    try:
        with sessions.begin() as db:
            interview = db.scalar(select(InterviewSession).where(or_(
                InterviewSession.processing_status == "queued",
                and_(InterviewSession.processing_status == "running", InterviewSession.lease_until <= now()),
            )).order_by(InterviewSession.updated_at, InterviewSession.id).limit(1))
            if interview is None:
                return None
            token = str(uuid4())
            interview.job = {**interview.job, "token": token}
            interview.version += 1
            interview.processing_status = "running"
            interview.lease_until = now() + timedelta(seconds=timeout + 10)
            return interview.id, token
    except StaleDataError:
        return None


def get_claimed(db, interview_id, token):
    interview = db.get(InterviewSession, interview_id)
    if (interview and interview.processing_status == "running"
            and interview.job.get("token") == token):
        return interview
    return None


async def execute_stage(sessions, settings, interview_id, token):
    with sessions() as db:
        interview = get_claimed(db, interview_id, token)
        if interview is None:
            return
        job, stage, state = interview.job, interview.processing_stage, interview.case_state
        topic = db.get(Topic, state.get("active_topic_id") or interview.topic_id)
        title, opening = state.get("discovered_topic") or topic.title, topic.opening_question
        message = db.get(Message, job["answer_id"]) if "answer_id" in job else None
        model_state = {**state, "intake": interview.context,
                       "recent_history": recent_history(db, interview, message)}
        answer = {"id": message.id, "text": message.text} if message else None
        question = db.get(Message, message.reply_to_question_id).text if message else None
        if stage == "synthesis":
            transcript = [
                {"id": item.id, "kind": item.kind, "text": item.text}
                for item in messages_for(db, interview_id) if item.status != "draft"
            ]

    if stage == "initial_question":
        if state.get("open_interview") and job.get("new_case"):
            output = "Теперь вспомните другой рабочий случай. Что произошло и какую задачу нужно было решить?"
        elif job.get("same_topic"):
            output = f"Вспомните другой рабочий случай по теме «{title}». Что произошло?"
        else:
            output = opening or f"Расскажите о конкретном рабочем случае по теме «{title}»."
            if job.get("new_case"):
                output = f"Перейдём к теме «{title}». {output}"
    elif stage == "extraction":
        output = await extract(settings, question, answer, model_state)
    elif stage == "question":
        output = await formulate(settings, title, job["intent"], answer, model_state)
    elif stage == "synthesis":
        past = state.get("past_cases", [])
        if not past:
            output = await synthesize(settings, title, state, transcript)
        else:
            cases = [*past, {"title": title, "state": state}] if state["facts"] else past
            # Render each case from its own evidence without sending an unbounded
            # multi-case transcript back to the model.
            cards = [render_card(case["title"], case["state"], case["state"]["facts"]) for case in cases]
            output = ({"title": "Карточка интервью", "cases": [content for content, _ in cards]},
                      "\n\n".join(text for _, text in cards))
    else:
        raise InvalidModelOutput("Unknown processing stage")

    try:
        with sessions.begin() as db:
            interview = get_claimed(db, interview_id, token)
            if interview is None:
                return
            interview.version += 1
            if stage == "extraction":
                message = db.get(Message, answer["id"])
                interview.case_state, message.extraction = apply_extraction(
                    state, output, message.id, job["current_intent"],
                )
                message.status = "processed"
                discovered = interview.case_state.get("discovered_topic")
                if discovered and not state.get("discovered_topic"):
                    append_message(db, interview, kind="control", text=f"Тема случая: {discovered}")
                intent = choose_intent(interview.case_state, refused=output.refused,
                                       needs_rephrase=output.needs_rephrase)
                if intent:
                    queue(interview, "question", intent=intent, answer_id=message.id)
                else:
                    advance_case(db, interview)
            elif stage == "synthesis":
                content, text = output
                db.add(KnowledgeCard(
                    session_id=interview.id, owner_id=interview.owner_id, origin=interview.origin,
                    completion_version=job["completion_version"], content=content, text=text,
                ))
                interview.status = "completed"
                clear_job(interview)
            else:
                message = append_message(db, interview, kind="question", text=output)
                interview.current_question_id = message.id
                intent = "get_case" if stage == "initial_question" else job["intent"]
                interview.case_state = {**state, "asked_intents": [*state["asked_intents"], intent]}
                if stage == "initial_question":
                    interview.case_state = {**interview.case_state, "case_start_sequence": message.sequence}
                clear_job(interview)
    except StaleDataError:
        # A finish command or a new lease invalidated this result.
        return


def mark_failed(sessions, interview_id, token, code):
    try:
        with sessions.begin() as db:
            interview = get_claimed(db, interview_id, token)
            if interview is None:
                return
            interview.version += 1
            interview.processing_status = "failed"
            interview.lease_until = None
            interview.error = {
                "code": code, "message": "Обработка прервалась. Повторите попытку.",
                "retryable": True, "request_id": str(uuid4()),
            }
            if interview.processing_stage == "extraction":
                db.get(Message, interview.job["answer_id"]).status = "failed"
    except StaleDataError:
        return


async def run_worker(sessions, settings):
    timeout = settings.llm_timeout_seconds
    while True:
        try:
            job = claim(sessions, timeout)
            if job:
                try:
                    async with asyncio.timeout(timeout):
                        await execute_stage(sessions, settings, *job)
                except (TimeoutError, httpx.TimeoutException):
                    mark_failed(sessions, *job, "PROCESSING_TIMEOUT")
                except (InvalidModelOutput, ValidationError):
                    mark_failed(sessions, *job, "INVALID_MODEL_OUTPUT")
                except (RuntimeError, httpx.RequestError) as exc:
                    code = "INVALID_MODEL_OUTPUT" if str(exc) == "LLM_INVALID_RESPONSE" else "LLM_UNAVAILABLE"
                    mark_failed(sessions, *job, code)
                continue
        except SQLAlchemyError as exc:
            logger.warning("Interview worker database error: %s", type(exc).__name__)
        await asyncio.sleep(0.25)
