from datetime import datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from backend.db import HeadHunterRequestWindow, InterviewSession, LlmResponse, User, copy_database, database


def migrated(url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    return database(url)


def test_rows_move_to_a_fresh_database_with_their_values(database_url, tmp_path, monkeypatch):
    source_url = f"sqlite:///{(tmp_path / 'source.db').as_posix()}"
    source, sessions = migrated(source_url, monkeypatch)
    started = datetime(2026, 11, 21, 12, 30)
    with sessions.begin() as db:
        owner = User(email="owner@example.test", password_hash="hash", is_editor=True)
        db.add(owner)
        db.flush()
        db.add(InterviewSession(owner_id=owner.id, topic_id="open-interview", context={"role": "QA", "projects": []},
                                case_state={"facts": [{"text": "Отчёт строился минуту"}]}))
        db.add(LlmResponse(key="k" * 64, model="test-model", response={"text": "Что вы сделали?"}, created_at=started))
        db.get(HeadHunterRequestWindow, 1).last_started_at = started
    source.dispose()

    target, sessions = migrated(database_url, monkeypatch)
    copied = copy_database(source_url, database_url)
    assert copied["users"] == copied["interview_sessions"] == copied["llm_responses"] == 1
    with sessions() as db:
        assert db.scalar(select(User.is_editor))
        interview = db.scalar(select(InterviewSession))
        assert interview.context == {"role": "QA", "projects": []}
        assert interview.case_state["facts"][0]["text"] == "Отчёт строился минуту"
        assert db.get(LlmResponse, "k" * 64).created_at == started
        # The seeded row comes from the source, not from the target's migration.
        assert db.get(HeadHunterRequestWindow, 1).last_started_at == started
    # A serial key keeps counting after the copied ids.
    with sessions.begin() as db:
        db.add(HeadHunterRequestWindow(last_started_at=started))
    with sessions() as db:
        assert sorted(db.scalars(select(HeadHunterRequestWindow.id))) == [1, 2]

    with pytest.raises(RuntimeError, match="TARGET_IN_USE"):
        copy_database(source_url, database_url)
    target.dispose()
