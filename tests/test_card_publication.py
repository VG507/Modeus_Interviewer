from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from backend.app import create_app
from backend.card_api import ContentEdit, reconcile_content
from backend.db import InterviewSession, KnowledgeCard, Message, Topic, User, now
from backend.security import hash_password
from backend.settings import Settings

ORIGIN = "http://localhost:5173"
FACT = {"id": "fact-1", "text": "Проверили план запроса", "supporting_message_ids": ["private-message-1"]}
TASK = {**FACT, "id": "task-1", "text": "Ускорить формирование отчёта"}


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
                                 content={"title": title, "task": [TASK], "actions": [FACT], "internal": "private-field"},
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


def test_reordering_legacy_cases_preserves_evidence_without_mixing_case_ids():
    case_a = {"title": "Повторяющаяся задача", "actions": [
        {"id": "fact-a", "text": "Действие A", "supporting_message_ids": ["answer-a"]},
    ]}
    case_b = {"title": case_a["title"], "actions": [
        {"id": "fact-b", "text": "Действие B", "supporting_message_ids": ["answer-b"]},
    ]}
    previous = {"title": "Карточка", "cases": [case_a, case_b]}
    new_case = {"title": case_a["title"], "actions": [{"text": "Новый случай"}]}
    request = ContentEdit.model_validate({**previous, "cases": [new_case, case_b, case_a]})
    ordered = reconcile_content(request, previous, "edit")
    added, first, second = ordered["cases"]
    assert added["actions"][0]["source"] == "owner_edit"
    assert first["actions"][0]["supporting_message_ids"] == ["answer-b"]
    assert second["actions"][0]["supporting_message_ids"] == ["answer-a"]
    request = ContentEdit.model_validate({**ordered, "cases": [
        {**first, "actions": second["actions"]}, second,
    ]})
    edited = reconcile_content(request, ordered, "another-edit")
    foreign = edited["cases"][0]["actions"][0]
    assert foreign["source"] == "owner_edit" and foreign["supporting_message_ids"] == []
    assert "derived_from_fact_id" not in foreign


def test_fact_evidence_requires_current_consent_and_follows_edit_lineage(publication):
    app, clients, (card_id, other_id) = publication
    owner, editor, reader, guest = (clients[role] for role in ("owner", "editor", "reader", "guest"))
    card_path = f"/api/v1/cards/{card_id}"
    def evidence(fact_id):
        return f"{card_path}/facts/{fact_id}/evidence"

    with app.state.sessions.begin() as db:
        card = db.get(KnowledgeCard, card_id)
        other = db.get(KnowledgeCard, other_id)
        question = Message(session_id=card.session_id, sequence=1, kind="question", status="processed",
                           text="Как проверяли план запроса?", market_sources=[
                               {"used": True, "title": "Invalid", "url": "javascript:alert(1)", "task_excerpt": "bad"},
                               {"used": True, "title": "Original", "url": "https://example.test/commit/file",
                                "task_excerpt": "Check the query plan."},
                           ])
        foreign = Message(session_id=other.session_id, sequence=1, kind="question", status="processed",
                          text="Foreign private project")
        db.add_all((question, foreign))
        db.flush()
        quote = "Я проверил план запроса в Postgres"
        grounded = {"id": "supported", "text": quote, "source": "interview"}
        answers = [
            Message(session_id=card.session_id, sequence=index, kind="answer", status="processed",
                    text=quote + " PRIVATE_ANSWER_SUFFIX", reply_to_question_id=parent.id,
                    extraction={"facts": [{"id": grounded["id"], "text": quote}]})
            for index, parent in enumerate((question, foreign), 2)
        ]
        db.add_all(answers)
        db.flush()
        card.content = {**card.content, "verification": [
            {**grounded, "supporting_message_ids": [answer.id for answer in answers]},
        ]}
        answer_id = answers[0].id

    proof = assert_ok(owner.get(evidence("supported")))
    assert proof["skills"] == ["postgresql"]
    assert len(proof["turns"]) == 1 and proof["turns"][0]["answer_id"] == answer_id
    assert proof["turns"][0]["source"]["title"] == "Original"
    assert "PRIVATE_ANSWER_SUFFIX" not in str(proof)
    assert editor.get(evidence("supported")).status_code == reader.get(evidence("supported")).status_code == 404
    assert guest.get(evidence("supported")).status_code == 401

    edited = assert_ok(change(owner, card_path, 1, method="PATCH", text="Исправленная версия", content={
        "title": "Исправленный случай", "task": [TASK], "actions": [FACT],
        "verification": [{"id": "supported", "text": "Уточнённая формулировка"}],
    }))
    changed = edited["content"]["verification"][0]
    assert changed["derived_from_fact_id"] == "supported" and changed["supporting_message_ids"] == []
    revised = assert_ok(owner.get(evidence(changed["id"])))
    assert revised["original_text"] == quote and revised["skills"] == [] and revised["turns"] == proof["turns"]
    assert_ok(change(owner, card_path + "/submit-review", 2, consent=True, show_author=False, share_evidence=True))
    assert editor.get(evidence("supported")).status_code == 404  # Only the current fact is addressable.
    response = editor.get(evidence(changed["id"]))
    assert response.headers["cache-control"] == "no-store"
    assert assert_ok(response)["turns"][0]["answer_id"] is None
    assert_ok(change(editor, card_path + "/review", 3, decision="accept"))
    visible = assert_ok(guest.get(f"/api/v1/catalog/{card_id}"))
    assert visible["content"]["verification"] == [{"text": changed["text"]}]
    assert quote not in str(visible)
    assert_ok(change(editor, card_path + "/unpublish", 4, comment="Уточнить условия"))
    assert editor.get(evidence(changed["id"])).status_code == 404
    assert_ok(owner.get(evidence(changed["id"])))


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
        "title": "Диагностика SQL", "task": [TASK], "actions": [FACT], "marker": "private-marker",
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
                 for value in ("Отчёт формировался медленно.\nДобавили индекс.",
                               "Отчёт формировался медленно.\nПереписали запрос.")]
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
    assert editor.get(path).json()["original"]["content"]["actions"] == [FACT]
    assert_ok(change(editor, path + "/review", 6, decision="accept"))
    public_card = guest.get(public).json()
    assert public_card["author_name"] is None
    assert public_card["content"] == updated["content"]

    with app.state.sessions.begin() as db:
        card = db.scalar(select(KnowledgeCard).where(KnowledgeCard.id == card_id))
        card.consent = {**card.consent, "content_version": card.content_version - 1}
    assert guest.get(public).status_code == 404
    assert guest.get("/api/v1/catalog").json()["items"] == []


@pytest.mark.parametrize(("fields", "text"), [
    ({"task": [], "unknown_fields": ["task", "action"]}, "Карточка\n\nОсталось уточнить: Задача, Действия"),
    ({"task": [TASK], "actions": [{"text": "  "}]}, "Карточка"),
    ({"task": [TASK], "actions": [TASK]}, "Карточка"),
    ({"cases": [{"title": "А", "task": [TASK]}, {"title": "Б", "actions": [FACT]}]}, "Карточка"),
    ({}, "Старое название\n\nЗадача\n• Ускорить отчёт\n\nОсталось уточнить: Действия"),
    ({}, "Карточка\n\nУскорить отчёт\n• Ускорить отчёт!\n—"),
])
def test_incomplete_card_stays_private_and_can_be_completed(publication, fields, text):
    app, clients, (card_id, _) = publication
    owner, editor = clients["owner"], clients["editor"]
    path = f"/api/v1/cards/{card_id}"
    with app.state.sessions.begin() as db:
        card = db.get(KnowledgeCard, card_id)
        card.content = {"title": "Карточка", **fields}
        card.text = text

    before = assert_ok(owner.get(path))
    assert before["review_blocker"]
    response = change(owner, path + "/submit-review", 1, consent=True, show_author=False)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CARD_INCOMPLETE"
    assert assert_ok(owner.get(path)) == before
    assert assert_ok(editor.get("/api/v1/cards?scope=review"))["items"] == []

    edited = assert_ok(change(owner, path, 1, method="PATCH", text="Отчёт выполнялся медленно.\nДобавили индекс."))
    assert edited["review_blocker"] is None
    submitted = assert_ok(change(owner, path + "/submit-review", 2, consent=True, show_author=False))
    assert submitted["publication_status"] == "in_review"


def test_catalog_classifies_open_roles_and_pages_from_newest(publication):
    app, clients, (older, newer) = publication
    with app.state.sessions.begin() as db:
        old_card = db.get(KnowledgeCard, older)
        old_card.created_at = now() - timedelta(days=1)
        new_card = db.get(KnowledgeCard, newer)
        session = db.get(InterviewSession, new_card.session_id)
        session.topic_id = "open-interview"
        session.context = {"role": "Frontend engineer"}

    for card_id in (older, newer):
        path = f"/api/v1/cards/{card_id}"
        assert_ok(change(clients["owner"], path + "/submit-review", 1, consent=True, show_author=False))
        assert_ok(change(clients["editor"], path + "/review", 2, decision="accept"))

    guest = clients["guest"]
    path = "/api/v1/catalog"
    selects = []

    def capture(_, __, statement, *args):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", capture)
    try:
        listing = assert_ok(guest.get(path))
        assert len(listing["items"]) == 2
    finally:
        event.remove(app.state.engine, "before_cursor_execute", capture)
    assert len(selects) == 1

    first = assert_ok(guest.get(path, params={"limit": 1}))
    assert [item["id"] for item in first["items"]] == [newer]
    assert first["items"][0]["profession_id"] == "frontend"
    second = assert_ok(guest.get(path, params={"limit": 1, "cursor": first["next_cursor"]}))
    assert [item["id"] for item in second["items"]] == [older]
    assert second["next_cursor"] is None
    by_profession = assert_ok(guest.get(path, params={"profession_id": "frontend"}))
    assert [item["id"] for item in by_profession["items"]] == [newer]
    by_topic = assert_ok(guest.get(path, params={"topic_title": "SQL"}))
    assert [item["id"] for item in by_topic["items"]] == [older]
    assert set(assert_ok(guest.get(path + "/topics"))["items"]) == {"SQL", "Вторая карточка"}

    app.state.engine.dispose()
    command.downgrade(Config("alembic.ini"), "0015")
    command.upgrade(Config("alembic.ini"), "head")
    assert assert_ok(guest.get(path, params={"profession_id": "frontend"})) == by_profession
