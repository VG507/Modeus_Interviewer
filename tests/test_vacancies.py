import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import interview_engine, vacancy_import
from backend.app import create_app
from backend.db import Message, User, VacancyCollection, VacancySnapshot, database, now
from backend.security import hash_password
from backend.settings import Settings
from backend.source_context import import_excerpts, sources_for_followup
from backend.vacancy_context import sources_for_question
from backend.vacancy_import import HeadHunter, claim_collection, prepare_cards, run_collection
from tests.test_interview_flow import wait_for


def vacancy(vacancy_id="123", **overrides):
    return {
        "id": vacancy_id, "name": "Middle Python backend-разработчик",
        "description": "<h3>Обязанности</h3><ul><li>Анализировать план выполнения SQL-запросов и оптимизировать индексы PostgreSQL.</li></ul>"
                       "<h3>Требования</h3><p>Опыт с Python и Docker.</p><script>Redis</script>",
        "key_skills": [{"name": "Postgres"}, {"name": "PostgreSQL"}],
        "experience": {"id": "between1And3"}, "employer": {"id": "5"}, **overrides,
    }


def test_hh_pagination_details_and_normalization(monkeypatch):
    requests = []

    def hh_api(request):
        requests.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path == "/vacancies":
            assert request.url.params.get_list("area") == ["113", "1"]
            assert request.url.params["period"] == "30"
            items = [{"id": "123", "snippet": {"requirement": "<highlighttext>Celery</highlighttext>"}}]
            if request.url.params["page"] == "1":
                items.append({"id": "456"})
            return httpx.Response(200, json={"items": items, "pages": 2})
        if request.url.path == "/vacancies/123":
            return httpx.Response(200, json=vacancy())
        return httpx.Response(200, json=vacancy("456", name="Преподаватель Python"))

    monkeypatch.setattr(vacancy_import, "wait_for_request_window", lambda sessions, deadline: None)
    with httpx.Client(base_url="https://api.hh.ru", transport=httpx.MockTransport(hh_api),
                      headers={"Authorization": "Bearer test-token"}) as client:
        cards = prepare_cards(HeadHunter(client, None).vacancies("python_backend", ["113", "1"]), "python_backend")
    assert requests == ["/vacancies", "/vacancies/123", "/vacancies", "/vacancies/456"]
    assert len(cards) == 1
    card = cards[0]
    assert set(card["skills"]) == {"python", "sql", "postgresql", "docker", "celery"}
    assert card["level"] == "middle" and card["experience_band"] == "between1And3"
    assert card["tasks"][0].endswith("индексы PostgreSQL.")
    assert {item["source_field"] for item in card["evidence"]} >= {"key_skills[0].name", "description", "snippet.requirement"}


@pytest.mark.parametrize(("path", "status", "code"), [
    ("/token", 403, "HH_TOKEN_FAILED"), ("/vacancies", 403, "HH_AUTH_FAILED"),
    ("/vacancies", 429, "HH_RATE_LIMITED"), ("/vacancies", 503, "HH_UNAVAILABLE"),
])
def test_hh_request_reports_distinct_failures(monkeypatch, path, status, code):
    monkeypatch.setattr(vacancy_import, "wait_for_request_window", lambda *_: None)
    monkeypatch.setattr(vacancy_import, "sleep", lambda _: None)
    transport = httpx.MockTransport(lambda _: httpx.Response(status))
    with httpx.Client(base_url="https://api.hh.ru", transport=transport) as client:
        with pytest.raises(vacancy_import.ImportFailure, match=code):
            HeadHunter(client, None).request("GET", path)


def test_hh_request_interval_is_shared_between_collectors(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'interval.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    first_engine, first = database(url)
    second_engine, second = database(url)
    elapsed = 0

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(vacancy_import, "now", lambda: datetime(2026, 11, 20) + timedelta(seconds=elapsed))
    monkeypatch.setattr(vacancy_import, "monotonic", lambda: elapsed)
    monkeypatch.setattr(vacancy_import, "sleep", advance)
    try:
        for sessions, expected_ms in ((first, 0), (second, 334), (first, 668)):
            vacancy_import.wait_for_request_window(sessions, deadline=60)
            assert round(elapsed * 1000) == expected_ms
    finally:
        first_engine.dispose()
        second_engine.dispose()


def test_parser_prefers_complete_tasks_and_keeps_alias_evidence():
    records = prepare_cards([vacancy(
        description="<h3>Обязанности</h3><li>Проектировать и сопровождать API для заполнения заявок.</li>"
                    "<li>— Проектировать и сопровождать API для заполнения заявок!</li>"
                    "<h3>Требования</h3><p>Работать с Django REST Framework.</p>",
        key_skills=[{"name": "Django"}, {"name": "Django Framework"}, {"name": "DRF"}],
        snippet={"responsibility": "Проектировать и сопровождать API…"},
    )], "python_backend")
    card = records[0]
    assert card["skills"] == ["django", "django rest framework"]
    assert {item["quote"] for item in card["evidence"] if item["field_path"] == "skills.django"} == {
        "Django", "Django Framework",
    }
    assert card["tasks"] == ["Проектировать и сопровождать API для заполнения заявок."]
    assert next(item for item in card["evidence"] if item["field_path"] == "tasks.0")["source_field"] == "description"


def test_sections_separate_tasks_requirements_and_conditions_with_exact_quotes():
    task = "Оптимизировать время загрузки страниц."
    requirement = "Знать React и TypeScript."
    condition = "Удалённая работа."
    card = prepare_cards([vacancy(name="Frontend React", description=(
        f"<p><b>Задачи:</b> {task}</p><li>Опыт работы с React от трёх лет.</li>"
        f"<p><b>Требования:</b> {requirement}</p><p><b>Условия работы:</b> {condition}</p>"
        "<h3>О компании</h3><p>Мы создаём продукты для образования.</p>"
    ), snippet={"responsibility": "Оптимизировать время загрузки…", "requirement": "Знать React и TypeScript"})], "frontend")[0]
    assert card["tasks"] == [task]
    assert card["requirements"] == ["Опыт работы с React от трёх лет.", requirement]
    assert card["conditions"] == [condition]
    quotes = {item["field_path"]: item for item in card["evidence"]}
    for field, quote in (("tasks.0", task), ("requirements.1", requirement), ("conditions.0", condition)):
        assert quotes[field]["quote"] == quote and quote in card["description"]
        assert quotes[field]["url"] == card["url"]


def test_task_relevance_beats_stack_overlap():
    noise = vacancy("124", description="<h3>Обязанности</h3><p>Настраивать развёртывание сервисов.</p>"
                                         "<h3>Требования</h3><p>Python, PostgreSQL, Docker, Redis, FastAPI, SQL.</p>")
    relevant = vacancy("125", description="<h3>Обязанности</h3><p>Разбирать планы выполнения запросов.</p>", key_skills=[])
    requirements_only = vacancy("126", description="<h3>Требования</h3><p>Оптимизировать запросы PostgreSQL.</p>",
                                key_skills=[{"name": "PostgreSQL"}])
    unverified = vacancy("127", description="<h3>Обязанности</h3><p>Оптимизировать отчёты PostgreSQL.</p>")
    cards = prepare_cards([noise, relevant, requirements_only, unverified], "python_backend")
    cards[-1]["evidence"] = []
    collection = SimpleNamespace(cards=cards, updated_at=now(), profession_id="python_backend", aliases=None)
    db = SimpleNamespace(get=lambda *_: collection, scalars=lambda *_: [collection])
    state = {"facts": [{"field": "task", "text": "Медленный отчёт в Python, PostgreSQL, Docker, Redis и FastAPI."}]}
    result = sources_for_question(db, {"role": "Python backend"}, state, "decision")
    assert [hint["vacancy_id"] for hint in result] == ["125"]
    assert result[0]["task_excerpt"] == "Разбирать планы выполнения запросов."
    assert result[0]["source_field"] == "description"


def test_reviewed_sources_require_applicability_and_rank_by_purpose(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'sources.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine, sessions = database(url)
    quote = "Проверяли план выполнения и считали медиану на одинаковых данных."
    base = {
        "profession_id": "python_backend", "title": "Первичный источник", "url": "https://example.org/source",
        "text": quote, "quote": quote, "task": "Медленный запрос отчёта PostgreSQL", "technology": "PostgreSQL",
    }
    docs = {**base, "id": "docs", "kind": "documentation", "purpose": "behavior", "detail": "план выполнения",
            "version": "16"}
    research = {**base, "id": "research", "kind": "research", "purpose": "effect", "detail": "считали медиану",
                "conditions": ["на одинаковых данных"], "method": "считали медиану на одинаковых данных"}
    engineering = {**base, "id": "engineering", "kind": "engineering", "purpose": "implementation",
                   "detail": "план выполнения"}
    intake = {"role": "Python backend"}
    state = {"facts": [{"field": "task", "text": "Ускоряли медленный запрос отчёта PostgreSQL 16.2."}]}

    with sessions.begin() as db:
        with pytest.raises(ValueError, match="quote must occur"):
            import_excerpts(db, [docs, {**docs, "id": "invalid", "quote": "Вымышленная цитата"}])
        assert sources_for_followup(db, intake, state, "context") == []
        with pytest.raises(ValueError, match="measurement method"):
            import_excerpts(db, [{**research, "method": None}])
        assert import_excerpts(db, [engineering, docs, research, {**docs, "id": "wrong-version", "version": "16.1"}]) == 4
        assert import_excerpts(db, [docs]) == 1
        assert [x["excerpt_id"] for x in sources_for_followup(db, intake, state, "context")] == ["docs"]
        assert [x["excerpt_id"] for x in sources_for_followup(db, intake, state, "action")] == ["engineering"]
        assert [x["excerpt_id"] for x in sources_for_followup(db, intake, state, "verification", "На одинаковых данных.")] == [
            "research", "docs",
        ]
        assert [x["excerpt_id"] for x in sources_for_followup(db, intake, state, "verification", "Не на одинаковых данных.")] == [
            "docs",
        ]
        assert sources_for_followup(db, intake, state, "context", "Мы уже проверяли план выполнения.") == []
        other_case = {"facts": [{"field": "task", "text": "Настраивали доставку почтовых уведомлений в PostgreSQL 16.2."}]}
        assert sources_for_followup(db, intake, other_case, "context") == []
    engine.dispose()


@pytest.mark.parametrize(("text", "number", "expected"), [
    ("Как вы проверили результат?", 1, 1),
    ("Как вы проверили результат?", 7, None),
    ("Как изменили план? Что проверили?", 1, None),
])
def test_question_trace_requires_a_valid_model_reference(monkeypatch, text, number, expected):
    async def model(settings, schema, instructions, payload):
        assert schema is interview_engine.SourcedQuestion
        assert payload["market_context"][0]["number"] == 1
        return schema(text=text, source_number=number)

    monkeypatch.setattr(interview_engine, "ask_model", model)
    result = asyncio.run(interview_engine.formulate_question(None, "SQL", "verification", {"text": "Добавил индекс."},
        {"facts": [], "market_context": [{"task_excerpt": "Анализировать планы запросов.", "skills": ["sql"]}]}))
    assert getattr(result, "source_number", None) == expected
    assert result.text.count("?") == 1


def test_paired_question_evaluation_changes_only_the_quote(monkeypatch):
    from scripts.evaluate_vacancy_context import compare_pair

    requests = []

    async def model(settings, schema, instructions, payload):
        requests.append((schema, instructions, payload))
        return schema(text="Как проверили результат?", source_number=1 if payload["market_context"] else None)

    monkeypatch.setattr(interview_engine, "ask_model", model)
    quote = "Анализировать задержки при взаимодействии."
    results = asyncio.run(compare_pair(None, "Загрузка каталога", "verification", {"text": "Уменьшил бандл."},
        {"facts": [], "intake": {"role": "Frontend"}}, {"task_excerpt": quote, "vacancy_id": "123"},
        ("baseline", "with_vacancies")))
    (schema_a, prompt_a, input_a), (schema_b, prompt_b, input_b) = requests
    assert schema_a is schema_b is interview_engine.SourcedQuestion and prompt_a == prompt_b
    assert input_a.pop("market_context") == []
    assert input_b.pop("market_context") == [{"task_excerpt": quote, "number": 1}]
    assert input_a == input_b
    assert results["baseline"]["selected_vacancy"] is None and results["with_vacancies"]["selected_vacancy"] == "123"


def test_repeat_collect_reuses_fresh_details_and_refreshes_stale_or_changed(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'hh-cache.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine, sessions = database(url)
    settings = Settings(database_url=url, hh_user_agent="Modeus test", hh_access_token="test")
    titles = {"123": "Python Backend", "124": "Python разработчик"}
    requests = []

    def hh_api(request):
        path = request.url.path
        requests.append(path)
        if path == "/vacancies":
            with sessions() as db:
                assert db.get(VacancyCollection, "python_backend").progress["stage"] == "search"
            return httpx.Response(200, json={"items": [{"id": key, "name": title} for key, title in titles.items()], "pages": 1})
        with sessions() as db:
            assert db.get(VacancyCollection, "python_backend").progress["stage"] == "details"
        key = path.rsplit("/", 1)[-1]
        if key == "125":
            return httpx.Response(403)
        return httpx.Response(200, json=vacancy(key, name=titles[key]))

    client = httpx.Client
    monkeypatch.setattr(vacancy_import.httpx, "Client", lambda **args: client(transport=httpx.MockTransport(hh_api), **args))
    monkeypatch.setattr(vacancy_import, "wait_for_request_window", lambda *_: None)

    def collect():
        requests.clear()
        return run_collection(sessions, settings, "python_backend", claim_collection(sessions, "python_backend"))

    try:
        assert collect()["details_fetched"] == 2
        with sessions() as db:
            checkpoint = db.get(VacancyCollection, "python_backend").details_cache
        assert collect()["details_reused"] == 2
        assert requests == ["/vacancies"]
        with sessions.begin() as db:
            row = db.get(VacancyCollection, "python_backend")
            assert row.details_cache == checkpoint
            old = {**checkpoint["123"], "fetched_at": (now() - timedelta(hours=25)).isoformat()}
            row.details_cache = {**checkpoint, "123": old}
        titles["124"] = "Senior Python разработчик"
        assert collect()["details_fetched"] == 2
        assert requests == ["/vacancies", "/vacancies/123", "/vacancies/124"]
        titles["125"] = "Новая Python вакансия"
        assert collect()["error"] == "HH_AUTH_FAILED"
        with sessions() as db:
            row = db.get(VacancyCollection, "python_backend")
            assert row.stats["vacancies"] == 2 and set(row.details_cache) == {"123", "124"}
    finally:
        engine.dispose()


@pytest.mark.parametrize("use_documentation", [False, True])
def test_latest_vacancy_sample_reaches_question_but_not_card(tmp_path, monkeypatch, use_documentation):
    url = f"sqlite:///{(tmp_path / 'market.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    origin = "http://localhost:5173"
    settings = Settings(database_url=url, app_origin=origin, hh_weekly_enabled=False)
    app = create_app(settings)
    sessions = app.state.sessions
    path = tmp_path / "hh.json"

    def refresh(items, result):
        path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        token = claim_collection(sessions, "python_backend")
        assert token and claim_collection(sessions, "python_backend") is None
        assert run_collection(sessions, settings, "python_backend", token, path)["status"] == result

    refresh([vacancy()], "ok")
    refresh([vacancy(), vacancy(name="Middle Python backend-разработчик (обновлено)")], "ok")
    refresh([vacancy(archived=True)], "failed")
    with sessions.begin() as db:
        collection = db.get(VacancyCollection, "python_backend")
        assert db.scalar(select(func.count()).select_from(VacancySnapshot)) == 2
        assert len(collection.cards) == 1 and "обновлено" in collection.cards[0]["title"]
        assert collection.error == "HH_EMPTY_SAMPLE"
        db.add_all([User(email=f"{role}@example.test", password_hash=hash_password("test-password-123"),
                         is_editor=role == "editor") for role in ("editor", "participant")])
        if use_documentation:
            import_excerpts(db, [{
                "id": "explain", "profession_id": "python_backend", "title": "Документация",
                "url": "https://example.org/docs", "text": "Сравнивайте план выполнения на одинаковых данных.",
                "quote": "Сравнивайте план выполнения на одинаковых данных.", "detail": "план выполнения",
                "kind": "documentation", "purpose": "behavior", "task": "Медленный запрос отчёта PostgreSQL",
                "technology": "PostgreSQL",
            }])

    task = "Отчёт PostgreSQL выполнялся 30 секунд."
    action = "Я переписал SQL-запрос."
    observed = []

    async def model(settings, schema, instructions, payload):
        if schema is interview_engine.DiscoverySelection:
            assert "market_context" not in payload
            return schema(facts=[{"field": "task", "sentence": 1}, {"field": "action", "sentence": 2}],
                          ambiguous=[], not_applicable=[], refused=False, answer_fit="work_case",
                          topic_title="Медленный отчёт PostgreSQL")
        if schema is interview_engine.SourcedQuestion:
            observed.extend(payload["market_context"])
            return schema(text="Что в плане выполнения повлияло на ваш выбор?", source_number=1)
        assert schema is interview_engine.SummaryOrder
        assert "market_context" not in payload
        return schema(fact_numbers=[1, 2])

    monkeypatch.setattr(interview_engine, "ask_model", model)
    with TestClient(app) as client:
        def login(role):
            assert client.post("/api/v1/auth/login", headers={"Origin": origin}, json={
                "email": f"{role}@example.test", "password": "test-password-123",
            }).status_code == 200

        def post(endpoint, body):
            response = client.post(endpoint, headers={"Origin": origin, "Idempotency-Key": str(uuid4())}, json=body)
            assert response.is_success, response.text
            return response.json()

        login("editor")
        overview = client.get("/api/v1/vacancies/overview").json()["profiles"]
        assert {item["id"] for item in overview} == {"python_backend", "frontend", "qa", "devops"}
        python_profile = next(item for item in overview if item["id"] == "python_backend")
        assert python_profile["error"] == "HH_EMPTY_SAMPLE" and python_profile["stats"]["vacancies"] == 1
        token = claim_collection(sessions, "python_backend")
        progress = vacancy_import.CollectionProgress(sessions, "python_backend", token)
        progress.report("details", accepted=7, details_reused=3)
        active = next(item for item in client.get("/api/v1/vacancies/overview").json()["profiles"]
                      if item["id"] == "python_backend")
        assert active["running"] and active["progress"]["accepted"] == 7
        assert active["progress"]["details_reused"] == 3 and active["stats"]["vacancies"] == 1
        assert run_collection(sessions, settings, "python_backend", token, path)["error"] == "HH_EMPTY_SAMPLE"
        assert len(client.get("/api/v1/vacancies?level=middle").json()["cards"]) == 1
        assert client.get("/api/v1/vacancies?level=senior").json()["cards"] == []
        login("participant")
        assert client.get("/api/v1/vacancies/overview").status_code == 403
        assert client.get("/api/v1/vacancies").status_code == 403
        interview = post("/api/v1/interviews", {"context": {"role": "Python backend", "experience": "2 года"}})
        endpoint = f'/api/v1/interviews/{interview["id"]}'
        opening = wait_for(client, endpoint, "idle")
        draft = post(endpoint + "/answers", {"expected_version": opening["version"],
                     "question_id": opening["current_question_id"], "text": f"{task} {action}"})
        post(f'{endpoint}/answers/{draft["answer"]["id"]}/submit', {"expected_version": draft["interview"]["version"]})
        followup = wait_for(client, endpoint, "idle")
        assert followup["messages"][-1]["text"] == "Что в плане выполнения повлияло на ваш выбор?"
        assert "план выполнения" in observed[0]["task_excerpt"]
        message = followup["messages"][-1]
        expected = ({"title": "Документация", "url": "https://example.org/docs"} if use_documentation else
                    {"title": "Middle Python backend-разработчик (обновлено)", "url": "https://hh.ru/vacancy/123"})
        assert message["source_quote"] == {**expected, "quote": observed[0]["task_excerpt"]}
        assert message["vacancy_quote"] == (None if use_documentation else message["source_quote"])
        post(endpoint + "/finish", {"expected_version": followup["version"]})
        finished = wait_for(client, endpoint, "idle")
        card = client.get(f'/api/v1/cards/{finished["card_id"]}').json()
        assert "план выполнения" not in card["text"]

    state = {"facts": [{"field": "task", "text": task}]}
    with sessions.begin() as db:
        trace = db.get(Message, followup["current_question_id"]).market_sources[0]
        assert trace["excerpt_id" if use_documentation else "vacancy_id"] == ("explain" if use_documentation else "123")
        assert not sources_for_question(db, {"role": "QA"}, state, "decision")
        assert not sources_for_question(db, {"role": "Senior Python backend"}, state, "decision")
        assert not sources_for_question(db, {"role": "Python backend", "experience": "8 лет"}, state, "decision")
        db.get(VacancyCollection, "python_backend").updated_at = now() - timedelta(days=15)
        db.flush()
        assert not sources_for_question(db, {"role": "Python backend"}, state, "decision")


def test_editor_adds_profession_from_an_interview_and_reviews_a_trial_sample(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'profession.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    origin = "http://localhost:5173"
    app = create_app(Settings(database_url=url, app_origin=origin, hh_user_agent="Modeus test", hh_access_token="test"))
    sessions = app.state.sessions
    with sessions.begin() as db:
        db.add_all([User(email=f"{role}@example.test", password_hash=hash_password("test-password-123"),
                         is_editor=role == "editor") for role in ("editor", "participant")])

    def hh_api(request):
        if request.url.path == "/vacancies":
            assert request.url.params["text"] == "NAME:(data engineer OR ETL)"
            return httpx.Response(200, json={"items": [{"id": "555"}], "pages": 1})
        return httpx.Response(200, json=vacancy("555", name="Инженер данных",
            description="<h3>Обязанности</h3><p>Восстанавливать задержанные ETL пайплайны в Airflow.</p>",
        ))

    client_factory = httpx.Client
    monkeypatch.setattr(vacancy_import.httpx, "Client", lambda **args: client_factory(transport=httpx.MockTransport(hh_api), **args))
    monkeypatch.setattr(vacancy_import, "wait_for_request_window", lambda *_: None)
    client = TestClient(app)
    headers = {"Origin": origin, "Idempotency-Key": str(uuid4())}

    def login(role):
        result = client.post("/api/v1/auth/login", headers=headers, json={
            "email": f"{role}@example.test", "password": "test-password-123",
        })
        assert result.status_code == 200

    base = "/api/v1/vacancies"
    record = {"title": "Data engineer", "aliases": ["Инженер данных"], "search_query": "NAME:(data engineer OR ETL)"}
    login("participant")
    assert client.post("/api/v1/interviews", headers=headers, json={"context": {"role": "Инженер данных"}}).status_code == 201
    assert client.post(base + "/profiles", headers=headers, json=record).status_code == 403
    assert client.get(base + "/candidates").status_code == 403
    login("editor")
    assert client.get(base + "/candidates").json() == {"candidates": [{"role": "Инженер данных", "interviews": 1}]}
    assert client.post(base + "/profiles", json=record).status_code == 403
    created = client.post(base + "/profiles", headers=headers, json=record)
    assert created.status_code == 201, created.text
    key = created.json()["id"]
    assert client.post(base + "/profiles", headers=headers, json={**record, "title": "data-engineer"}).status_code == 409
    assert client.get(base + "/candidates").json() == {"candidates": []}
    review = f"{base}/profiles/{key}/review"
    assert client.post(review, headers=headers, json={"approved": True}).status_code == 409
    assert client.post(f"{base}/{key}/collect", headers=headers).status_code == 202
    assert client.get(base, params={"profession_id": key}).json()["cards"][0]["id"] == "555"
    case = {"facts": [{"field": "task", "text": "Восстанавливали задержанный ETL пайплайн для отчётов."}]}
    role = {"role": "Инженер данных"}
    with sessions() as db:
        assert not sources_for_question(db, role, case, "decision")
    assert client.post(review, headers=headers, json={"approved": True}).status_code == 200
    with sessions() as db:
        assert sources_for_question(db, role, case, "decision")[0]["vacancy_id"] == "555"
    assert client.put(f"{base}/profiles/{key}", headers=headers,
                      json={**record, "search_query": "NAME:(ETL developer)"}).status_code == 200
    assert client.post(review, headers=headers, json={"approved": True}).status_code == 409
    with sessions() as db:
        assert not sources_for_question(db, role, case, "decision")
