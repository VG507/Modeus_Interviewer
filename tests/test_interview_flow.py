import time
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from backend import interview_worker
from backend.app import create_app
from backend.db import Topic, User
from backend.interview_engine import Candidate, Evidence
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

    async def save_card(settings, title, state, messages):
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
        created = post("/api/v1/interviews", {"topic_id": "sql", "context": {"role": "Backend"}})
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
        assert post(continuing + "/finish", {"expected_version": after_worker["version"]}).status_code == 202
        continued_card_id = wait_for(client, continuing, "idle")["card_id"]
        cases = client.get(f"/api/v1/cards/{continued_card_id}").json()["content"]["cases"]
        assert [case["title"] for case in cases] == ["SQL", "Сбой фоновой задачи"]
        assert [case["actions"][0]["supporting_message_ids"] for case in cases] == [
            [sql_answer], [worker_answer],
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
