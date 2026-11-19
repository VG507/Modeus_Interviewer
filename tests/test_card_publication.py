from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import create_app
from backend.db import InterviewSession, KnowledgeCard, Topic, User
from backend.security import hash_password
from backend.settings import Settings

ORIGIN = "http://localhost:5173"
FACT = {"id": "fact-1", "text": "Проверили план запроса", "supporting_message_ids": ["private-message-1"]}


@pytest.fixture
def publication(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'publication.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(Settings(database_url=url, app_origin=ORIGIN))
    password = "test-password-123"
    digest = hash_password(password)
    with app.state.sessions.begin() as db:
        owner = User(email="owner@example.test", password_hash=digest)
        db.add_all([owner, User(email="editor@example.test", password_hash=digest, is_editor=True),
                    User(email="reader@example.test", password_hash=digest)])
        db.add(Topic(id="sql", profession_id="python_backend", title="SQL", source_ids=[]))
        db.flush()
        cards = []
        for title in ("План SQL-запроса", "Вторая карточка"):
            session = InterviewSession(owner_id=owner.id, topic_id="sql", context={"role": "private-intake"},
                                       status="completed")
            db.add(session)
            db.flush()
            card = KnowledgeCard(session_id=session.id, owner_id=owner.id, origin="human", completion_version=1,
                                 content={"title": title, "actions": [FACT], "internal": "private-field"},
                                 text="Исходный текст")
            db.add(card)
            db.flush()
            cards.append(card.id)

    clients = {role: TestClient(app) for role in ("owner", "editor", "reader", "guest")}
    for role, client in clients.items():
        if role != "guest":
            response = client.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                                   json={"email": f"{role}@example.test", "password": password})
            assert response.status_code == 200
    yield app, clients, cards
    for client in clients.values():
        client.close()
    app.state.engine.dispose()


def change(client, path, version, *, key=None, method="POST", **fields):
    return client.request(method, path, headers={"Origin": ORIGIN, "Idempotency-Key": key or str(uuid4())},
                          json={"expected_version": version, **fields})


def assert_ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def test_publication_full_cycle(publication):
    _, clients, (card_id, private_id) = publication
    owner, editor, reader, guest = (clients[role] for role in ("owner", "editor", "reader", "guest"))
    path = f"/api/v1/cards/{card_id}"
    public = f"/api/v1/catalog/{card_id}"
    queue = "/api/v1/cards?scope=review"
    publish = path + "/review"
    submit = path + "/submit-review"
    assert guest.get(path).status_code == 401
    assert guest.get(public).status_code == 404
    assert guest.get("/api/v1/catalog").json()["items"] == []
    assert reader.get(path).status_code == editor.get(path).status_code == 404
    assert reader.get(queue).status_code == 403
    assert reader.get("/api/v1/cards").json()["items"] == []
    assert change(editor, publish, 1, decision="accept").status_code == 404
    assert change(owner, submit, 1, consent=False, show_author=False).status_code == 422
    assert owner.post(submit, headers={"Idempotency-Key": "missing-origin"},
                      json={"expected_version": 1, "consent": True, "show_author": False}).status_code == 403
    assert owner.post(submit, headers={"Origin": ORIGIN},
                      json={"expected_version": 1, "consent": True, "show_author": False}).status_code == 422

    initial = assert_ok(change(owner, submit, 1, consent=True, show_author=False))
    assert initial["content_version"] == 1
    assert [card["id"] for card in editor.get(queue).json()["items"]] == [card_id]
    assert editor.get(f"/api/v1/cards/{private_id}").status_code == 404
    assert change(owner, publish, 2, decision="accept").status_code == 403
    assert change(editor, publish, 2, decision="return", comment="  ").status_code == 422
    returned = assert_ok(change(editor, publish, 2, decision="return", comment="Уточните результат"))
    assert returned["review_status"] == "needs_changes"
    assert returned["consent"] is None
    assert editor.get(path).status_code == 200
    assert change(editor, path, 3, method="PATCH", text="Чужая правка").status_code == 404

    key = "approved-edit"
    content = {"title": "Индекс для отчёта", "cases": [{
        "title": "Диагностика SQL", "actions": [FACT], "marker": "private-marker",
    }], "actions": [
        FACT, {"id": "invented-id", "text": "Сверили время", "source": "interview",
               "supporting_message_ids": ["fake-reference"]},
    ]}
    text = "Сверили время выполнения."
    edited = assert_ok(change(owner, path, 3, method="PATCH", key=key, content=content, text=text))
    assert edited["content"]["actions"][0]["source"] == "interview"
    changed = edited["content"]["actions"][1]
    assert changed["source"] == "owner_edit" and changed["supporting_message_ids"] == []
    assert changed["id"] != "invented-id"
    assert edited["content_version"] == 2 and edited["review"] is None
    assert assert_ok(change(owner, path, 3, method="PATCH", key=key, content=content, text=text)) == edited
    assert change(owner, path, 3, method="PATCH", key=key, content=content, text="Иное").json()["error"][
        "code"] == "IDEMPOTENCY_CONFLICT"
    detail = owner.get(path).json()
    assert detail["original"]["text"] == "Исходный текст"
    assert changed["edit_id"] == detail["history"][-1]["id"]
    assert detail["history"][-1]["details"]["text_source"] == "owner_edit"

    submitted = assert_ok(change(owner, submit, 4, consent=True, show_author=True, author_name="Автор SQL"))
    assert submitted["content_version"] == submitted["consent"]["content_version"] == 3
    assert change(editor, publish, 2, decision="accept").json()["error"]["code"] == "VERSION_CONFLICT"
    accepted = assert_ok(change(editor, publish, 5, decision="accept"))
    assert accepted["content_version"] == accepted["review"]["content_version"] == 3
    assert accepted["publication_status"] == "published"
    assert change(owner, path, 6, method="PATCH", text="Редактирование после публикации").status_code == 409
    assert change(owner, path + "/unpublish", 6, comment="Причина").status_code == 403

    response = guest.get(public)
    assert response.headers["cache-control"] == "no-store"
    visible = assert_ok(response)
    assert set(visible) == {"id", "title", "profession_id", "topic_title", "content", "text", "reviewed_at", "author_name"}
    assert visible["text"] == text and visible["author_name"] == "Автор SQL"
    assert visible["content"]["actions"] == [{"text": FACT["text"]}, {"text": "Сверили время"}]
    for token in ("private-message-1", "private-marker", "private-intake", "fake-reference", "private-field"):
        assert token not in response.text
    summary = guest.get("/api/v1/catalog").json()["items"]
    assert len(summary) == 1 and set(summary[0]) == set(visible) - {"text", "content"}
    assert [card["id"] for card in editor.get("/api/v1/cards?scope=published").json()["items"]] == [card_id]

    removed = assert_ok(change(editor, path + "/unpublish", 6, comment="Удалите закрытые сведения"))
    assert removed["review_status"] == "needs_changes" and removed["consent"] is None
    assert removed["review"]["comment"] == "Удалите закрытые сведения"
    assert guest.get(public).status_code == 404
    assert guest.get("/api/v1/catalog").json()["items"] == []


def test_review_race_and_updated_consent(publication):
    app, clients, (card_id, _) = publication
    owner, editor, guest = (clients[role] for role in ("owner", "editor", "guest"))
    path = f"/api/v1/cards/{card_id}"
    public = f"/api/v1/catalog/{card_id}"
    assert_ok(change(owner, path + "/submit-review", 1, consent=True, show_author=False))
    with ThreadPoolExecutor(max_workers=2) as pool:
        edits = [pool.submit(change, owner, path, 2, method="PATCH", text=value)
                 for value in ("Правка А", "Правка Б")]
        results = [edit.result() for edit in edits]
    assert sorted(result.status_code for result in results) == [200, 409]
    updated = owner.get(path).json()
    assert updated["publication_status"] == "draft" and updated["consent"] is None
    assert updated["content"] == {"title": "План SQL-запроса"}
    assert change(editor, path + "/review", 3, decision="accept").status_code == 409

    assert_ok(change(owner, path + "/submit-review", 3, consent=True, show_author=False))
    kept = assert_ok(change(editor, path + "/review", 4, decision="keep_private", comment="Только автору"))
    assert kept["review_status"] == "unreviewed" and kept["consent"] is None
    assert guest.get(public).status_code == 404
    assert_ok(change(owner, path + "/submit-review", 5, consent=True, show_author=False))
    assert_ok(change(editor, path + "/review", 6, decision="accept"))
    assert guest.get(public).json()["author_name"] is None

    with app.state.sessions.begin() as db:
        card = db.scalar(select(KnowledgeCard).where(KnowledgeCard.id == card_id))
        card.consent = {**card.consent, "content_version": card.content_version - 1}
    assert guest.get(public).status_code == 404
    assert guest.get("/api/v1/catalog").json()["items"] == []
