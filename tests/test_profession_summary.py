import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from openpyxl import load_workbook

from backend import profession_summary
from backend.db import EscoProfile, OnetProfile, VacancyCollection, VacancySnapshot, database
from backend.interview_engine import InvalidModelOutput

SETTINGS = SimpleNamespace(embedding_base_url="", embedding_model="", llm_model="test-model")
TODAY = date(2026, 11, 22)


def vacancy(number, source, published, skills, tasks, requirements=()):
    return {"id": f"v{number}", "source": source, "url": f"https://example.org/v{number}", "title": "Python",
            "published_at": published, "skills": skills, "tasks": tasks, "requirements": list(requirements)}


def materials():
    return {
        "demo": True, "note": "Тестовые данные.",
        "profession": {"id": "python_backend", "title": "Python backend"},
        "vacancies": [
            vacancy(1, "hh", "2026-11-10", ["python", "postgresql"], ["Оптимизация медленных SQL-запросов"],
                    ["Понимание индексов и транзакций"]),
            # The same words and an alias of the same skill join the first vacancy's wording and skill.
            vacancy(2, "hh", "2026-11-12", ["Python", "Postgres"], ["оптимизация медленных SQL-запросов!"]),
            vacancy(3, "trudvsem", "2026-05-01", ["python", "docker"], ["Контейнеризация сервисов"]),
            vacancy(4, "hh", "2026-11-01", ["python", "docker"], ["Контейнеризация сервисов"]),
        ],
        "snapshots": [
            {"collected_at": "2026-08-01", "source": "hh", "vacancies": 4, "skill_counts": {"python": 4, "postgresql": 1}},
            {"collected_at": "2026-11-15", "source": "hh", "vacancies": 4,
             "skill_counts": {"python": 4, "postgresql": 2, "docker": 2}},
        ],
        "esco": {"version": "v1.2.0", "collected_at": "2026-11-01", "skills": [
            {"title": "Python (computer programming)", "title_ru": "Программирование на Python",
             "relations": ["essential"], "uri": "https://example.org/esco/1"},
        ]},
        "onet": {"version": "30.0", "collected_at": "2026-09-30",
                 "tasks": [{"id": "t1", "text": "Store, retrieve and manipulate data.", "importance": 4.0}]},
        "cards": [{"id": "c1", "reviewed_at": "2026-11-12", "content": {
            "title": "Медленный отчёт", "task": [{"text": "Отчёт строился минуту."}],
            "actions": [{"text": "Я добавил индекс.", "skills": ["postgresql"]}],
            "verification": [{"text": "Было 60 секунд, стало 3."}],
        }}],
        "sources": [{"provider": "docs", "title": "PostgreSQL: EXPLAIN", "task": "Оптимизация запросов",
                     "quote": "EXPLAIN показывает план.", "technology": "postgresql", "version": "17",
                     "retrieved_on": "2026-11-14", "url": "https://example.org/docs"}],
    }


def model_answer(build):
    async def answer(settings, schema, instructions, payload, check=None):
        def statement(fragment):
            return next(item["number"] for item in payload["statements"] if fragment in item["text"])

        def skill(name):
            return next((item["number"] for item in payload["skills"] if item["name"] == name), None)

        result = schema.model_validate(build(statement, skill))
        check(result)
        return result
    return answer


def two_competencies(statement, skill):
    return {"competencies": [
        {"title": "Оптимизация баз данных",
         "statements": [statement("Оптимизация"), statement("Store"), statement("Медленный отчёт"), statement("EXPLAIN")],
         "skills": [skill("PostgreSQL")],
         "knowledge": [{"title": "Индексы и транзакции", "statements": [statement("Понимание индексов"),
                                                                        statement("EXPLAIN")]}]},
        {"title": "Контейнеризация", "statements": [statement("Контейнеризация")],
         # A period can leave a skill in too few vacancies to be listed.
         "skills": [number for number in (skill("Docker"), skill("Python")) if number], "knowledge": []},
    ]}


def test_summary_counts_evidence_and_scores_from_data(monkeypatch, tmp_path):
    monkeypatch.setattr(profession_summary, "ask_model", model_answer(two_competencies))
    summary = asyncio.run(profession_summary.summarize(SETTINGS, materials(), TODAY))

    skills = {row["name"]: row for row in summary["skills"]}
    assert [row["name"] for row in summary["skills"]] == ["Python", "PostgreSQL", "Docker"]
    # Every vacancy and an essential ESCO skill.
    assert skills["Python"]["score"] == 90 and skills["Python"]["level"] == "Обязательно"
    assert skills["PostgreSQL"]["trend"] == pytest.approx(0.25) and skills["PostgreSQL"]["level"] == "Важно"

    database, containers = summary["rows"]
    assert database["title"] == "Оптимизация баз данных" and database["score"] > containers["score"]
    assert "Вакансии: 2 из 4 (50%) — HH 2" in database["evidence"]
    assert "Интервью: «Медленный отчёт» — «Было 60 секунд, стало 3.»" in database["evidence"]
    assert "Надёжность: высокая — 4 из 4 типов (рынок, справочники, интервью, источники)" in database["evidence"]
    assert database["evidence"].count("Документация: PostgreSQL: EXPLAIN") == 1
    assert database["knowledge"] == ["Индексы и транзакции (вакансий: 1, внешний источник)"]
    assert database["freshness"][0] == "Вакансии: последняя 12.11.2026, за 30 дней — 100%"
    assert database["freshness"][-1] == "Итог: актуально"
    assert "Вакансии: 2 из 4 (50%) — HH 1, «Работа России» 1" in containers["evidence"]
    assert [item["kind"] for item in summary["unassigned"]] == ["esco"]

    path = tmp_path / "summary.xlsx"
    profession_summary.write_workbook(summary, path)
    book = load_workbook(path)
    assert book.sheetnames == ["Сводка", "Навыки", "Доказательства", "Источники и методика"]
    assert book["Сводка"]["A2"].value.startswith("ДЕМО-ДАННЫЕ")
    assert book["Сводка"].cell(row=6, column=2).value == "Оптимизация баз данных"
    assert book["Навыки"].cell(row=4, column=2).value == "Python"
    links = {cell.hyperlink.target for row in book["Доказательства"].iter_rows() for cell in row if cell.hyperlink}
    assert {"https://example.org/v1", "https://example.org/v2", "https://example.org/docs"} <= links


def test_summary_rejects_numbers_missing_from_the_input(monkeypatch):
    monkeypatch.setattr(profession_summary, "ask_model", model_answer(lambda statement, skill: {"competencies": [
        {"title": "Выдуманная компетенция", "statements": [99], "skills": [], "knowledge": []},
    ]}))
    with pytest.raises(InvalidModelOutput):
        asyncio.run(profession_summary.summarize(SETTINGS, materials(), TODAY))


def test_gather_reads_collected_profession_data(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    engine, sessions = database(database_url)
    card = {"id": "hh-1", "url": "https://example.org/hh-1", "title": "Python", "skills": ["python"],
            "tasks": ["Разработка API"], "requirements": [], "published_at": "2026-11-10T09:00:00+0300"}
    with sessions.begin() as db:
        db.get(VacancyCollection, "python_backend").cards = [card]
        db.add(VacancySnapshot(id="s1", profession_id="python_backend", collected_at=datetime(2026, 11, 16),
                               stats={"vacancies": 1, "vacancy_skill_counts": {"python": 1}}, cards=[card]))
        db.add(EscoProfile(profession_id="python_backend", version="v1.2.0", target="software developer",
                           match="broader", collected_at=datetime(2026, 11, 10), occupation={},
                           skills=[{"uri": "http://data.europa.eu/esco/skill/1", "title": "SQL", "relations": ["optional"]}]))
        db.add(OnetProfile(profession_id="python_backend", version="30.0", occupation_code="15-1252.00",
                           occupation_title="Software Developers", collected_at=datetime(2026, 9, 30),
                           tasks=[{"id": "1", "text": "Modify software.", "type": "Core", "importance": 4.3}]))
    with sessions() as db:
        gathered = profession_summary.gather(db, "python_backend")
        with pytest.raises(LookupError):
            profession_summary.gather(db, "unknown")
    engine.dispose()

    assert gathered["vacancies"] == [{"id": "hh-1", "source": "hh", "url": "https://example.org/hh-1",
                                      "title": "Python", "published_at": "2026-11-10", "level": None,
                                      "skills": ["python"], "tasks": ["Разработка API"], "requirements": []}]
    assert gathered["snapshots"] == [{"collected_at": "2026-11-16", "source": "hh", "vacancies": 1,
                                      "skill_counts": {"python": 1}}]
    assert gathered["esco"]["skills"][0]["title_ru"] == "SQL"
    assert gathered["onet"]["tasks"][0]["importance"] == 4.3
    assert gathered["cards"] == [] and gathered["sources"] == []


def test_editor_downloads_a_report_for_a_period(database_url, monkeypatch, tmp_path):
    from io import BytesIO
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from backend.app import create_app
    from backend.db import User
    from backend.security import hash_password
    from backend.settings import Settings

    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    origin = "http://localhost:5173"
    app = create_app(Settings(database_url=database_url, app_origin=origin, llm_base_url="http://llm.test/v1",
                              llm_model="test-model"))
    with app.state.sessions.begin() as db:
        db.add_all([User(email=f"{role}@example.test", password_hash=hash_password("test-password-123"),
                         is_editor=role == "editor") for role in ("editor", "participant")])
    demo = tmp_path / "python_backend_demo.json"
    demo.write_text(__import__("json").dumps(materials(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(profession_summary, "DEMO_DIR", tmp_path)
    monkeypatch.setattr(profession_summary, "ask_model", model_answer(two_competencies))
    client = TestClient(app)
    headers = {"Origin": origin, "Idempotency-Key": str(uuid4())}

    def login(role):
        assert client.post("/api/v1/auth/login", headers=headers, json={
            "email": f"{role}@example.test", "password": "test-password-123"}).status_code == 200

    body = {"profession_id": "python_backend", "date_from": "2026-10-01", "date_to": "2026-11-30", "demo": True}
    login("participant")
    assert client.get("/api/v1/reports/professions").status_code == 403
    assert client.post("/api/v1/reports/profession", headers=headers, json=body).status_code == 403
    login("editor")
    listed = client.get("/api/v1/reports/professions").json()["professions"]
    assert {"id": "python_backend", "title": "Python backend", "vacancies": 0, "demo": True} in listed
    assert not next(item for item in listed if item["id"] == "frontend")["demo"]
    assert client.post("/api/v1/reports/profession", json=body).status_code == 403
    assert client.post("/api/v1/reports/profession", headers=headers,
                       json={**body, "date_from": "2026-12-01"}).status_code == 422
    assert client.post("/api/v1/reports/profession", headers=headers,
                       json={**body, "profession_id": "frontend"}).json()["error"]["code"] == "REPORT_DEMO_NOT_FOUND"
    # Nothing collected in the database yet.
    empty = client.post("/api/v1/reports/profession", headers=headers, json={**body, "demo": False})
    assert empty.status_code == 409 and empty.json()["error"]["code"] == "REPORT_NO_DATA"

    report = client.post("/api/v1/reports/profession", headers=headers, json=body)
    assert report.status_code == 200, report.text
    assert report.headers["content-disposition"] == \
        'attachment; filename="python_backend-demo-summary-2026-10-01_2026-11-30.xlsx"'
    sheet = load_workbook(BytesIO(report.content))["Сводка"]
    assert sheet["A3"].value.startswith("Период: 01.10.2026 – 30.11.2026 · ")
    # The May vacancy is outside the period: only the November one supports the second competency.
    evidence = [sheet.cell(row=row, column=7).value for row in (6, 7)]
    assert any("Вакансии: 1 из 3 (33%) — HH 1" in text for text in evidence)
