"""Run a synthetic first turn with the configured LLM in an isolated SQLite database."""

from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from backend.app import create_app
from backend.db import InterviewSession, Topic, User
from backend.security import hash_password
from backend.settings import Settings


def run(settings):
    email = "smoke@example.test"
    password = str(uuid4())
    app = create_app(settings)
    with app.state.sessions.begin() as db:
        db.add(User(email=email, password_hash=hash_password(password)))
        db.add(Topic(id="sql-test", profession_id="python_backend",
                     title="Диагностика медленного SQL-запроса", source_ids=[]))

    with TestClient(app) as client:
        def post(path, payload, expected):
            response = client.post(path, json=payload, headers={
                "Origin": settings.app_origin, "Idempotency-Key": str(uuid4()),
            })
            if response.status_code != expected:
                raise RuntimeError(f"{path}: HTTP {response.status_code} {response.text}")
            return response.json()

        def wait(path):
            deadline = monotonic() + 3 * settings.llm_timeout_seconds + 15
            while monotonic() < deadline:
                response = client.get(path)
                response.raise_for_status()
                state = response.json()
                if state["processing_status"] == "failed":
                    raise RuntimeError(str(state["error"]))
                if state["processing_status"] == "idle":
                    return state
                sleep(0.3)
            raise TimeoutError("Interview did not settle")

        post("/api/v1/auth/login", {"email": email, "password": password}, 200)
        interview = post("/api/v1/interviews", {"topic_id": "sql-test", "context": {"role": "Backend"}}, 201)
        path = f'/api/v1/interviews/{interview["id"]}'
        state = wait(path)
        with app.state.sessions.begin() as db:
            record = db.get(InterviewSession, interview["id"])
            record.origin = "synthetic"
            record.version += 1
        state = wait(path)

        draft = post(path + "/answers", {
            "expected_version": state["version"], "question_id": state["current_question_id"],
            "text": "В PostgreSQL SQL-запрос для ежемесячного отчёта выполнялся две секунды. "
                    "Я добавил индекс по полю даты.",
        }, 201)
        answer_id = draft["answer"]["id"]
        start = monotonic()
        post(f"{path}/answers/{answer_id}/submit", {"expected_version": draft["interview"]["version"]}, 202)
        followup = wait(path)
        assert followup["current_question_id"] not in (None, state["current_question_id"])
        print(f'Следующий вопрос ({monotonic() - start:.1f} с): {followup["messages"][-1]["text"]}', flush=True)
        post(path + "/finish", {"expected_version": followup["version"]}, 202)
        final = wait(path)
        response = client.get(f'/api/v1/cards/{final["card_id"]}')
        response.raise_for_status()
        card = response.json()
        assert card["origin"] == "synthetic" and card["publication_status"] == "draft"
        assert card["content"]["actions"]
        assert all(fact["supporting_message_ids"] == [answer_id] for fact in card["content"]["actions"])
        print(card["text"])


def main():
    with TemporaryDirectory() as folder:
        url = f"sqlite:///{(Path(folder) / 'interview.db').as_posix()}"
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                config = Config("alembic.ini")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        finally:
            engine.dispose()
        run(Settings(database_url=url))


if __name__ == "__main__":
    main()
