import asyncio
import time
from threading import Event
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from backend import interview_worker
from backend.app import create_app
from backend.db import InterviewSession, Message, Topic, User
from backend.interview_engine import Candidate, Evidence
from backend.interview_schemas import CaseState
from backend.security import hash_password
from backend.settings import Settings

ORIGIN = "http://localhost:5173"


def wait_for(client, path, status, stage=None):
    for _ in range(200):
        response = client.get(path)
        assert response.status_code == 200, response.text
        state = response.json()
        if state["processing_status"] == status and state["processing_stage"] == stage:
            return state
        time.sleep(0.025)
    raise AssertionError(state)


def test_worker_continues_after_an_incomplete_job(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'worker.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(Settings(database_url=url, app_origin=ORIGIN, documentation_weekly_enabled=False,
                              hh_weekly_enabled=False))
    with app.state.sessions.begin() as db:
        owner = User(email="owner@example.test", password_hash=hash_password("test-password-123"))
        db.add(owner)
        db.flush()
        invalid, opening = [InterviewSession(
            owner_id=owner.id, topic_id="open-interview", context={"role": "QA engineer"},
            case_state=CaseState(open_interview=True).model_dump(), processing_status="queued",
            processing_stage=stage, job=None,
        ) for stage in ("extraction", "initial_question")]
        db.add_all((invalid, opening))
        db.flush()
        opening.case_state = {"open_interview": True}
        missing_parent = InterviewSession(
            owner_id=owner.id, topic_id="open-interview", context={"role": "QA engineer"},
            case_state=CaseState(open_interview=True).model_dump(), processing_status="queued",
            processing_stage="extraction", job={"current_intent": "get_case"},
        )
        db.add(missing_parent)
        db.flush()
        without_question = Message(session_id=missing_parent.id, sequence=1, kind="answer",
                                   text="Ответ из старой записи", status="queued")
        db.add(without_question)
        db.flush()
        missing_parent.job = {**missing_parent.job, "answer_id": without_question.id}
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", headers={"Origin": ORIGIN}, json={
            "email": owner.email, "password": "test-password-123",
        }).status_code == 200
        invalid_path = f"/api/v1/interviews/{invalid.id}"
        failed = wait_for(client, invalid_path, "failed", "extraction")
        assert failed["error"]["code"] == "INVALID_STATE"
        assert not failed["error"]["retryable"]
        retry = client.post(invalid_path + "/retry", headers={"Origin": ORIGIN, "Idempotency-Key": str(uuid4())},
                            json={"expected_version": failed["version"]})
        assert retry.status_code == 409
        assert wait_for(client, f"/api/v1/interviews/{opening.id}", "idle")["current_question_id"]
        failed = wait_for(client, f"/api/v1/interviews/{missing_parent.id}", "failed", "extraction")
        assert failed["error"]["code"] == "INVALID_STATE"
        with app.state.sessions() as db:
            assert db.get(Message, without_question.id).status == "failed"


def test_invalid_question_state_is_rejected_without_writing(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'question.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(Settings(database_url=url, app_origin=ORIGIN, hh_weekly_enabled=False,
                              documentation_weekly_enabled=False))
    with app.state.sessions.begin() as db:
        owner = User(email="owner@example.test", password_hash=hash_password("test-password-123"))
        db.add(owner)
        db.flush()
        db.add(Topic(id="audit-topic", profession_id="python_backend", title="Исходная тема", source_ids=[]))
        db.flush()
        interview = InterviewSession(
            owner_id=owner.id, topic_id="audit-topic", context={"role": "Backend"},
            case_state=CaseState(active_topic_id="retired-topic", asked_intents=["get_case"]).model_dump(),
            current_question_id="missing-question",
        )
        db.add(interview)
        db.flush()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/api/v1/auth/login", headers={"Origin": ORIGIN}, json={
            "email": owner.email, "password": "test-password-123",
        }).status_code == 200
        path = f"/api/v1/interviews/{interview.id}"

        def post(suffix, payload):
            return client.post(path + suffix, json={"expected_version": 1, **payload},
                               headers={"Origin": ORIGIN, "Idempotency-Key": str(uuid4())})

        for result in (post("/answers", {"question_id": "missing-question", "text": "Ответ"}), post("/skip", {})):
            assert result.status_code == 409
            assert result.json()["error"]["code"] == "INVALID_STATE"
        assert client.get(path).json()["current_topic_title"] == "Исходная тема"
        assert client.get("/api/v1/interviews").json()["items"][0]["current_topic_title"] == "Исходная тема"

        with app.state.sessions.begin() as db:
            record = db.get(InterviewSession, interview.id)
            question = Message(session_id=record.id, sequence=1, kind="question", text="Что случилось?",
                               status="processed")
            db.add(question)
            db.flush()
            record.current_question_id = question.id
            record.case_state = {**record.case_state, "asked_intents": []}
            draft = Message(session_id=record.id, sequence=2, kind="answer", text="Ответ",
                            status="draft", reply_to_question_id=question.id)
            db.add(draft)
            db.flush()
        assert post(f"/answers/{draft.id}/submit", {}).status_code == 409
        assert post("/skip", {}).status_code == 409
        detail = client.get(path).json()
        assert detail["version"] == 1
        assert [message["status"] for message in detail["messages"]] == ["processed", "draft"]


def test_interview_retries_and_private_draft(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'interview.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(Settings(database_url=url, app_origin=ORIGIN))
    with app.state.sessions.begin() as db:
        db.add(Topic(id="sql", profession_id="python_backend", title="SQL", source_ids=[]))
        db.add(Topic(id="worker", profession_id="python_backend", title="Сбой фоновой задачи", source_ids=[]))
        db.add_all([
            User(email=email, password_hash=hash_password("test-password-123"))
            for email in ("owner@example.test", "other@example.test")
        ])

    extraction_calls = []
    question_calls = []

    async def extract_once(settings, question, answer, state):
        assert state["intake"]["role"] == "Backend"
        assert state["intake"]["projects"][0]["task"] == "Сервис расчёта доставки"
        extraction_calls.append(answer["text"])
        return Evidence(facts=[Candidate(field="task", quote="Медленный отчёт"),
                               Candidate(field="action", quote="добавил индекс")],
                        ambiguous=[], not_applicable=[], refused=False)

    async def fail_then_ask(settings, topic, intent, answer, state):
        assert state["intake"]["role"] == "Backend"
        question_calls.append(intent)
        if len(question_calls) == 1:
            raise httpx.ReadTimeout("simulated")
        return "Почему вы решили добавить индекс?"

    async def save_card(settings, title, state):
        return {"title": title, "actions": [state["facts"][1]]}, "Добавлен индекс."

    monkeypatch.setattr(interview_worker, "extract", extract_once)
    monkeypatch.setattr(interview_worker, "formulate", fail_then_ask)
    monkeypatch.setattr(interview_worker, "synthesize", save_card)
    with TestClient(app) as client:
        def login(email):
            response = client.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                                   json={"email": email, "password": "test-password-123"})
            assert response.status_code == 200

        def post(url, payload, key=None):
            return client.post(url, headers={"Origin": ORIGIN, "Idempotency-Key": key or str(uuid4())},
                               json=payload)

        login("owner@example.test")
        assert client.get("/api/v1/topics").json()["items"][0]["id"] == "sql"
        created = post("/api/v1/interviews", {"topic_id": "sql", "context": {
            "role": "Backend", "projects": [{"task": "Сервис расчёта доставки"}],
        }})
        assert created.status_code == 201, created.text
        path = f'/api/v1/interviews/{created.json()["id"]}'
        state = wait_for(client, path, "idle")
        saved = post(path + "/answers", {"expected_version": state["version"],
                     "question_id": state["current_question_id"], "text": "Медленный отчёт, добавил индекс."})
        assert saved.status_code == 201, saved.text
        assert not extraction_calls
        draft = saved.json()
        submit_path = f'{path}/answers/{draft["answer"]["id"]}/submit'
        payload = {"expected_version": draft["interview"]["version"]}
        submitted = post(submit_path, payload, "submit")
        assert submitted.status_code == 202, submitted.text
        failed = wait_for(client, path, "failed", "question")
        assert failed["error"]["code"] == "PROCESSING_TIMEOUT"
        assert post(submit_path, payload, "submit").json() == submitted.json()
        conflict = post(submit_path, {"expected_version": failed["version"]}, "submit")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        stale = post(path + "/retry", {"expected_version": payload["expected_version"]})
        assert stale.status_code == 409
        assert stale.json()["error"]["current_version"] == failed["version"]
        assert post(path + "/retry", {"expected_version": failed["version"]}).status_code == 202
        updated = wait_for(client, path, "idle")
        assert updated["messages"][-1]["text"] == "Почему вы решили добавить индекс?"
        assert len(extraction_calls) == 1
        assert question_calls == ["decision", "decision"]

        async def reject_unrelated(settings, question, answer, state):
            assert state["recent_history"][0]["answer"] == "Медленный отчёт, добавил индекс."
            return Evidence(facts=[], ambiguous=[], not_applicable=[], refused=False,
                            assessment="unrelated", needs_rephrase=True)

        monkeypatch.setattr(interview_worker, "extract", reject_unrelated)
        draft_off_topic = post(path + "/answers", {
            "expected_version": updated["version"], "question_id": updated["current_question_id"],
            "text": "В выходные пойдём в кино.",
        }).json()
        assert post(f'{path}/answers/{draft_off_topic["answer"]["id"]}/submit',
                    {"expected_version": draft_off_topic["interview"]["version"]}).status_code == 202
        updated = wait_for(client, path, "idle")
        assert next(message for message in updated["messages"]
                    if message["id"] == draft_off_topic["answer"]["id"])["assessment"] == "unrelated"
        assert post(path + "/finish", {"expected_version": updated["version"]}).status_code == 202
        final = wait_for(client, path, "idle")
        assert final["status"] == "completed"
        card_path = f'/api/v1/cards/{final["card_id"]}'
        card = client.get(card_path).json()
        assert card["publication_status"] == "draft"
        assert card["content"]["actions"][0]["supporting_message_ids"] == [draft["answer"]["id"]]
        assert "Сервис расчёта доставки" not in card["text"]
        assert len(client.get("/api/v1/cards").json()["items"]) == 1

        async def full_case(settings, question, answer, state):
            assert state["recent_history"] == []
            return Evidence(facts=[Candidate(field=field, quote=answer["text"]) for field in (
                "task", "context", "action", "decision", "verification",
            )], ambiguous=[], not_applicable=[], refused=False)

        monkeypatch.setattr(interview_worker, "extract", full_case)
        second = post("/api/v1/interviews", {"topic_id": "sql", "context": {"role": "Backend"}})
        continuing = f'/api/v1/interviews/{second.json()["id"]}'

        def submit_complete_case(text, title):
            current = wait_for(client, continuing, "idle")
            assert current["current_topic_title"] == title
            saved = post(continuing + "/answers", {
                "expected_version": current["version"], "question_id": current["current_question_id"], "text": text,
            }).json()
            assert post(f'{continuing}/answers/{saved["answer"]["id"]}/submit',
                        {"expected_version": saved["interview"]["version"]}).status_code == 202
            return saved["answer"]["id"], wait_for(client, continuing, "idle")

        sql_answer, after_sql = submit_complete_case("Подробный случай SQL", "SQL")
        assert after_sql["status"] == "active" and after_sql["card_id"] is None
        assert after_sql["current_topic_title"] == "Сбой фоновой задачи"
        worker_answer, after_worker = submit_complete_case("Подробный случай фоновой задачи", "Сбой фоновой задачи")
        assert after_worker["status"] == "active" and after_worker["card_id"] is None
        assert after_worker["current_topic_title"] == "SQL"
        expected_cases = [("SQL", sql_answer), ("Сбой фоновой задачи", worker_answer)]
        for index in range(3, 9):
            title = "SQL" if index % 2 else "Сбой фоновой задачи"
            answer_id, after_worker = submit_complete_case(f"Подробный случай {index}", title)
            expected_cases.append((title, answer_id))
        assert post(continuing + "/finish", {"expected_version": after_worker["version"]}).status_code == 202
        continued_card_id = wait_for(client, continuing, "idle")["card_id"]
        cases = client.get(f"/api/v1/cards/{continued_card_id}").json()["content"]["cases"]
        assert [(case["title"], case["actions"][0]["supporting_message_ids"]) for case in cases] == [
            (title, [answer_id]) for title, answer_id in expected_cases
        ]

        async def discover(settings, question, answer, state):
            result = await full_case(settings, question, answer, state)
            return result.model_copy(update={"topic_title": "Разбор сбоя очереди"})

        monkeypatch.setattr(interview_worker, "extract", discover)
        open_session = post("/api/v1/interviews", {"context": {"role": "Backend"}})
        assert open_session.status_code == 201, open_session.text
        open_path = f'/api/v1/interviews/{open_session.json()["id"]}'
        open_state = wait_for(client, open_path, "idle")
        assert "конкретный случай" in open_state["messages"][-1]["text"]
        draft = post(open_path + "/answers", {
            "expected_version": open_state["version"], "question_id": open_state["current_question_id"],
            "text": "Очередь перестала доставлять задачи после релиза.",
        }).json()
        assert post(f'{open_path}/answers/{draft["answer"]["id"]}/submit',
                    {"expected_version": draft["interview"]["version"]}).status_code == 202
        open_state = wait_for(client, open_path, "idle")
        assert open_state["status"] == "active" and open_state["card_id"] is None
        assert open_state["messages"][-2]["text"] == "Тема случая: Разбор сбоя очереди"
        assert "другой рабочий случай" in open_state["messages"][-1]["text"]
        assert post(open_path + "/finish", {"expected_version": open_state["version"]}).status_code == 202
        draft_id = wait_for(client, open_path, "idle")["card_id"]
        assert client.get(f"/api/v1/cards/{draft_id}").json()["content"]["cases"][0]["title"] == "Разбор сбоя очереди"

        login("other@example.test")
        assert client.get(path).status_code == client.get(card_path).status_code == 404
        assert client.get("/api/v1/cards").json()["items"] == []


def test_finish_while_extraction_runs_keeps_submitted_answer_once(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'finish.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(Settings(database_url=url, app_origin=ORIGIN))
    with app.state.sessions.begin() as db:
        db.add(User(email="owner@example.test", password_hash=hash_password("test-password-123")))

    started = Event()
    release = Event()
    calls = []
    reply = "После релиза тесты оформления заказов перестали проходить."

    async def extract(settings, question, answer, state):
        calls.append(answer["id"])
        if len(calls) == 1:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return Evidence(facts=[], ambiguous=[], not_applicable=[], refused=False)
        if len(calls) == 2:
            raise httpx.ReadTimeout("simulated")
        return Evidence(facts=[Candidate(field="task", quote=answer["text"])],
                        ambiguous=[], not_applicable=[], refused=False, topic_title="Сбой тестов заказов")

    monkeypatch.setattr(interview_worker, "extract", extract)
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", headers={"Origin": ORIGIN}, json={
            "email": "owner@example.test", "password": "test-password-123",
        }).status_code == 200

        def post(url, body, key=None):
            return client.post(url, headers={"Origin": ORIGIN, "Idempotency-Key": key or str(uuid4())}, json=body)

        session = post("/api/v1/interviews", {"context": {"role": "QA"}}).json()
        path = f'/api/v1/interviews/{session["id"]}'
        current = wait_for(client, path, "idle")
        draft = post(path + "/answers", {
            "expected_version": current["version"], "question_id": current["current_question_id"], "text": reply,
        }).json()
        answer_id = draft["answer"]["id"]
        assert post(f"{path}/answers/{answer_id}/submit", {
            "expected_version": draft["interview"]["version"],
        }).status_code == 202
        assert started.wait(5)
        running = wait_for(client, path, "running", "extraction")
        finish = {"expected_version": running["version"]}
        try:
            accepted = post(path + "/finish", finish, "finish")
            assert accepted.status_code == 202
        finally:
            release.set()

        failed = wait_for(client, path, "failed", "synthesis")
        assert failed["status"] == "completing" and failed["card_id"] is None
        assert failed["error"]["code"] == "PROCESSING_TIMEOUT"
        cookies = client.cookies

    resumed = create_app(Settings(database_url=url, app_origin=ORIGIN))
    client = TestClient(resumed, cookies=cookies)
    assert post(path + "/retry", {"expected_version": failed["version"]}).status_code == 202
    wait_for(client, path, "queued", "synthesis")
    with client:
        completed = wait_for(client, path, "idle")
        assert completed["status"] == "completed"
        assert [message["kind"] for message in completed["messages"]] == ["question", "answer", "control"]
        assert completed["messages"][1]["status"] == "processed"
        card = client.get(f'/api/v1/cards/{completed["card_id"]}').json()
        assert [(fact["text"], fact["supporting_message_ids"]) for fact in card["content"]["task"]] == [
            (reply, [answer_id]),
        ]
        assert post(path + "/finish", finish, "finish").json() == accepted.json()
        assert len(client.get("/api/v1/cards").json()["items"]) == 1
        assert calls == [answer_id] * 3
