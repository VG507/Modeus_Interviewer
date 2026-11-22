import asyncio
import base64
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import agent_skills, discovery, documentation, esco, github_evidence, interview_engine, interview_worker
from backend import openalex, profession_collection
from backend import discovery_api
from backend.app import create_app
from backend.auth import current_user
from backend.db import (
    DocumentationRevision, DiscoveryCandidate, DiscoveryItem, DiscoveryRevision, RepositoryObservation,
    InterviewSession, KnowledgeCard, Message, Source, SourceExcerpt, User, VacancyCollection, database, now,
)
from backend.discovery_fetch import (
    Collected, CollectionError, ProviderRateLimit, fetch, github_issues, repository_trends,
)
from backend.settings import Settings
from backend.interview_api import message_view
from backend.interview_schemas import CaseState
from backend.knowledge import fact_skills, prior_case_context
from backend.source_context import sources_for_followup
from backend.documentation_fetch import TARGETS, release_notes


@pytest.fixture
def sessions(database_url, monkeypatch):
    url = database_url
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


def test_agent_skill_trace_from_github_to_question_answer_and_card(sessions, monkeypatch):
    target = next(target for target in agent_skills.TARGETS if "webapp-testing" in target.path)
    monkeypatch.setattr(agent_skills, "TARGETS", (target,))
    revision = "a" * 40
    inputs = "Supply the local browser page and a test account."
    output = "Save screenshots of the rendered interface."
    verification = "Compare the screenshots before and after the interaction."
    task = "Проверял поведение веб-приложения в браузере."
    raw = f"---\nname: webapp-testing\ndescription: Browser checks\n---\n{inputs}\n{output}\n{verification}\n"
    requested = []
    analyses = []
    invalid_fragment = False

    def github(request):
        requested.append(request.url.path)
        assert request.url.host == "api.github.com"
        if request.url.path == f"/repos/{target.repo}":
            return httpx.Response(200, json={"full_name": target.repo, "private": False, "default_branch": "main"})
        if request.url.path == f"/repos/{target.repo}/commits/main":
            return httpx.Response(200, json={"sha": revision})
        assert request.url.path == f"/repos/{target.repo}/contents/{target.path}"
        assert request.url.params["ref"] == revision
        content = raw.encode()
        return httpx.Response(200, json={
            "type": "file", "path": target.path, "encoding": "base64", "size": len(content),
            "content": base64.b64encode(content).decode(),
        })

    def fragment(line):
        return {"first_line": line, "last_line": line}

    async def analyze(settings, name, schema, instructions, payload, parse=None):
        analyses.append(payload)
        assert f"3: {verification}" in payload["numbered_text"]
        return {"relevant": True, "inputs": fragment(1), "output": fragment(2),
                "verification": fragment(3000 if invalid_fragment else 3), "human_check": None, "limitation": None}

    monkeypatch.setattr(agent_skills, "structured_completion", analyze)
    settings = Settings(llm_base_url="http://localhost:8001/v1", llm_model="test")

    def collect():
        return agent_skills.collect_skills(sessions, settings, "qa", transport=httpx.MockTransport(github))

    assert collect() == {"status": "ok", "collected": 1, "accepted": 1}
    assert len(requested) == 3
    revision = "b" * 40
    assert collect()["accepted"] == 1
    assert len(analyses) == 1

    with sessions.begin() as db:
        item = db.scalar(select(DiscoveryItem).where(DiscoveryItem.provider == "skills"))
        first_revision = db.get(DiscoveryRevision, (item.id, item.digest))
        assert item.url == first_revision.url
        assert item.payload["commit"] == first_revision.payload["commit"] == "a" * 40
        candidate = db.get(DiscoveryCandidate, (item.id, "qa"))
        excerpt = db.get(SourceExcerpt, candidate.excerpt_id)
        old_source = db.get(Source, excerpt.source_id)
        legacy_source = Source(id="legacy-source", url=old_source.url.replace("a" * 40, revision),
                               text=old_source.text, retrieved_on=old_source.retrieved_on)
        db.add(legacy_source)
        db.flush()
        excerpt.source_id = legacy_source.id
    assert collect()["accepted"] == 1
    assert len(analyses) == 2

    with sessions.begin() as db:
        owner = User(email="skills-owner@example.test", password_hash="unused")
        db.add(owner)
        db.flush()
        interview = InterviewSession(owner_id=owner.id, topic_id="open-interview", context={"role": "QA Engineer"},
                                     case_state=CaseState(open_interview=True).model_dump())
        db.add(interview)
        db.flush()
        interview_id = interview.id
        opening = interview_worker.append_message(db, interview, kind="question", text="Что проверяли?")
        db.flush()
        first = interview_worker.append_message(
            db, interview, kind="answer", text=task, reply_to_question_id=opening.id,
        )
        state, _ = interview_engine.apply_extraction(interview.case_state, interview_engine.Evidence(
            facts=[interview_engine.Candidate(field="task", quote=task)], ambiguous=[], not_applicable=[], refused=False,
        ), first.id, "get_case")
        interview.case_state = state
        assert sources_for_followup(db, interview.context, state, "action") == []
        for role, text in (("Python Backend", task), ("QA Engineer", "Настраивал резервное копирование.")):
            assert sources_for_followup(db, {"role": role}, {"facts": [{"field": "task", "text": text}]},
                                        "verification") == []
        assert sources_for_followup(db, interview.context, state, "verification", verification) == []
        interview_worker.queue(interview, "question", intent="verification", answer_id=first.id)

    async def question_model(settings, schema, instructions, payload, check=None):
        assert payload["market_context"][0]["task_excerpt"] == verification
        assert payload["market_context"][0]["provider"] == "skills"
        return schema(text="Как вы проверяли результат взаимодействия до и после действия?", source_number=1)

    answer_text = "Сравнил скриншоты с эталонами."

    async def extract(settings, question, answer, state):
        assert question == "Как вы проверяли результат взаимодействия до и после действия?"
        return interview_engine.Evidence(facts=[interview_engine.Candidate(field="verification", quote=answer_text)],
                                         ambiguous=[], not_applicable=[], refused=False)

    monkeypatch.setattr(interview_engine, "ask_model", question_model)
    monkeypatch.setattr(interview_worker, "extract", extract)

    def run_stage():
        job = interview_worker.claim(sessions, 30)
        assert job and job[0] == interview_id
        asyncio.run(interview_worker.execute_stage(sessions, settings, *job))

    run_stage()
    with sessions.begin() as db:
        interview = db.get(InterviewSession, interview_id)
        question = db.get(Message, interview.current_question_id)
        trace = question.market_sources[0]
        assert trace["provider"] == "skills" and trace["used"]
        assert "/blob/" + ("a" * 40) + "/" in trace["url"]
        assert message_view(question).source_quote.quote == verification
        reply = interview_worker.append_message(
            db, interview, kind="answer", status="queued", text=answer_text, reply_to_question_id=question.id,
        )
        interview_worker.queue(interview, "synthesis", completion_version=interview.version,
                               answer_id=reply.id, current_intent="verification")
    run_stage()

    raw += "\nChanged how screenshots are archived.\n"
    revision = "c" * 40
    invalid_fragment = True
    assert collect()["accepted"] == 0
    with sessions.begin() as db:
        card = db.scalar(select(KnowledgeCard).where(KnowledgeCard.session_id == interview_id))
        fact = card.content["verification"][0]
        assert fact["text"] == answer_text
        answer = db.get(Message, fact["supporting_message_ids"][0])
        question = db.get(Message, answer.reply_to_question_id)
        assert question.market_sources[0] == trace
        assert db.get(DiscoveryRevision, (trace["item_id"], trace["revision_digest"])).url == trace["url"]
        assert db.get(DiscoveryCandidate, (trace["item_id"], "qa")).status == "observe"
        state = {"facts": [{"field": "task", "text": task}]}
        assert sources_for_followup(db, {"role": "QA Engineer"}, state, "verification") == []
    invalid_fragment = False
    assert collect()["accepted"] == 1
    async def change_skill_during_generation(settings, schema, instructions, payload, check=None):
        with sessions.begin() as db:
            candidate = db.get(DiscoveryCandidate, (trace["item_id"], "qa"))
            candidate.valid_until = now() - timedelta(seconds=1)
        return schema(text="Этот вопрос с истёкшим источником не должен сохраниться?", source_number=1)

    monkeypatch.setattr(interview_engine, "ask_model", change_skill_during_generation)
    with sessions.begin() as db:
        interview = db.get(InterviewSession, interview_id)
        interview.case_state = CaseState(facts=[{
            "id": "grounded-task", "field": "task", "text": task, "supporting_message_ids": ["earlier-answer"],
        }]).model_dump()
        interview_worker.queue(interview, "question", intent="verification")
    run_stage()
    with sessions() as db:
        interview = db.get(InterviewSession, interview_id)
        fallback = db.get(Message, interview.current_question_id)
        assert fallback.text == interview_engine.FALLBACK_QUESTIONS["verification"]
        assert fallback.market_sources is None
        assert sources_for_followup(db, {"role": "QA Engineer"}, state, "verification") == []


def test_skill_labels_and_reuse_of_prior_case_are_conservative():
    evidence = {"supporting_message_ids": ["answer-1"], "responsibility": "personal"}
    assert fact_skills({**evidence, "text": "Я настроил Postgres."}, "actions") == ["postgresql"]
    for text in ("Другая команда проверила результат в Playwright.", "The vendor tested with Selenium.",
                 "Я не применял Docker.", "Мы рассматривали Playwright.", "Планировал использовать Docker.",
                 "Я проверил строку «использовал Postgres»", "Проверил наличие Docker."):
        assert fact_skills({**evidence, "text": text}, "verification") == []
    assert fact_skills({**evidence, "text": "I configured Docker.", "source": "owner_edit"}, "actions") == []

    old = "Предыдущий проект работал только в локальной сети."
    state = {"past_cases": [{"state": {"facts": [{"field": "context", "text": old},
                                                {"field": "decision", "text": "Старое решение"}]}}]}
    assert prior_case_context(state, "Задача была похожая.") == []
    assert prior_case_context(state, "Да, в том же проекте.") == [old]
    assert prior_case_context(state, "Работали по-разному.",
                              ["Это был другой проект.", "Да, в том же проекте."]) == []


def test_only_current_published_card_gaps_reach_question_and_unpublish_invalidates_them(sessions, monkeypatch):
    task = "Проверяли поведение интерфейса при обновлении страницы."
    action = "Я настроил Playwright для проверки страницы."
    state = CaseState(facts=[
        {"field": "task", "id": "task", "text": task, "supporting_message_ids": ["answer"]},
        {"field": "action", "id": "action", "text": action, "responsibility": "personal",
         "supporting_message_ids": ["answer"]},
    ]).model_dump()
    content = interview_engine.render_card("Проверка UI", state, state["facts"])[0]
    assert content["actions"][0]["skills"] == ["playwright"]
    content["context"] = [{"text": "Отдельный тестовый стенд. PRIVATE_SENTINEL",
                           "supporting_message_ids": ["older-answer"], "source": "interview"}]
    assert "verification" in content["unknown_fields"]
    with sessions.begin() as db:
        users = [User(email=f"knowledge-{index}@example.test", password_hash="unused") for index in range(3)]
        db.add_all(users)
        db.flush()
        authors = [InterviewSession(owner_id=user.id, topic_id="open-interview", context={"role": "QA Engineer"},
                                    status="completed") for user in users[1:]]
        current = InterviewSession(owner_id=users[0].id, topic_id="open-interview",
                                   context={"role": "QA Engineer"}, case_state=state)
        db.add_all([current, *authors])
        db.flush()
        cards = [KnowledgeCard(session_id=item.id, owner_id=item.owner_id, origin="human", completion_version=1,
                               catalog_profession_id="qa", publication_status="published", review_status="reviewed",
                               consent={"content_version": 1}, review={"content_version": review_version,
                               "decision": "accept"}, content=content, text="PRIVATE_SENTINEL")
                 for item, review_version in zip(authors, (1, 5))]
        db.add_all(cards)
        db.flush()
        current_id, selected_id = current.id, cards[0].id
        interview_worker.queue(current, "question", intent="verification")
    model_contexts = []
    unpublish = False

    async def model(settings, schema, instructions, payload, check=None):
        model_contexts.append(payload.get("knowledge_gaps"))
        assert "PRIVATE_SENTINEL" not in str(payload)
        if unpublish:
            with sessions.begin() as db:
                published = db.get(KnowledgeCard, selected_id)
                published.publication_status = "draft"
                published.consent = None
                published.version += 1
        return schema(text="Как вы отличали обновление интерфейса от ошибки проверки?")

    monkeypatch.setattr(interview_engine, "ask_model", model)
    for index in range(3):
        unpublish = index == 1
        claim = interview_worker.claim(sessions, 20)
        assert claim and claim[0] == current_id
        asyncio.run(interview_worker.execute_stage(sessions, Settings(), *claim))
        with sessions.begin() as db:
            current = db.get(InterviewSession, current_id)
            question = db.get(Message, current.current_question_id)
            assert "knowledge_gaps" not in current.case_state
            assert question.market_sources is None
            assert question.text == (interview_engine.FALLBACK_QUESTIONS["verification"] if unpublish else
                                     "Как вы отличали обновление интерфейса от ошибки проверки?")
            if index < 2:
                interview_worker.queue(current, "question", intent="verification")
    assert model_contexts == [{"aspect": "verification", "shared_skills": ["playwright"],
                               "conditions_to_clarify": ["тестовая среда"]}] * 2 + [None]


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


def test_openalex_admits_grounded_research_and_removes_it_on_crossref_notice(sessions, monkeypatch):
    doi = "10.1234/flaky"
    published = (now() - timedelta(days=365)).date().isoformat()
    method = "We repeatedly ran each flaky test using pytest."
    sample = "The sample contained 40 Python projects."
    result = "Repeated test runs detected 19 additional failures."
    limitation = "The study covered Python projects from public repositories."
    work = {
        "id": "https://openalex.org/W10", "doi": f"https://doi.org/{doi}",
        "display_name": "Flaky test detection", "publication_date": published,
        "type": "article", "is_retracted": False, "has_content": {"grobid_xml": True},
        "best_oa_location": {"is_oa": True, "version": "publishedVersion", "license": "cc-by"},
    }
    xml = f"""<TEI xmlns="http://www.tei-c.org/ns/1.0">
      <teiHeader><fileDesc><sourceDesc><idno type="DOI">{doi}</idno></sourceDesc></fileDesc></teiHeader>
      <text><body>
        <div><head>Methodology</head><p>{method}</p><p>{sample}</p></div>
        <div><head>Results</head><p>{result}</p></div>
        <div><head>Threats to Validity</head><p>{limitation}</p></div>
      </body></text></TEI>"""
    real_client = httpx.Client
    notice = False
    mismatch = False

    def upstream(request):
        if request.url.host in {"api.openalex.org", "content.openalex.org"}:
            assert request.headers["authorization"] == "Bearer test-key"
            if request.url.host == "content.openalex.org":
                assert request.url.path == "/works/W10.grobid-xml"
                return httpx.Response(200, text=xml)
            return httpx.Response(200, json={"results": [work, work]} if request.url.path == "/works" else work)
        assert "authorization" not in request.headers
        if request.url.host == "api.crossref.org":
            if request.url.path == "/works":
                assert request.url.params["filter"] == f"updates:{doi}"
                return httpx.Response(200, json={"message": {"total-results": int(notice)}})
            return httpx.Response(200, json={"message": {"DOI": doi, "type": "journal-article", "ISSN": ["1111-1111"]}})
        assert request.url.host == "doaj.org"
        return httpx.Response(200, json={"results": [{"id": "verified", "bibjson": {"identifier": [
            {"type": "doi", "id": doi}, {"type": "eissn", "id": "1111-1111"},
        ]}}]})

    async def model(settings, name, schema, instructions, payload, parse=None):
        assert payload["sections"]["method"][0]["heading"] == "Methodology"
        return {"relevant": True, "task": "flaky test detection", "quote": result,
                "detail": "19 additional failures", "method": method, "sample": sample,
                "conditions": ["pytest"], "limitation": "A made-up limitation." if mismatch else limitation,
                "technology": "pytest", "version": None}

    monkeypatch.setattr(openalex.httpx, "Client",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs))
    monkeypatch.setattr(openalex, "structured_completion", model)
    settings = Settings(openalex_api_key="test-key", llm_base_url="http://localhost:8001/v1", llm_model="test")

    def collect_for(profession):
        return openalex.collect_research(sessions, settings, profession, overrides=("flaky test detection",))

    assert collect_for("qa")["accepted"] == 1
    mismatch = True
    assert collect_for("python_backend")["accepted"] == 0
    with sessions() as db:
        item = db.scalar(select(DiscoveryItem).where(DiscoveryItem.provider == "openalex"))
        candidate = db.get(DiscoveryCandidate, (item.id, "qa"))
        output = discovery.candidate_output(db, item, candidate, include_text=True)
        assert output["assessment"]["sections"]["limitation"] == "Threats to Validity"
        assert output["payload"]["review_url"] == "https://doaj.org/article/verified"
        assert "sections" not in output["payload"]
        assert db.get(DiscoveryCandidate, (item.id, "python_backend")).status == "observe"
        item_id, excerpt_id = item.id, candidate.excerpt_id
        state = {"facts": [{"field": "task", "text": "flaky test detection using pytest"}]}
        monkeypatch.setattr("backend.source_context.profession_for_question", lambda *_: "qa")
        assert sources_for_followup(db, {}, state, "verification")[0]["excerpt_id"] == excerpt_id

    notice = True
    assert collect_for("qa")["accepted"] == 0
    with sessions() as db:
        assert db.get(DiscoveryCandidate, (item_id, "qa")).status == "rejected"
        assert db.get(DiscoveryCandidate, (item_id, "qa")).excerpt_id == excerpt_id
        assert db.scalar(select(func.count()).select_from(DiscoveryRevision).where(
            DiscoveryRevision.item_id == item_id)) == 2
        monkeypatch.setattr("backend.source_context.sources_for_question", lambda *args, **kwargs: [])
        assert sources_for_followup(db, {}, state, "verification") == []


def test_openalex_never_uses_abstract_or_xml_from_another_doi():
    doi = "10.1234/right"
    xml = b"""<TEI xmlns="http://www.tei-c.org/ns/1.0">
      <teiHeader><idno type="DOI">10.1234/wrong</idno></teiHeader>
      <text><front><div><head>Methods</head><p>Text only in the abstract.</p></div></front><body/></text>
    </TEI>"""
    assert openalex.full_text_sections(xml, doi) == {}
    assert not any(openalex.full_text_sections(xml, "10.1234/wrong").values())


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
    with sessions() as db:
        assert db.get(VacancyCollection, "frontend").discovery_searches == {
            "trends": {"values": ["react", "vite"], "labels": [], "limit": 7},
        }


def test_profession_collection_can_retry_only_failed_source(sessions, monkeypatch):
    calls = []

    def refresh(_, settings, profession, source):
        assert profession == "python_backend"
        calls.append(source)
        if source == "github" and calls.count(source) == 1:
            raise ProviderRateLimit("rate limited", 90)
        return {"status": "ok", "collected": 1}

    monkeypatch.setattr(profession_collection, "refresh_source", refresh)
    app = create_app()
    actor = {"id": "actor", "is_editor": False}
    app.dependency_overrides[current_user] = lambda: actor
    path = "/api/v1/discovery/collection/python_backend"
    headers = {"Origin": app.state.app_origin}
    with TestClient(app) as client:
        assert client.get(path).status_code == 403
        assert client.post(path, headers=headers).status_code == 403
        actor["is_editor"] = True
        assert client.post(path).status_code == 403
        assert client.get(path).json()["run"] is None
        started = client.post(path, headers=headers)
        assert started.status_code == 202 and started.json()["running"]
        assert sorted(calls) == sorted(profession_collection.SOURCES)
        status = client.get(path).json()
        run = status["run"]
        assert not run["running"]
        assert run["steps"]["github"] == {"status": "failed", "code": "DISCOVERY_RATE_LIMIT"}
        assert run["steps"]["hn"]["collected"] == 1
        assert run["steps"]["openalex"]["collected"] == 1
        assert status["quality"]["materials"]["github"] == {}
        retry = client.post(path, headers=headers, json={"run_id": run["id"], "source": "github"})
        complete = client.get(path).json()["run"]
        assert retry.status_code == 202 and complete["id"] != run["id"]
        assert complete["steps"]["github"]["status"] == "ok"
        assert complete["steps"]["docs"] == run["steps"]["docs"]
        assert calls[-1] == "github" and len(calls) == len(profession_collection.SOURCES) + 1
        stale = client.post(path, headers=headers, json={"run_id": run["id"], "source": "github"})
        finished = client.post(path, headers=headers, json={"run_id": complete["id"], "source": "docs"})
        for rejected in (stale, finished):
            assert rejected.status_code == 409
            assert rejected.json()["error"]["code"] == "COLLECTION_RETRY_UNAVAILABLE"
        assert client.get(path).json()["run"] == complete
        assert len(calls) == len(profession_collection.SOURCES) + 1


def test_esco_collection_from_hal_to_editor_and_interview(sessions, monkeypatch):
    uri = "http://data.europa.eu/esco/occupation/f2b15a0e-e65a-438a-affb-29b9d50b77d1"
    python = {"uri": "http://data.europa.eu/esco/skill/10000000-0000-4000-8000-000000000001",
              "title": "Python (computer programming)"}
    other = {"uri": "http://data.europa.eu/esco/skill/10000000-0000-4000-8000-000000000002",
             "title": "COBOL"}
    requests = []
    unavailable = False

    def respond(request):
        requests.append(request.url.path)
        assert request.url.params["language"] == "en"
        assert request.url.params["selectedVersion"] == "v1.2.0"
        if unavailable:
            return httpx.Response(429)
        if request.url.path.endswith("/search"):
            assert request.url.params["text"] == "software developer"
            return httpx.Response(200, json={"_embedded": {"results": [
                {"title": "other developer", "uri": uri.replace("f2b15a0e", "11111111")},
                {"title": "software developer", "uri": uri},
            ]}})
        assert request.url.params["uri"] == uri
        return httpx.Response(200, json={
            "uri": uri, "title": "software developer", "code": "2512.4",
            "description": {"en": {"literal": "<p>Builds software.</p><script>hidden</script>"}},
            "alternativeLabel": {"en": ["software engineer"]},
            "_links": {"hasEssentialSkill": [python, python], "hasOptionalSkill": [python, other]},
            "_embedded": None,
        })

    client_type = httpx.Client
    monkeypatch.setattr(esco.httpx, "Client", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    app = create_app()
    actor = {"id": "actor", "is_editor": False}
    app.dependency_overrides[current_user] = lambda: actor
    path = "/api/v1/discovery/collection/python_backend"
    headers = {"Origin": app.state.app_origin}
    with TestClient(app) as client:
        assert client.get(path + "/esco").status_code == 403
        actor["is_editor"] = True
        assert client.get(path + "/esco").status_code == 404
        assert client.post(path + "/esco", headers=headers).status_code == 202
        run = client.get(path).json()["run"]
        assert run["steps"] == {"esco": {"status": "ok", "collected": 2}}
        result = client.get(path + "/esco").json()
        assert result["occupation"]["description"] == "Builds software."
        assert result["occupation"]["aliases"] == ["software engineer"]
        assert result["skills"][0]["relations"] == ["essential", "optional"]
        assert result["skills"][0]["title_ru"] == "Программирование на Python"
        assert "/classification/skills?" in result["skills"][0]["url"]
        assert [part.rsplit("/", 1)[1] for part in requests] == ["search", "occupation"]

        with sessions.begin() as db:
            db.get(VacancyCollection, "python_backend").approved = True
            facts = {"facts": [{"field": "task", "text": "Разобрал сбой в коде на питоне"}]}
            intake = {"role": "Python backend developer"}
            assert esco.hints_for_question(db, app.state.settings, intake, facts, "action", "") == [
                "Программирование на Python",
            ]
            facts["facts"][0]["text"] = "Разобрал сбой в коде"
            assert esco.hints_for_question(db, app.state.settings, intake, facts, "action", "Проверял Python") == [
                "Программирование на Python",
            ]
            assert esco.hints_for_question(db, app.state.settings, intake, facts, "action", "Использовал PHP") == []

        unavailable = True
        assert client.post(path + "/esco", headers=headers).status_code == 202
        run = client.get(path).json()["run"]
        assert run["steps"]["esco"] == {"status": "failed", "code": "ESCO_RATE_LIMITED"}
        assert client.get(path + "/esco").json() == result
        assert client.put(path + "/esco", headers=headers, json={
            "target": "https://example.com/occupation/1", "match": "exact",
        }).status_code == 422
        assert client.put(path + "/esco", headers=headers, json={"target": uri, "match": "exact"}).status_code == 204
        assert not client.get(path).json()["quality"]["esco"]["sample"]["current_config"]
        requests.clear()
        unavailable = False
        assert client.post(path, headers=headers, json={"run_id": run["id"], "source": "esco"}).status_code == 202
        assert requests == ["/esco/api/resource/occupation"]
        assert client.get(path + "/esco").json()["match"] == "exact"


def test_esco_ambiguous_labels_and_invalid_relations():
    entry = {"title": "software developer",
             "uri": "http://data.europa.eu/esco/occupation/10000000-0000-4000-8000-000000000001"}
    duplicate = {**entry, "uri": entry["uri"][:-1] + "2"}
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"_embedded": {"results": [entry, duplicate]}},
    ))
    with httpx.Client(base_url=esco.API, transport=transport) as client:
        with pytest.raises(esco.EscoError, match="ESCO_AMBIGUOUS"):
            esco.find_occupation(client, entry["title"], float("inf"))
    with pytest.raises(esco.EscoError, match="ESCO_INVALID_RESPONSE"):
        esco.occupation_skills({"_links": {"hasEssentialSkill": [entry]}})


def test_esco_disregards_result_if_mapping_changes_during_fetch(sessions):
    with sessions.begin() as db:
        _, token = profession_collection.start(db, "python_backend", only="esco")
    with sessions.begin() as db:
        db.get(VacancyCollection, "python_backend").discovery_searches = {
            "esco": {"target": "web developer", "match": "exact"},
        }
    assert profession_collection.change_steps(sessions, "python_backend", token, {"esco": {
        "status": "ok", "collected": 1, "profile": {"target": "software developer", "match": "broader"},
    }})
    app = create_app()
    app.dependency_overrides[current_user] = lambda: {"id": "actor", "is_editor": True}
    with TestClient(app) as client:
        path = "/api/v1/discovery/collection/python_backend"
        assert client.get(path).json()["run"]["steps"]["esco"] == {"status": "failed", "code": "ESCO_SUPERSEDED"}
        assert client.get(path + "/esco").status_code == 404


def test_active_profession_run_is_reused_and_expired_run_can_be_retried(sessions, monkeypatch):
    with sessions.begin() as db:
        run, token = profession_collection.start(db, "python_backend")
    with sessions.begin() as db:
        same, duplicate = profession_collection.start(db, "python_backend")
    assert same == run and duplicate is None
    monkeypatch.setattr(profession_collection, "now", lambda: now() + timedelta(minutes=5))
    with sessions.begin() as db:
        retry, new_token = profession_collection.start(db, "python_backend", token, "github")
    assert new_token != token
    assert retry["steps"]["docs"]["status"] == "interrupted"
    assert not profession_collection.change_steps(sessions, "python_backend", token, {})


def test_parallel_github_collection_uses_the_same_database_lease(sessions, monkeypatch):
    stamp = now()
    monkeypatch.setattr(discovery, "now", lambda: stamp)
    with ExitStack() as old, ExitStack() as replacement:
        old.enter_context(discovery.collection_lease(sessions, "github"))
        with pytest.raises(discovery.DiscoveryBusy), discovery.collection_lease(sessions, "github"):
            pytest.fail("The same source was allowed to collect concurrently")
        with discovery.collection_lease(sessions, "trends"):
            pass
        stamp += timedelta(minutes=16)
        replacement.enter_context(discovery.collection_lease(sessions, "github"))
        old.close()
        with pytest.raises(discovery.DiscoveryBusy), discovery.collection_lease(sessions, "github"):
            pytest.fail("The previous collector released the replacement's lease")
    with discovery.collection_lease(sessions, "github"):
        pass


def test_new_source_revision_preserves_old_payload_and_requires_reevaluation(sessions):
    first = Collected("github:100", "Case", "https://github.com/example/project/issues/10",
                      {"fragments": {"body": {"text": "First description"}}})
    changed = Collected(first.id, first.title, first.url, {"fragments": {"body": {"text": "Updated description"}}})
    stamp = now()
    with sessions.begin() as db:
        discovery.collect_records(db, "github", "python_backend", [first], stamp - timedelta(minutes=1))
        discovery.collect_records(db, "github", "qa", [first], stamp - timedelta(minutes=1))
    with sessions.begin() as db:
        review = db.get(DiscoveryCandidate, (first.id, "python_backend"))
        review.status = "accepted"
        review.reviewed_digest = discovery.item_digest(first)
        review.valid_until = stamp + timedelta(days=1)
    with sessions() as db:
        assert profession_collection.quality(db, "python_backend", Settings())["materials"]["github"] == {"accepted": 1}
    with sessions.begin() as db:
        discovery.collect_records(db, "github", "python_backend", [changed], stamp)
        discovery.collect_records(db, "github", "python_backend", [first], stamp - timedelta(seconds=1))
    with sessions() as db:
        revisions = list(db.scalars(select(DiscoveryRevision).where(DiscoveryRevision.item_id == first.id)))
        assert {row.digest: row.payload for row in revisions} == {
            discovery.item_digest(first): first.payload, discovery.item_digest(changed): changed.payload,
        }
        assert db.get(DiscoveryItem, first.id).payload == changed.payload
        for profile in ("python_backend", "qa"):
            assert profession_collection.quality(db, profile, Settings())["materials"]["github"] == {"automatic_check": 1}


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
        discovery.collect_records(db, "trends", "python_backend", results, today - timedelta(days=6))
        discovery.collect_records(db, "trends", "python_backend", results, today - timedelta(days=6))
        assert discovery.list_candidates(db, "python_backend", 10)[0]["weekly_star_change"] is None
        increased = Collected(one.id, one.title, one.url, {**one.payload, "stars": 147})
        discovery.collect_records(db, "trends", "python_backend", [increased], today)
        assert discovery.list_candidates(db, "python_backend", 10)[0]["weekly_star_change"] == 17
        assert db.scalar(select(func.count()).select_from(RepositoryObservation)) == 2
        exact = Collected(one.id, one.title, one.url, {**one.payload, "stars": 129})
        discovery.collect_records(db, "trends", "python_backend", [exact], today - timedelta(days=7))
        assert discovery.list_candidates(db, "python_backend", 10)[0]["weekly_star_change"] == 18


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

    async def model(_, __, ___, ____, payload, parse=None):
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
