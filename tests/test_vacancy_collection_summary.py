from types import SimpleNamespace

import httpx
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from backend import trudvsem, vacancy_import
from backend.app import create_app
from backend.auth import current_user
from backend.db import VacancyCollection, database
from backend.settings import Settings
from backend.vacancy_parser import PROFILES, vacancy_title_candidate
from backend.vacancy_samples import vacancy_review


def test_both_sources_collect_and_report_only_selected_role(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")
    titles = {
        "python_backend": "Python Backend Developer",
        "frontend": "Frontend Developer",
        "qa": "Senior Automation QA Frontend",
        "devops": "DevOps Engineer",
        "ml_engineer": "Machine Learning Engineer",
    }
    ids = dict(enumerate(titles.values(), start=1))
    details = []
    portal_empty = False

    def upstream(request):
        if request.url.host == "api.hh.ru":
            if request.url.path == "/vacancies":
                assert request.url.params["text"] == PROFILES[role][1]
                assert request.url.params["per_page"] == "100"
                return httpx.Response(200, json={"pages": 1, "items": [
                    {"id": str(i), "name": title} for i, title in ids.items()
                ]})
            idx = int(request.url.path.split("/")[-1])
            details.append(idx)
            return httpx.Response(200, json={"id": str(idx), "name": ids[idx],
                                             "description": "Develop and test application features.",
                                             "employer": {"id": "123"}})
        assert request.url.host == "opendata.trudvsem.ru"
        assert request.url.params["text"] in trudvsem.DEFAULT_TERMS[role]
        assert request.url.params["limit"] == "100"
        records = [{"vacancy": {"id": str(i), "job-name": title, "duty": "Test application changes.",
                                 "company": {"companycode": "123"}}} for i, title in ids.items()]
        return httpx.Response(200, json={"status": "200", "results": {"vacancies": [] if portal_empty else records}})

    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(upstream), **kwargs))
    monkeypatch.setattr(vacancy_import, "wait_for_request_window", lambda *_: None)
    app = create_app(Settings(hh_access_token="test", hh_user_agent="test"))
    app.dependency_overrides[current_user] = lambda: {"id": "test", "is_editor": True}
    try:
        # No lifespan workers: all requests in this scenario originate from the editor button.
        client = TestClient(app)
        for expected_id, (role, title) in enumerate(titles.items(), start=1):
            details.clear()
            url = f"/api/v1/discovery/collection/{role}"
            assert client.post(url + "/vacancies", headers={"Origin": app.state.app_origin}).status_code == 202
            result = client.get(url).json()
            assert not result["run"]["running"]
            assert set(result["run"]["steps"]) == {"hh", "trudvsem"}
            for source in ("hh", "trudvsem"):
                assert result["run"]["steps"][source] == {"status": "ok", "collected": 1, "employers": 1}
                assert result["quality"][source]["vacancies"] == 1
            assert details == [expected_id]
            params = {"profession_id": role}
            profiles = client.get("/api/v1/vacancies/overview", params=params).json()["profiles"]
            assert [p["id"] for p in profiles] == [role]
            assert profiles[0]["stats"]["review"]["status"] == "pending"
            assert profiles[0]["trudvsem"]["review"]["status"] == "pending"
            listings = client.get("/api/v1/vacancies", params=params).json()
            assert listings["total"] == 2
            assert {(c["source"], c["title"]) for c in listings["cards"]} == {("hh", title), ("trudvsem", title)}

        portal_empty = True
        details.clear()
        assert client.post(url + "/vacancies", headers={"Origin": app.state.app_origin}).status_code == 202
        latest = client.get(url).json()
        assert latest["run"]["steps"]["trudvsem"]["code"] == "TRUDVSEM_EMPTY_SAMPLE"
        assert latest["quality"]["trudvsem"]["checked_at"] == result["quality"]["trudvsem"]["checked_at"]
        assert latest["run"]["steps"]["hh"]["status"] == "ok" and not details
        assert client.get("/api/v1/vacancies", params=params).json()["total"] == 2
        response = client.post(f"/api/v1/vacancies/profiles/{role}/review",
                               headers={"Origin": app.state.app_origin}, json={"approved": True})
        assert response.status_code == 200
        reviewed = client.get("/api/v1/vacancies/overview", params=params).json()["profiles"][0]
        assert reviewed["stats"]["review"]["status"] == reviewed["trudvsem"]["review"]["status"] == "manual"
    finally:
        app.state.engine.dispose()


def test_both_sources_stop_at_maximum_vacancy_sample(monkeypatch):
    profile = SimpleNamespace(profession_id="qa", title="QA", aliases=None)
    hh_records = [{"id": str(index), "name": "QA Engineer", "description": "Test API changes."}
                  for index in range(1, 506)]
    portal_records = [{"vacancy": {"id": record["id"], "job-name": record["name"],
                                   "duty": record["description"], "company": {"companycode": "123"}}}
                      for record in hh_records]
    def fetch_page(_, term, page, *, deadline):
        term_index = trudvsem.DEFAULT_TERMS["qa"].index(term)
        start = 200 * term_index + 100 * page
        return portal_records[start:start + 100]

    monkeypatch.setattr(trudvsem, "fetch_page", fetch_page)
    limit = Settings(_env_file=None).vacancy_sample_limit
    assert limit == 500
    assert len(vacancy_import.prepare_cards(hh_records, "qa", profile=profile, limit=limit)) == 500
    assert len(trudvsem.gather_cards(None, profile, limit=limit)) == 500


def test_vacancy_automatic_review_requires_grounded_tasks_and_distinct_employers():
    cards = [{"employer_id": str(index % 3), "tasks": ["Test an API contract."],
              "evidence": [{"field_path": "tasks.0", "quote": "Test an API contract."}]} for index in range(5)]
    assert vacancy_review(cards, True)["status"] == "automatic"
    assert vacancy_review(cards, False)["status"] == "pending"
    for card in cards:
        card["employer_id"] = "same"
    assert vacancy_review(cards, True)["status"] == "pending"


def test_existing_ml_profile_keeps_identity_and_custom_aliases(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "0027")
    engine, sessions = database(database_url)
    try:
        with sessions.begin() as db:
            db.add(VacancyCollection(profession_id="existing", title="ML инженер", title_key="ml инженер",
                                     aliases=[], search_query="ML инженер"))
        command.upgrade(config, "head")
        with sessions() as db:
            profile = db.get(VacancyCollection, "existing")
            assert db.get(VacancyCollection, "ml_engineer") is None
            assert vacancy_title_candidate("existing", "Machine Learning Engineer", profile)
            assert "ML engineer" in trudvsem.search_terms(profile)
            profile.aliases = ["Computer Vision Engineer"]
            assert vacancy_title_candidate("existing", "Computer Vision Engineer", profile)
            assert "Computer Vision Engineer" in trudvsem.search_terms(profile)
    finally:
        engine.dispose()
