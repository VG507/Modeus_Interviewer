import asyncio
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import discovery, documentation, github_evidence
from backend import discovery_api
from backend.app import create_app
from backend.auth import current_user
from backend.db import DocumentationRevision, DiscoveryCandidate, DiscoveryItem, RepositoryObservation, SourceExcerpt, database, now
from backend.discovery_fetch import (
    Collected, CollectionError, ProviderRateLimit, fetch, github_issues, repository_trends,
)
from backend.settings import Settings
from backend.source_context import sources_for_followup
from backend.documentation_fetch import TARGETS, release_notes


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'discovery.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ORIGIN", "http://localhost:5173")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("DOCUMENTATION_WEEKLY_ENABLED", "false")
    command.upgrade(Config("alembic.ini"), "head")
    engine, result = database(url)
    yield result
    engine.dispose()


def issue_entry(**fields):
    return {
        "id": 123, "number": 4, "title": "Slow report", "body": "Slow report used a sequential scan.",
        "state": "closed", "state_reason": "completed", "labels": [{"name": "Type: Performance"}],
        "created_at": "2026-01-01T00:00:00Z", "closed_at": "2026-06-01T00:00:00Z", "comments": 4,
        **fields,
    }


def test_issues_pages_comments_filters_and_reopened():
    seen = []

    def upstream(request):
        path = request.url.path
        page = request.url.params.get("page")
        seen.append((path, page))
        assert request.url.host == "api.github.com"
        if path == "/repos/sample/project":
            return httpx.Response(200, json={"private": False})
        if path.endswith("/issues/4"):
            return httpx.Response(200, json=issue_entry(state="open", closed_at=None))
        if path.endswith("/comments"):
            useful = {"id": 20, "body": "The fix shipped in 2.0. https://github.com/sample/project/pull/44",
                      "author_association": "MEMBER"}
            return httpx.Response(200, json=[useful] if page == "2" else [])
        if path.endswith("/pulls/44"):
            return httpx.Response(200, json={"merged_at": "2026-05-01T00:00:00Z", "body": "Fixes #4\nUse a cached plan."})
        if page == "1":
            return httpx.Response(200, json=[issue_entry(pull_request={})],
                                  headers={"Link": '<https://example.org/>; rel="next"'})
        return httpx.Response(200, json=[
            issue_entry(), issue_entry(), issue_entry(id=124, title="CVE-2025-1234"),
            issue_entry(id=125, state_reason="duplicate"), issue_entry(id=126, labels=[{"name": "question"}]),
        ])

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        items, partial = github_issues(client, ["sample/project"], 2, 10, datetime(2025, 6, 1), {"performance"}, [])
        assert not partial and [item.id for item in items] == ["github:123"]
        assert items[0].payload["fragments"]["comment:20"]["url"].endswith("#issuecomment-20")
        assert items[0].payload["fragments"]["pull:44"]["merged_fix"]
        assert len([path for path, _ in seen if path.endswith("/comments")]) == 2
        updated, _ = github_issues(client, ["sample/project"], 2, 10, datetime(2025, 6, 1), {"performance"}, items)
        assert updated[0].payload == {"reason": "Issue reopened or expired"}


def test_github_token_never_follows_redirect_off_origin():
    seen = []

    def upstream(request):
        seen.append(request.url.host)
        return httpx.Response(301, headers={"location": "https://another.example/repos/sample/project"})

    with httpx.Client(transport=httpx.MockTransport(upstream), headers={"Authorization": "Bearer fixture-token"}) as client:
        with pytest.raises(CollectionError, match="outside its API"):
            fetch(client, "https://api.github.com/repos/sample/project")
    assert seen == ["api.github.com"]


def test_discovery_view_is_editor_only_and_filters_before_pagination(sessions):
    app = create_app()
    actor = {"id": "actor", "is_editor": False}
    app.dependency_overrides[current_user] = lambda: actor
    url = "https://arxiv.org/abs/2606.01234"
    paper = Collected("arxiv:2606.01234", "Paper", url, {
        "version": 1, "fragments": {"abstract": {"text": "An abstract", "url": url}},
    })
    issue = Collected("github:1", "Issue", "https://github.com/example/project/issues/1", {})
    trend = Collected("repo:77", "Repo", "https://github.com/example/project", {"stars": 17})
    with sessions.begin() as db:
        discovery.collect_records(db, "arxiv", "python_backend", [paper], now())
        discovery.collect_records(db, "github", "python_backend", [issue], now())
        discovery.collect_records(db, "trends", "python_backend", [trend], now())
    with TestClient(app) as client:
        endpoint = "/api/v1/discovery"
        assert client.get(f"{endpoint}/profiles").status_code == 403
        assert client.get(f"{endpoint}?profession_id=python_backend").status_code == 403
        assert client.get(f"{endpoint}/items/{paper.id}?profession_id=python_backend").status_code == 403
        actor["is_editor"] = True
        assert "python_backend" in {p["id"] for p in client.get(f"{endpoint}/profiles").json()}
        listed = client.get(f"{endpoint}?profession_id=python_backend&provider=github&limit=1").json()
        assert listed["next_offset"] is None
        assert [(item["id"], item["status"]) for item in listed["items"]] == [(issue.id, "automatic_check")]
        all_items = client.get(endpoint, params={"profession_id": "python_backend"}).json()["items"]
        assert {item["id"] for item in all_items} == {issue.id, trend.id}
        assert client.get(f"{endpoint}?profession_id=python_backend&limit=1").json()["next_offset"] == 1
        assert client.get(f"{endpoint}?profession_id=python_backend&provider=arxiv").status_code == 422
        assert client.get(f"{endpoint}/items/{paper.id}?profession_id=python_backend").status_code == 404
        assert client.get(f"{endpoint}/items/{issue.id}?profession_id=qa").status_code == 404


def test_editor_starts_collection_with_manual_terms_and_sees_rate_limit(sessions, monkeypatch):
    calls = []

    def collect(_, settings, provider, profession, values, pages, limit, min_stars, labels, *, interactive):
        assert interactive
        calls.append((profession, values, labels, limit))
        if provider == "github":
            raise ProviderRateLimit("rate limited", 90)
        return {"collected": 2, "partial": False}

    monkeypatch.setattr(discovery_api, "collect", collect)
    app = create_app()
    actor = {"id": "actor", "is_editor": False}
    app.dependency_overrides[current_user] = lambda: actor
    path = "/api/v1/discovery/collect"
    headers = {"Origin": app.state.app_origin}
    payload = {"profession_id": "frontend", "provider": "trends", "values": ["react", "vite", "react"], "limit": 7}
    with TestClient(app) as client:
        assert client.post(path, headers=headers, json=payload).status_code == 403
        actor["is_editor"] = True
        assert client.post(path, json=payload).status_code == 403
        assert client.post(path, headers=headers, json={**payload, "values": []}).status_code == 422
        response = client.post(path, headers=headers, json=payload)
        assert response.json() == {"collected": 2, "partial": False}
        assert calls == [("frontend", ["react", "vite"], [], 7)]
        response = client.post(path, headers=headers, json={**payload, "provider": "github",
                                                            "values": ["sample/project"]})
        assert response.status_code == 429 and response.json()["error"]["code"] == "DISCOVERY_RATE_LIMIT"
        assert response.headers["Retry-After"] == "90"
        removed = client.post(path, headers=headers, json={**payload, "provider": "arxiv"})
        assert removed.status_code == 422


def test_github_collection_saves_candidates_for_automatic_check(sessions, monkeypatch):
    def upstream(request):
        path = request.url.path
        if path == "/repos/sample/private":
            return httpx.Response(200, json={"private": True})
        if path == "/repos/sample/project":
            return httpx.Response(200, json={"private": False})
        assert path == "/repos/sample/project/issues"
        return httpx.Response(200, json=[issue_entry(comments=0)])

    monkeypatch.setattr(discovery, "httpx", SimpleNamespace(
        Client=lambda **options: httpx.Client(transport=httpx.MockTransport(upstream), **options)
    ))
    result = discovery.collect(sessions, Settings(), "github", "python_backend",
                               ["sample/private", "sample/project"], 2, 10, 100, ["performance"])
    assert result == {"collected": 1, "partial": False}
    with sessions() as db:
        assert db.get(DiscoveryItem, "github:123") is not None
        assert db.get(DiscoveryCandidate, ("github:123", "python_backend")).status == "new"



def test_trends_fetch_once_and_compare_weekly_counters(sessions):
    calls = []
    repo = {"id": 7, "full_name": "sample/project", "stargazers_count": 130, "created_at": "2026-01-01T00:00:00Z",
            "private": False}

    def upstream(request):
        calls.append(request.url.path)
        if request.url.path == "/search/repositories":
            assert "stars:>=100" in request.url.params["q"]
            return httpx.Response(200, json={"incomplete_results": False, "items": [repo]})
        if request.url.path.endswith("/readme"):
            text = base64.b64encode(b"A library for building knowledge bases.").decode()
            return httpx.Response(200, json={"content": text, "encoding": "base64"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        results, partial = repository_trends(client, ["rag", "llm"], 2, 5, "2026-01-01", "2026-06-01", 100, [])
        assert not partial and len(results) == 1 and calls.count("/repos/sample/project/readme") == 1
        unavailable, _ = repository_trends(client, [], 2, 5, "2026-01-01", "2026-06-01", 100, results)
        assert unavailable[0].payload["unavailable"]
    one = results[0]
    today = datetime(2026, 7, 1)
    with sessions.begin() as db:
        discovery.collect_records(db, "trends", "python_backend", results, today - timedelta(days=7))
        discovery.collect_records(db, "trends", "python_backend", results, today - timedelta(days=7))
        assert discovery.list_candidates(db, "python_backend", 10)[0]["weekly_star_change"] is None
        increased = Collected(one.id, one.title, one.url, {**one.payload, "stars": 147})
        discovery.collect_records(db, "trends", "python_backend", [increased], today)
        assert discovery.list_candidates(db, "python_backend", 10)[0]["weekly_star_change"] == 17
        assert db.scalar(select(func.count()).select_from(RepositoryObservation)) == 2


def test_automatic_check_retries_verifies_provenance_and_invalidates_old_excerpt(sessions, monkeypatch):
    problem = "PostgreSQL 16 used a sequential scan for slow reports."
    solution = "The fix was to reuse cardinality statistics."
    url = "https://github.com/sample/project/issues/4"
    item = Collected("github:123", "Slow report", url, {
        "repo": "sample/project", "closed_at": now().isoformat() + "Z", "fragments": {
            "body": {"url": url, "text": problem},
            "comment:20": {"url": url + "#issuecomment-20", "text": solution, "association": "MEMBER"},
            "comment:21": {"url": url + "#issuecomment-21", "text": solution, "association": "NONE"},
        },
    })
    intake = {"role": "Python backend"}
    state = {"facts": [{"field": "task", "text": "Оптимизировали медленный запрос отчёта PostgreSQL 16."}]}
    settings = Settings(llm_base_url="http://example.test", llm_model="test")
    key = (item.id, "python_backend")
    response = {
        "relevant": True, "task": "Медленный запрос отчёта PostgreSQL", "quote": problem,
        "detail": "sequential scan", "resolution_fragment": "comment:20", "resolution_quote": solution,
        "technology": "PostgreSQL", "version": "16", "conditions": [],
    }
    available = False

    async def model(_, __, ___, ____, payload):
        assert set(payload["fragments"]) == {"body", "comment:20"}
        if not available:
            raise TimeoutError()
        return response

    monkeypatch.setattr(github_evidence, "structured_completion", model)
    with sessions.begin() as db:
        for profession in ("python_backend", "qa"):
            discovery.collect_records(db, "github", profession, [item], now())
        assert sources_for_followup(db, intake, state, "action") == []
    asyncio.run(github_evidence.process_pending(sessions, settings, profession=key[1]))
    with sessions.begin() as db:
        candidate = db.get(DiscoveryCandidate, key)
        assert candidate.retry_at > now()
        assert candidate.status == "new"
        assert github_evidence.claim_candidate(sessions, key[1]) is None
        candidate.retry_at = now() - timedelta(seconds=1)
    available = True
    asyncio.run(github_evidence.process_pending(sessions, settings, profession=key[1]))
    with sessions.begin() as db:
        candidate = db.get(DiscoveryCandidate, key)
        assert candidate.status == "accepted"
        assert candidate.assessment["resolution_quote"] == solution
        hints = sources_for_followup(db, intake, state, "action")
        assert [hint["task_excerpt"] for hint in hints] == [problem]
        assert solution not in str(hints)
        other_version = {"facts": [{"field": "task", "text": state["facts"][0]["text"].replace("16", "15")}]}
        assert not sources_for_followup(db, intake, other_version, "action")
        assert db.get(DiscoveryCandidate, (item.id, "qa")).status == "new"

        revised = Collected(item.id, item.title, item.url, {**item.payload, "state_reason": "completed"})
        discovery.collect_records(db, "github", "python_backend", [revised], now())
        assert not sources_for_followup(db, intake, state, "action")
    response["resolution_fragment"] = "comment:21"
    asyncio.run(github_evidence.process_pending(sessions, settings, profession=key[1]))
    with sessions() as db:
        candidate = db.get(DiscoveryCandidate, key)
        assert candidate.status == "observe"
        assert candidate.excerpt_id  # An invalidated excerpt must stay linked to its candidate.
        assert not sources_for_followup(db, intake, state, "action")


def test_automatic_check_cannot_publish_a_superseded_issue(sessions):
    item = Collected("github:123", "Slow report", "https://github.com/sample/project/issues/4", {})
    key = (item.id, "python_backend")
    with sessions.begin() as db:
        discovery.collect_records(db, "github", key[1], [item], now())
    claimed, profile, _, lease = github_evidence.claim_candidate(sessions)
    assert github_evidence.claim_candidate(sessions) is None
    revised = Collected(item.id, item.title, item.url, {"reason": "Issue reopened or expired"})
    with sessions.begin() as db:
        discovery.collect_records(db, "github", key[1], [revised], now())
    github_evidence.save_outcome(sessions, claimed, profile, lease, {"status": "observe", "reason": "Stale check"})
    with sessions() as db:
        assert db.get(DiscoveryCandidate, key).reviewed_digest is None
        assert db.scalar(select(func.count()).select_from(SourceExcerpt)) == 0


def test_documentation_refresh_review_and_invalidation(sessions, monkeypatch):
    main = "EXPLAIN ANALYZE reports actual execution time."
    child = "Compare actual rows with planner estimates."
    banner = "Homepage"
    online = True
    supported = True
    calls = []

    def upstream(request):
        calls.append(str(request.url))
        headers = {"content-type": "text/html; charset=utf-8"}
        if request.url.path == "/support/versioning/":
            return httpx.Response(200, headers=headers, text=f"""
                <table><tr><td>16</td><td>16.16</td><td>{'Yes' if supported else 'No'}</td>
                <td>September 14, 2023</td><td>November 9, 2028</td></tr></table>""")
        assert str(request.url) == TARGETS["postgresql-explain"].url
        if not online:
            return httpx.Response(503)
        return httpx.Response(200, headers=headers, text=f"""
            <title>PostgreSQL: Documentation: 16: 14.1. Using EXPLAIN</title>
            <nav>{banner}</nav>
            <div class="sect1" id="USING-EXPLAIN">
              <h2>14.1. Using <code>EXPLAIN</code><a class="id_link" href="#USING-EXPLAIN">#</a></h2>
              <div class="toc">Table of contents {banner}</div>
              <p>{main}</p>
              <pre>SELECT *\n  FROM reports;</pre>
              <div class="sect2"><h3><a id="ANALYZE"></a>14.1.1. EXPLAIN ANALYZE</h3>
                <p>{child}</p></div>
            </div>
            <h2>An unrelated chapter</h2><p>{banner}</p>
        """)

    monkeypatch.setattr(documentation, "httpx", SimpleNamespace(
        Client=lambda **options: httpx.Client(transport=httpx.MockTransport(upstream), **options),
        HTTPStatusError=httpx.HTTPStatusError, HTTPError=httpx.HTTPError,
    ))
    app = create_app()
    actor = {"id": "editor", "is_editor": False}
    app.dependency_overrides[current_user] = lambda: actor
    profile = {"profession_id": "python_backend"}
    intake = {"role": "Python backend"}
    state = {"facts": [{"field": "task", "text": "Оптимизировали медленный запрос отчёта PostgreSQL 16."}]}
    endpoint = "/api/v1/discovery"
    docs = f"{endpoint}/documentation"
    headers = {"Origin": app.state.app_origin}
    with TestClient(app) as client:
        assert client.get(docs, params=profile).status_code == 403
        actor["is_editor"] = True
        selected = {**profile, "targets": ["postgresql-explain"]}
        assert client.put(docs, json=selected).status_code == 403
        assert client.put(docs, headers=headers, json={**profile, "targets": ["https://localhost"]}).status_code == 422
        assert client.put(docs, headers=headers, json=selected).status_code == 200

        def refresh():
            response = client.post(docs + "/collect", headers=headers, json=profile)
            assert response.status_code == 200, response.text
            return response.json()

        assert refresh()["changed"] == 2
        items = client.get(endpoint, params={**profile, "provider": "docs"}).json()["items"]
        root = next(item for item in items if item["title"] == "Using EXPLAIN")
        detail_url = f"{endpoint}/items/{root['id']}"
        review_url = f"{docs}/{root['id']}/review"
        initial = client.get(detail_url, params=profile).json()
        assert initial["payload"]["fragments"]["section"]["text"] == main + "\n\nSELECT *\n  FROM reports;"
        assert initial["url"].endswith("#USING-EXPLAIN")
        review = {**profile, "digest": root["digest"], "decision": "accepted", "reason": "Reviewed",
                  "evidence": {"task": "Медленный запрос отчёта PostgreSQL", "quote": main,
                               "detail": "actual execution time", "conditions": []}}
        invalid = {**review, "evidence": {**review["evidence"], "quote": "Invented quote."}}
        assert client.post(review_url, headers=headers, json=invalid).status_code == 422
        response = client.post(review_url, headers=headers, json=review)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "accepted"

        banner = "New navigation theme"
        assert refresh()["changed"] == 0
        child = "Compare actual and estimated row counts for a node."
        assert refresh()["changed"] == 1
        online = False
        assert "HTTP 503" in refresh()["errors"]["postgresql-explain"]
        with sessions() as db:
            assert [hint["task_excerpt"] for hint in sources_for_followup(db, intake, state, "verification")] == [main]
            candidate = db.get(DiscoveryCandidate, (root["id"], "python_backend"))
            assert candidate.valid_until.year - now().year == 3
        online = True
        supported = False
        assert refresh()["changed"] == 2
        revised = client.get(detail_url, params=profile).json()
        assert revised["status"] == "review"
        assert "+Версия 16 · Поддержка завершена" in revised["documentation_diff"]
        assert client.post(review_url, headers=headers, json=review).status_code == 409
        assert client.post(review_url, headers=headers, json={**review, "digest": revised["digest"]}).status_code == 422
        with sessions() as db:
            assert not sources_for_followup(db, intake, state, "verification")
            assert db.scalar(select(func.count()).select_from(DocumentationRevision)) == 5
        supported = True
        assert refresh()["changed"] == 2
        assert client.get(detail_url, params=profile).json()["status"] == "review"
        assert client.post(review_url, headers=headers, json=review).json()["status"] == "accepted"
        assert client.put(docs, headers=headers, json={**profile, "targets": []}).status_code == 200
        assert client.post(review_url, headers=headers, json=review).status_code == 422
        with sessions() as db:
            assert not sources_for_followup(db, intake, state, "verification")
    assert set(calls) == {TARGETS["postgresql-explain"].url, TARGETS["postgresql-releases"].url}


def test_documentation_release_and_weekly_claim(sessions):
    target = TARGETS["sqlalchemy-releases"]
    html = """<title>2.0 Changelog — SQLAlchemy 2.0 Documentation</title>
        <h2>2.0.59</h2><p>no release date</p>
        <section id="change-2.0.58"><h2>2.0.58 <a class="headerlink" href="#change-2.0.58">¶</a></h2>
        <p>Released: November 16, 2026</p><h3>Engine</h3><p>Documented connection pooling behavior.</p>
        </section><h2>2.0.57</h2><p>Released: November 1, 2026</p>"""
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/html; charset=utf-8"}, text=html,
    ))) as client:
        version, url, notes, published = release_notes(client, target)
    assert (version, published) == ("2.0.58", "2026-11-16")
    assert url.endswith("#change-2.0.58") and "connection pooling behavior" in notes
    at = datetime(2026, 11, 23, 6, tzinfo=timezone.utc)
    assert documentation.weekly_due(at) == at.replace(tzinfo=None)
    assert documentation.weekly_due(at - timedelta(minutes=1)) == (at - timedelta(days=7)).replace(tzinfo=None)
    due = now() - timedelta(minutes=10)
    token = documentation.claim_watch(sessions, "sqlalchemy-releases", due)
    assert token and documentation.claim_watch(sessions, "sqlalchemy-releases", None) is None
    assert documentation.finish_watch(sessions, "sqlalchemy-releases", token, {}, None) == (0, 0)
    assert documentation.claim_watch(sessions, "sqlalchemy-releases", due) is None
