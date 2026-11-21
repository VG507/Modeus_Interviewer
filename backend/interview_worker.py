"""Persisted processing stages for the interview API."""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError

from backend.db import InterviewSession, KnowledgeCard, Message, Topic, now
from backend.interview_engine import (
    FALLBACK_QUESTIONS,
    InvalidModelOutput,
    apply_extraction,
    choose_intent,
    extract,
    formulate,
    formulate_question,
    render_card,
    synthesize,
)
from backend.interview_schemas import CaseState, OPEN_TOPIC_ID
from backend.knowledge import prior_case_context, publication_versions_current, published_context
from backend.source_context import skill_sources_current, sources_for_followup

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
        question = answer_question(db, interview, reply)
        turns.append({"question": (question.text or "")[:400] if question else "",
                      "answer": (reply.text or "")[:1000]})
        if len(turns) == 3:
            break
    return list(reversed(turns))


def append_message(db, interview, *, kind, text, status="processed", reply_to_question_id=None, market_sources=None):
    last_sequence = db.scalar(select(func.max(Message.sequence)).where(Message.session_id == interview.id))
    message = Message(
        id=str(uuid4()), session_id=interview.id, sequence=(last_sequence or 0) + 1,
        kind=kind, text=text, status=status, reply_to_question_id=reply_to_question_id, market_sources=market_sources,
    )
    db.add(message)
    return message


def complete(db, interview):
    pending = {}
    if interview.processing_stage == "extraction":
        job = job_payload(interview)
        message = job_answer(db, interview, job)
        if message:
            if job.get("current_intent") and answer_question(db, interview, message):
                pending = {"answer_id": message.id, "current_intent": job["current_intent"]}
            if message.status == "queued":
                message.status = "failed"
    interview.status = "completing"
    interview.current_question_id = None
    queue(interview, "synthesis", completion_version=interview.version, **pending)


def advance_case(db, interview):
    state = dict(interview.case_state)
    original = db.get(Topic, interview.topic_id)
    active_id = state.pop("active_topic_id", None)
    if original is None:
        raise InvalidJob("Missing interview topic")
    current = (db.get(Topic, active_id) if active_id else None) or original
    past = state.pop("past_cases", [])
    open_interview = original.id == OPEN_TOPIC_ID
    if open_interview:
        following = original
    else:
        topics = list(db.scalars(select(Topic).where(
            Topic.profession_id == original.profession_id, Topic.id != OPEN_TOPIC_ID,
        ).order_by(Topic.id)))
        position = next((index for index, item in enumerate(topics) if item.id == current.id), -1)
        following = topics[(position + 1) % len(topics)] if topics else original
    if state.get("facts"):
        past = [*past, {"title": state.get("discovered_topic") or current.title, "state": state}]
    interview.case_state = CaseState(active_topic_id=following.id, past_cases=past,
                                     open_interview=open_interview).model_dump()
    interview.current_question_id = None
    queue(interview, "initial_question", new_case=True, same_topic=following.id == current.id)


class InvalidJob(Exception):
    pass


def job_payload(interview):
    return interview.job if isinstance(interview.job, dict) else {}


def job_answer(db, interview, job):
    answer_id = job.get("answer_id")
    answer = db.get(Message, answer_id) if isinstance(answer_id, str) and answer_id else None
    if answer and answer.session_id == interview.id and answer.kind == "answer":
        return answer
    return None


def answer_question(db, interview, answer):
    return interview_question(db, interview, answer.reply_to_question_id) if answer else None


def interview_question(db, interview, question_id):
    question = db.get(Message, question_id) if question_id else None
    if question and question.session_id == interview.id and question.kind == "question" and question.text:
        return question
    return None


def valid_job(db, interview, job):
    stage = interview.processing_stage
    if stage == "initial_question":
        return True
    if stage == "question":
        return bool(job.get("intent"))
    if stage == "synthesis":
        if not isinstance(job.get("completion_version"), int):
            return False
        if "answer_id" not in job:
            return True
    elif stage != "extraction":
        return False
    answer = job_answer(db, interview, job)
    intent = job.get("current_intent")
    return bool(isinstance(intent, str) and intent and answer and answer.text and answer_question(db, interview, answer))


def claim(sessions, timeout):
    try:
        with sessions.begin() as db:
            interview = db.scalar(select(InterviewSession).where(or_(
                InterviewSession.processing_status == "queued",
                and_(InterviewSession.processing_status == "running",
                     or_(InterviewSession.lease_until.is_(None), InterviewSession.lease_until <= now())),
            )).order_by(InterviewSession.updated_at, InterviewSession.id).limit(1))
            if interview is None:
                return None
            token = str(uuid4())
            interview.job = {**job_payload(interview), "token": token}
            interview.version += 1
            interview.processing_status = "running"
            interview.lease_until = now() + timedelta(seconds=timeout + 10)
            return interview.id, token
    except StaleDataError:
        return None


def get_claimed(db, interview_id, token):
    interview = db.get(InterviewSession, interview_id)
    if (interview and interview.processing_status == "running"
            and job_payload(interview).get("token") == token):
        return interview
    return None


async def execute_stage(sessions, settings, interview_id, token):
    with sessions() as db:
        interview = get_claimed(db, interview_id, token)
        if interview is None:
            return
        job, stage = job_payload(interview), interview.processing_stage
        if not valid_job(db, interview, job):
            raise InvalidJob("Incomplete processing job")
        try:
            state = CaseState.model_validate(interview.case_state).model_dump()
        except ValidationError as exc:
            raise InvalidJob("Invalid interview case state") from exc
        active_id = state.get("active_topic_id")
        topic = (db.get(Topic, active_id) if active_id else None) or db.get(Topic, interview.topic_id)
        if topic is None:
            raise InvalidJob("Missing interview topic")
        title, opening = state.get("discovered_topic") or topic.title, topic.opening_question
        message = job_answer(db, interview, job)
        model_state = {**state, "intake": interview.context,
                       "recent_history": recent_history(db, interview, message)}
        market_sources = []
        publication_versions = {}
        if stage == "question":
            if state["past_cases"] and state.get("case_start_sequence") is not None and message:
                # Identity corrections matter even when extraction marked the turn unrelated.
                recent_answers = db.scalars(select(Message.text).where(
                    Message.session_id == interview.id, Message.kind == "answer", Message.status == "processed",
                    Message.sequence >= state["case_start_sequence"], Message.sequence < message.sequence,
                    Message.text.is_not(None),
                ).order_by(Message.sequence.desc()).limit(6))
                previous = prior_case_context(state, message.text or "", recent_answers)
                if previous and job["intent"] in {"action", "decision", "verification", "context"}:
                    model_state["prior_project_context"] = previous
            gaps, publication_versions = published_context(db, interview, state, job["intent"])
            if gaps:
                model_state["knowledge_gaps"] = gaps
            market_sources = sources_for_followup(
                db, interview.context, model_state, job["intent"], message.text if message else "",
            )
        if market_sources:
            model_state["market_context"] = [
                {key: source[key] for key in ("task_excerpt", "skills", "kind", "purpose", "detail", "version", "provider")
                 if key in source and source[key] is not None} for source in market_sources
            ]
        answer = {"id": message.id, "text": message.text} if message else None
        parent = answer_question(db, interview, message)
        question = parent.text if parent else None

    final_extraction = None
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
        if market_sources:
            result = await formulate_question(settings, title, job["intent"], answer, model_state)
            output = result.text
            used_number = getattr(result, "source_number", None)
            for number, source in enumerate(market_sources, start=1):
                source["used"] = number == used_number
        else:
            output = await formulate(settings, title, job["intent"], answer, model_state)
    elif stage == "synthesis":
        if answer is not None:
            evidence = await extract(settings, question, answer, model_state)
            state, final_extraction = apply_extraction(state, evidence, answer["id"], job["current_intent"])
            title = state.get("discovered_topic") or title
        past = state.get("past_cases", [])
        if not past:
            output = await synthesize(settings, title, state)
        else:
            cases = [*past, {"title": title, "state": state}] if state["facts"] else past
            # Render each case from its own evidence without sending an unbounded
            # multi-case transcript back to the model.
            cards = [render_card(case["title"], case["state"], case["state"]["facts"]) for case in cases]
            output = ({"title": "Карточка интервью",
                       "cases": [{**content, "case_id": str(uuid4())} for content, _ in cards]},
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
                message = job_answer(db, interview, job)
                if message is None:
                    raise InvalidJob("Missing interview answer")
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
                if final_extraction is not None:
                    message = job_answer(db, interview, job)
                    if message is None:
                        raise InvalidJob("Missing interview answer")
                    message.extraction = final_extraction
                    message.status = "processed"
                    interview.case_state = state
                db.add(KnowledgeCard(
                    session_id=interview.id, owner_id=interview.owner_id, origin=interview.origin,
                    completion_version=job["completion_version"], content=content, text=text,
                ))
                interview.status = "completed"
                clear_job(interview)
            else:
                skill_sources = [source for source in market_sources if source.get("provider") == "skills"]
                if publication_versions or skill_sources:
                    db.flush()
                    if ((publication_versions and not publication_versions_current(db, publication_versions))
                            or not skill_sources_current(db, skill_sources)):
                        output = FALLBACK_QUESTIONS[job["intent"]]
                        market_sources = []
                message = append_message(db, interview, kind="question", text=output, market_sources=market_sources or None)
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
                "retryable": code != "INVALID_STATE", "request_id": str(uuid4()),
            }
            if interview.processing_stage == "extraction":
                answer = job_answer(db, interview, job_payload(interview))
                if answer is not None:
                    answer.status = "failed"
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
                except InvalidJob:
                    mark_failed(sessions, *job, "INVALID_STATE")
                except (RuntimeError, httpx.RequestError) as exc:
                    code = "INVALID_MODEL_OUTPUT" if str(exc) == "LLM_INVALID_RESPONSE" else "LLM_UNAVAILABLE"
                    mark_failed(sessions, *job, code)
                except SQLAlchemyError:
                    raise
                except Exception:
                    logger.exception("Interview processing failed for %s", job[0])
                    mark_failed(sessions, *job, "INTERNAL_SERVER_ERROR")
                continue
        except SQLAlchemyError as exc:
            logger.warning("Interview worker database error: %s", type(exc).__name__)
        except Exception:
            logger.exception("Interview worker cycle failed")
        await asyncio.sleep(0.25)
