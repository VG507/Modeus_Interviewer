import json
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from backend import embeddings, vectors
from backend.app import create_app
from backend.db import CardEmbedding, InterviewSession, KnowledgeCard, User, VacancyCollection, database, now
from backend.knowledge import published_cards, published_context
from backend.settings import Settings
from backend.themes import close_pairs
from backend.vacancy_context import sources_for_question
from backend.vacancy_import import prepare_cards
from tests.test_vacancies import vacancy

# Each concept is one axis of the fake model; a text points along the concepts it names.
CONCEPTS = (("отч", "запрос", "postgres", "индекс"), ("тест", "playwright", "прогон"), ("сертификат", "tls"))


def fake_vector(text):
    return embeddings.unit([sum(word in text.casefold() for word in words) for words in CONCEPTS] + [0.1])


def content(title, task, action, **extra):
    return {"title": title, "task": [{"text": task}], "context": [], "actions": [{"text": action}],
            "decisions": [], "verification": [], "errors": [], "limitations": [], **extra}


def publish(sessions, cards, drafts=()):
    """{name: card id} for cards stored by one author; names in drafts stay unpublished."""
    ids = {}
    with sessions.begin() as db:
        owner = User(email="author@example.test", password_hash="unused")
        db.add(owner)
        db.flush()
        for name, card_content in cards.items():
            interview = InterviewSession(owner_id=owner.id, topic_id="open-interview", context={}, status="completed")
            db.add(interview)
            db.flush()
            published = name not in drafts
            card = KnowledgeCard(
                session_id=interview.id, owner_id=owner.id, origin="human", completion_version=1,
                publication_status="published" if published else "draft",
                review_status="reviewed" if published else "unreviewed",
                consent={"content_version": 1} if published else None,
                review={"content_version": 1, "decision": "accept", "at": "2026-11-22T10:00:00Z"} if published else None,
                content=card_content, text="\n".join(fact["text"] for section in ("task", "actions")
                                                     for fact in card_content[section]))
            db.add(card)
            db.flush()
            ids[name] = card.id
    return ids


def test_card_texts_skip_the_multi_case_wrapper():
    card = {"title": "Карточка интервью", "cases": [
        content("Медленный отчёт", "Отчёт строился минуту.", "Добавил индекс."),
        {**content("Пустой случай", "", ""), "task": [], "actions": []},
        content("Флейки", "Тесты падали.", "Убрал sleep."),
    ]}
    assert embeddings.card_texts(card) == [
        (1, "Медленный отчёт\nОтчёт строился минуту.\nДобавил индекс."),
        (3, "Флейки\nТесты падали.\nУбрал sleep."),
    ]



def test_weak_search_hits_need_a_shared_word():
    assert embeddings.search_passes(0.56, 0.0)
    assert embeddings.search_passes(0.52, 0.25)
    assert not embeddings.search_passes(0.52, 0.0)  # an off-topic query near the line finds nothing
    assert not embeddings.search_passes(0.49, 1.0)

def test_embed_reads_openai_format_in_input_order(monkeypatch):
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        if request.headers.get("authorization") != "Bearer key":
            return httpx.Response(500)
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 4]}]})

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **options: client(transport=httpx.MockTransport(handler), **options))
    settings = Settings(_env_file=None, embedding_base_url="http://embeddings.test/v1/", embedding_model="m",
                        embedding_api_key="key")
    assert embeddings.embed(settings, ["a", "b"]) == [[0.6, 0.8], [0.0, 1.0]]
    assert seen == [("http://embeddings.test/v1/embeddings", {"model": "m", "input": ["a", "b"]})]
    with pytest.raises(RuntimeError, match="EMBEDDING_HTTP_ERROR: 500"):
        embeddings.embed(settings.model_copy(update={"embedding_api_key": ""}), ["a"])
    with pytest.raises(RuntimeError, match="EMBEDDING_NOT_CONFIGURED"):
        embeddings.embed(Settings(_env_file=None), ["a"])


def test_published_cards_are_indexed_searched_and_linked(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(_env_file=None, database_url=database_url, hh_weekly_enabled=False,
                        documentation_weekly_enabled=False, embedding_base_url="http://embeddings.test/v1",
                        embedding_model="fake")
    calls = []

    def fake_embed(_, texts):
        calls.append(list(texts))
        return [fake_vector(text) for text in texts]

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    monkeypatch.setattr(vectors, "embed", lambda _, texts, timeout=None: [fake_vector(text) for text in texts])
    engine, sessions = database(database_url)
    cards = {
        "report": content("Медленный отчёт", "Отчёт по заказам строился 40 секунд.", "Добавил индекс в PostgreSQL."),
        "query": content("Долгий запрос", "Запрос к таблице событий шёл минуту.", "Переписал запрос."),
        "flaky": content("Нестабильные тесты", "Каждый пятый прогон падал.", "Заменил sleep на ожидания в Playwright."),
        "draft": content("Черновик про отчёт", "Отчёт тормозил.", "Добавил индекс."),
    }
    ids = publish(sessions, cards, drafts={"draft"})

    assert embeddings.index_cards(sessions, settings) == 3
    assert embeddings.index_cards(sessions, settings) == 0  # unchanged text and model are not embedded again
    with sessions.begin() as db:
        db.get(KnowledgeCard, ids["query"]).content = content("Долгий запрос", "Запрос шёл минуту.", "Добавил индекс.")
    calls.clear()
    assert embeddings.index_cards(sessions, settings) == 1
    assert calls == [["Долгий запрос\nЗапрос шёл минуту.\nДобавил индекс."]]
    with sessions() as db:
        assert {row.card_id for row in db.scalars(select(CardEmbedding))} == {ids["report"], ids["query"], ids["flaky"]}
    engine.dispose()

    with TestClient(create_app(settings)) as client:
        found = client.get("/api/v1/catalog/search", params={"q": "медленный запрос в отчёте"}).json()
        assert found["mode"] == "meaning"
        assert [item["id"] for item in found["items"]][:2] in ([ids["report"], ids["query"]], [ids["query"], ids["report"]])
        assert ids["draft"] not in {item["id"] for item in found["items"]}
        assert ids["flaky"] not in {item["id"] for item in found["items"]}
        report = next(item for item in found["items"] if item["id"] == ids["report"])
        assert report["match"] == {"title": "Медленный отчёт", "task": "Отчёт по заказам строился 40 секунд.",
                                   "skills": ["postgresql"]}

        def search(**params):
            return client.get("/api/v1/catalog/search", params={"q": "медленный запрос в отчёте", **params})

        # Only the report card names a database skill; the edited query card no longer does.
        assert [item["id"] for item in search(category="databases").json()["items"]] == [ids["report"]]
        assert search(category="testing").json()["items"] == []
        assert search(category="nonsense").status_code == 422

        similar = client.get(f"/api/v1/catalog/{ids['report']}/similar").json()["items"]
        assert [item["id"] for item in similar] == [ids["query"]]
        assert client.get(f"/api/v1/catalog/{ids['draft']}/similar").status_code == 404

        def unavailable(*_):
            raise RuntimeError("EMBEDDING_UNAVAILABLE")

        monkeypatch.setattr(embeddings, "embed", unavailable)
        words = client.get("/api/v1/catalog/search", params={"q": "Playwright прогон"}).json()
        assert words["mode"] == "words" and [item["id"] for item in words["items"]] == [ids["flaky"]]


def test_vacancy_duty_matches_by_meaning_without_shared_words():
    duty = "Ускорять медленные SQL-запросы."
    cards = prepare_cards([vacancy("201", description=f"<h3>Обязанности</h3><p>{duty}</p>", key_skills=[])],
                          "python_backend")
    collection = VacancyCollection(cards=cards, updated_at=now(), profession_id="python_backend", aliases=None,
                                   approved=True, stats={}, config_version=1)
    db = SimpleNamespace(get=lambda *_: collection, scalars=lambda query: [
        collection] if query.column_descriptions[0]["entity"] is VacancyCollection else [])
    state = {"facts": [{"field": "task", "text": "Пользователи ждали выгрузку продаж целую вечность."}]}

    def hints(score):
        case = SimpleNamespace(scores=lambda _, texts: {text: score for text in texts})
        return sources_for_question(db, {"role": "Python backend"}, state, "decision", case_vector=case)

    assert sources_for_question(db, {"role": "Python backend"}, state, "decision") == []
    assert hints(0.5) == []
    assert [hint["task_excerpt"] for hint in hints(0.7)] == [duty]


def test_published_case_of_the_same_problem_matches_by_meaning(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine, sessions = database(database_url)
    grounded = {"source": "interview", "supporting_message_ids": ["answer"]}
    action = {"text": "Я настроил Playwright для проверки страницы.", "responsibility": "personal", **grounded}
    with sessions.begin() as db:
        users = [User(email=f"case-{index}@example.test", password_hash="unused") for index in range(2)]
        db.add_all(users)
        db.flush()
        author = InterviewSession(owner_id=users[1].id, topic_id="open-interview", context={"role": "QA Engineer"},
                                  status="completed")
        current = InterviewSession(owner_id=users[0].id, topic_id="open-interview", context={"role": "QA Engineer"})
        db.add_all([author, current])
        db.flush()
        card = KnowledgeCard(
            session_id=author.id, owner_id=author.owner_id, origin="human", completion_version=1,
            catalog_profession_id="qa", publication_status="published", review_status="reviewed",
            consent={"content_version": 1}, review={"content_version": 1, "decision": "accept"},
            content={"title": "Нестабильный CI", "task": [{"text": "Сценарии оформления заказа валились в CI.", **grounded}],
                     "actions": [action], "unknown_fields": ["verification"]},
            text="unused")
        db.add(card)
        db.flush()
        card_id, current_id = card.id, current.id
    state = {"facts": [{"field": "task", "id": "task", "text": "Тесты корзины то проходили, то падали.",
                        "supporting_message_ids": ["mine"]},
                       {"field": "action", "id": "action", "text": action["text"], "responsibility": "personal",
                        "supporting_message_ids": ["mine"]}]}
    with sessions() as db:
        interview = db.get(InterviewSession, current_id)

        def context(score):
            case = SimpleNamespace(case_scores=lambda _, ids: {(card_id, 0): score} if card_id in ids else {})
            return published_context(db, interview, state, "verification", case)

        assert published_context(db, interview, state, "verification") == ({}, {})
        assert context(0.5) == ({}, {})
        gaps, matches = context(0.7)
        assert gaps["shared_skills"] == ["playwright"] and list(matches) == [card_id]
    engine.dispose()


def test_text_vectors_are_cached_by_content(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(_env_file=None, database_url=database_url, embedding_base_url="http://embeddings.test/v1",
                        embedding_model="fake")
    calls = []

    def fake_embed(_, texts, timeout=None):
        calls.append(list(texts))
        return [fake_vector(text) for text in texts]

    monkeypatch.setattr(vectors, "embed", fake_embed)
    engine, sessions = database(database_url)
    assert vectors.cache_texts(sessions, settings, ["Оптимизировать запросы", "Писать тесты", "Писать тесты"]) == 2
    assert vectors.cache_texts(sessions, settings, ["Писать тесты", "Выпускать сертификаты"]) == 1
    assert calls == [["Оптимизировать запросы", "Писать тесты"], ["Выпускать сертификаты"]]
    with sessions() as db:
        case = vectors.CaseVector("fake", fake_vector("медленный запрос"))
        scores = case.scores(db, ["Оптимизировать запросы", "Писать тесты", "нет вектора"])
        assert set(scores) == {"Оптимизировать запросы", "Писать тесты"}
        assert scores["Оптимизировать запросы"] > 0.9 > scores["Писать тесты"]
    engine.dispose()


def test_themes_group_close_cases_and_correlate_with_categories(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(_env_file=None, database_url=database_url, hh_weekly_enabled=False,
                        documentation_weekly_enabled=False, embedding_base_url="http://embeddings.test/v1",
                        embedding_model="fake")
    fake = lambda _, texts, timeout=None: [fake_vector(text) for text in texts]  # noqa: E731
    monkeypatch.setattr(embeddings, "embed", fake)
    monkeypatch.setattr(vectors, "embed", fake)
    engine, sessions = database(database_url)
    ids = publish(sessions, {
        "report": content("Медленный отчёт PostgreSQL", "Отчёт строился 40 секунд.", "Добавил индекс."),
        "query": content("Долгий запрос", "Запрос к PostgreSQL шёл минуту.", "Переписал запрос."),
        "flaky": content("Нестабильные тесты", "Каждый пятый прогон падал.", "Заменил sleep на ожидания в Playwright."),
        "regress": content("Долгий регресс", "Ручной прогон шёл три дня.", "Автоматизировал тесты на pytest."),
        "tls": content("Истёк сертификат", "Сайт стал недоступен.", "Настроил автопродление TLS."),
    })
    assert embeddings.index_cards(sessions, settings) == 5
    engine.dispose()
    with TestClient(create_app(settings)) as client:
        page = client.get("/api/v1/catalog/themes").json()
        assert page["cases"] == 5 and page["unthemed"] == 1
        themes = {frozenset(card["id"] for card in theme["cards"]): theme for theme in page["themes"]}
        assert set(themes) == {frozenset({ids["report"], ids["query"]}), frozenset({ids["flaky"], ids["regress"]})}
        reports = themes[frozenset({ids["report"], ids["query"]})]
        assert reports["skills"] == [{"skill": "postgresql", "cases": 2}]
        cell = dict(zip(page["categories"], reports["matrix"]))
        # Both report cases name PostgreSQL and no other case does: a perfect correlation.
        assert cell["databases"] == {"phi": 1.0, "cases": 2}
        assert cell["testing"]["phi"] < 0
        for outside in (0.1, 0.99):
            assert client.get("/api/v1/catalog/themes", params={"threshold": outside}).status_code == 422


def test_search_query_can_use_the_hnsw_index(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "0029")
    engine, sessions = database(database_url)
    try:
        with sessions.begin() as db:
            if not vectors.pgvector_column(db):
                pytest.skip("the index exists only on PostgreSQL with pgvector")
            # Reproduce the index definition already deployed before 0030.
            db.execute(text("drop index card_embeddings_bge_m3_hnsw"))
            db.execute(text("create index card_embeddings_bge_m3_hnsw on card_embeddings "
                            "using hnsw ((embedding::vector(1024)) vector_cosine_ops) where model = 'bge-m3'"))
        command.upgrade(config, "head")
        with sessions() as db:
            definition = db.scalar(text("select pg_get_indexdef('card_embeddings_bge_m3_hnsw'::regclass)"))
            assert "vector_dims(embedding) = 1024" in definition
            statement = embeddings.nearest_cases_statement(
                "bge-m3", [0.01] * 1024, published_cards().with_only_columns(KnowledgeCard.id), 50)
            compiled = statement.compile(dialect=db.get_bind().dialect, compile_kwargs={"render_postcompile": True})
            # A few rows make a scan cheaper than the index; forbid the scan to see that the index fits the query.
            db.execute(text("set local enable_seqscan = off"))
            plan = "\n".join(row[0] for row in db.connection().exec_driver_sql("explain " + compiled.string, compiled.params))
    finally:
        engine.dispose()
    assert "Index Scan using card_embeddings_bge_m3_hnsw" in plan


def test_vector_search_and_themes_handle_mixed_dimensions(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine, sessions = database(database_url)
    try:
        names = ("match", "related", "different size", "alternative")
        ids = publish(sessions, {name: content(name, "Задача", "Действие") for name in names})
        primary = [1.0] + [0.0] * 1023
        alternative = [1.0] + [0.0] * 767
        with sessions.begin() as db:
            for name, model, vector in zip(names, ("bge-m3", "bge-m3", "bge-m3", "nomic-embed-text"),
                                           (primary, primary, alternative, alternative)):
                db.add(CardEmbedding(card_id=ids[name], case_index=0, model=model, text_hash="hash",
                                     embedding=vectors.stored_vector(db, vector)))
        with sessions() as db:
            candidates = published_cards().with_only_columns(KnowledgeCard.id)
            hits = embeddings.nearest_cases(db, "bge-m3", primary, candidates, 10)
            assert {(card_id, score) for card_id, _, score in hits} == {
                (ids["match"], 1.0), (ids["related"], 1.0),
            }
            assert embeddings.nearest_cases(db, "nomic-embed-text", alternative, candidates, 10) == [
                (ids["alternative"], 0, 1.0),
            ]
            neighbours = close_pairs(db, "bge-m3", candidates, 0.64)
            assert neighbours[(ids["different size"], 0)] == {}
            assert neighbours[(ids["match"], 0)] == {(ids["related"], 0): 1.0}
    finally:
        engine.dispose()
