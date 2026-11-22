import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException, Request
from pydantic import ValidationError

from backend import interview_engine
from backend.db import User, database
from backend.interview_api import create_interview, get_interview_profile, save_interview_profile
from backend.interview_engine import prompt_context
from backend.interview_schemas import (
    AnswerResult,
    CreateAnswer,
    CreateInterview,
    ErrorResponse,
    Extraction,
    InterviewContext,
    MessageView,
    VersionedRequest,
)


def test_first_turn_examples_match_contract():
    examples = json.loads(Path("data/interview/first_turn.json").read_text(encoding="utf-8"))
    create = CreateInterview.model_validate(examples["create"])
    draft = CreateAnswer.model_validate(examples["draft"])
    submit = VersionedRequest.model_validate(examples["submit"])
    accepted = AnswerResult.model_validate(examples["accepted"])
    extraction = Extraction.model_validate(examples["extraction"])
    next_question = MessageView.model_validate(examples["next_question"])
    conflict = ErrorResponse.model_validate(examples["conflict"])

    assert accepted.interview.topic_id == create.topic_id
    assert draft.expected_version + 1 == submit.expected_version
    assert accepted.interview.version == submit.expected_version + 1
    assert accepted.answer.reply_to_question_id == draft.question_id
    assert accepted.answer.id == extraction.message_id
    assert next_question.session_id == accepted.interview.id
    assert next_question.sequence == accepted.answer.sequence + 1
    assert conflict.error.current_version == accepted.interview.version


def test_optional_projects_preserve_the_previous_request_shape():
    original = CreateInterview.model_validate({"context": {"role": "QA"}})
    empty = CreateInterview.model_validate({"context": {"role": "QA", "projects": []}})
    assert original.model_dump() == empty.model_dump()
    assert "projects" not in original.model_dump()["context"]

    project = {"task": " Проверка оформления заказа ", "stack": "Playwright"}
    context = {"role": "QA", "projects": [project] * 3}
    saved = CreateInterview.model_validate({"context": context}).model_dump()["context"]
    assert [item["task"] for item in saved["projects"]] == ["Проверка оформления заказа"] * 3
    with pytest.raises(ValidationError):
        CreateInterview.model_validate({"context": {**context, "projects": [project] * 4}})


def test_profile_reuses_older_intake_and_copies_updates_only_to_new_interviews(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    _, sessions = database(database_url)
    with sessions.begin() as db:
        users = [User(email=f"{name}@example.test", password_hash="unused") for name in ("owner", "other")]
        db.add_all(users)
        db.flush()
        owner, other = ({"id": user.id} for user in users)
    origin = "http://localhost:5173"
    app = SimpleNamespace(state=SimpleNamespace(sessions=sessions, app_origins={origin}))
    request = Request({"type": "http", "app": app, "method": "POST", "path": "/api/v1/interviews",
                       "headers": [(b"origin", origin.encode())]})

    assert get_interview_profile(request, owner) is None
    with pytest.raises(HTTPException, match="INTERVIEW_PROFILE_REQUIRED"):
        create_interview(request, owner, "missing", CreateInterview())
    old = create_interview(request, owner, "legacy", CreateInterview(context=InterviewContext(role="QA")))
    assert get_interview_profile(request, owner)["role"] == "QA"
    reused = create_interview(request, owner, "reused", CreateInterview())
    assert reused["context"] == old["context"]

    detail = "Описание работы. " * 45 + "Важная деталь в конце."
    profile = InterviewContext(role="Frontend", responsibilities=detail, projects=[{"task": detail}])
    save_interview_profile(request, owner, profile)
    assert get_interview_profile(request, other) is None
    current = create_interview(request, owner, "current", CreateInterview())
    assert current["context"]["role"] == "Frontend"
    assert create_interview(request, owner, "reused", CreateInterview()) == reused
    model_intake = prompt_context({"intake": current["context"]})["intake"]
    assert model_intake["responsibilities"] == detail
    assert model_intake["projects"][0]["task"] == detail


def test_tone_is_kept_in_the_profile_and_steers_only_the_instructions(database_url, monkeypatch):
    assert "tone" not in InterviewContext(role="QA").model_dump()  # default requests keep their old shape
    with pytest.raises(ValidationError):
        InterviewContext(role="QA", tone="sarcastic")

    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    _, sessions = database(database_url)
    with sessions.begin() as db:
        user = User(email="tone@example.test", password_hash="unused")
        db.add(user)
        db.flush()
        owner = {"id": user.id}
    origin = "http://localhost:5173"
    app = SimpleNamespace(state=SimpleNamespace(sessions=sessions, app_origins={origin}))
    request = Request({"type": "http", "app": app, "method": "POST", "path": "/api/v1/interviews",
                       "headers": [(b"origin", origin.encode())]})
    save_interview_profile(request, owner, InterviewContext(role="QA", tone="playful"))
    assert get_interview_profile(request, owner)["tone"] == "playful"
    context = create_interview(request, owner, "playful", CreateInterview())["context"]
    assert context["tone"] == "playful"

    captured = []

    async def model(settings, schema, instructions, payload, check=None):
        captured.append((instructions, payload))
        return schema(text="Что вы тогда сделали?")

    monkeypatch.setattr(interview_engine, "ask_model", model)
    for tone in interview_engine.TONES:
        state = {"facts": [], "intake": {**context, "tone": tone}, "recent_history": []}
        asyncio.run(interview_engine.draft_question(None, "Тема", "action", {"text": "Ответ"}, state))
    for (instructions, payload), tone in zip(captured, interview_engine.TONES):
        assert instructions.startswith(interview_engine.TONES[tone] + " Задай один короткий вопрос")
        reminder = interview_engine.TONE_REMINDERS.get(tone)
        assert (reminder in instructions) if reminder else "Тон —" not in instructions
        assert "tone" not in payload["intake"]  # an instruction, not a fact about the expert
