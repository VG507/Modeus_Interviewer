import socket
from collections import Counter
from datetime import timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from backend import hn
from backend.db import HackerNewsItem, VacancyCollection, database, now
from backend.discovery_fetch import CollectionError


@pytest.fixture
def sessions(tmp_path):
    engine, sessions = database(f"sqlite:///{(tmp_path / 'hn.db').as_posix()}")
    config = Config("alembic.ini")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    yield sessions
    engine.dispose()


def story(id_, **changes):
    return {
        "id": id_, "type": "story", "title": "Python &amp; PostgreSQL improvements",
        "url": "https://example.org/release", "time": 1_760_000_000, "by": "submitter",
        "score": 51, "descendants": 11, "kids": [], **changes,
    }


def comment(id_, parent, **changes):
    return {"id": id_, "parent": parent, "type": "comment", "time": 1_760_000_010,
            "by": "reader", "text": "<p>A benchmark &amp; a question.</p>", **changes}


def test_fetch_deduplicates_filters_and_stops_at_third_comment_level(sessions):
    feeds = {"topstories": [4, 1, 2, 3, 5, 6], "newstories": [1, 7], "beststories": [1, 4]}
    records = {
        1: story(1, kids=[101, 102, 101]),
        2: story(2, score=50, kids=[201]),
        3: story(3, descendants=10, kids=[301]),
        4: story(4, title="Show HN: Python test", kids=[401]),
        5: story(5, url="https://news.ycombinator.com/item?id=5"),
        6: story(6, title="A general memoir"),
        7: story(7, title="A runtime improvement", url="https://example.org/releases/python-3"),
        101: comment(101, 1, kids=[103], text='<p>A <a href="https://example.com/benchmark">benchmark</a> &amp; a question.</p>'),
        102: comment(102, 1, deleted=True, text="Do not store a deleted comment"),
        103: comment(103, 101, kids=[104, 101]),
        104: comment(104, 103, kids=[105]),
    }
    requests = []

    def upstream(request):
        assert request.url.host == "hacker-news.firebaseio.com"
        assert "authorization" not in request.headers
        key = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        requests.append(key)
        return httpx.Response(200, json=feeds[key] if key in feeds else records[int(key)])

    transport = httpx.MockTransport(upstream)
    result = hn.collect_hn(sessions, "python_backend", transport=transport)
    assert not result["partial"] and result["collected"] == 2
    assert result["rejected"] == {
        "title_or_date": 1, "score": 1, "discussion_size": 1, "unavailable_or_no_external_url": 1, "irrelevant": 1,
    }
    assert Counter(requests)["1"] == 1
    assert not {"105", "201", "301", "401"} & set(requests)
    with sessions.begin() as db:
        item = db.get(HackerNewsItem, 1)
        assert item.title == "Python & PostgreSQL improvements"
        assert item.status == "new"
        assert item.feed_ranks == {"topstories": 2, "newstories": 1, "beststories": 1}
        assert item.profile_matches == {"python_backend": ["postgresql", "python"]}
        assert [(row["id"], row["depth"]) for row in item.comments] == [(101, 1), (103, 2), (104, 3)]
        assert item.comments[0]["text"] == "A benchmark & a question."
        assert item.comments[0]["url"].endswith("id=101")
        assert item.comments[0]["links"] == ["https://example.com/benchmark"]
        assert item.kids == [101, 102]
        item.status = "review"
    hn.collect_hn(sessions, "python_backend", transport=transport)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(HackerNewsItem)) == 2
        assert db.get(HackerNewsItem, 1).status == "review"


def test_partial_fetch_keeps_the_previous_discussion(sessions, monkeypatch):
    stamp = now()
    monkeypatch.setattr(hn, "now", lambda: stamp)
    partial = False
    unavailable = False

    def upstream(request):
        path = request.url.path
        if unavailable or (partial and path.endswith("beststories.json")):
            return httpx.Response(503)
        if path.endswith("stories.json"):
            return httpx.Response(200, json=[1])
        if path.endswith("/1.json"):
            return httpx.Response(200, json=story(1, kids=[101, 102], score=90 if partial else 85))
        if partial:
            return httpx.Response(503)
        id_ = int(path.rsplit("/", 1)[-1].removesuffix(".json"))
        return httpx.Response(200, json=comment(id_, 1))

    transport = httpx.MockTransport(upstream)
    assert hn.collect_hn(sessions, transport=transport)["collected"] == 1
    first = stamp
    stamp += timedelta(minutes=1)
    partial = True
    result = hn.collect_hn(sessions, transport=transport)
    assert result["partial"] and result["comment_errors"] == 1
    assert "beststories" in result["feed_failures"]
    with sessions() as db:
        item = db.get(HackerNewsItem, 1)
        assert (item.score, item.observed_at, item.comments_observed_at) == (90, stamp, first)
        assert not item.comments_incomplete and len(item.comments) == 2
    unavailable = True
    with pytest.raises(CollectionError, match="Hacker News feeds unavailable"):
        hn.collect_hn(sessions, transport=transport)
    with sessions() as db:
        assert db.get(HackerNewsItem, 1).score == 90
    partial = unavailable = False
    stamp += timedelta(minutes=1)
    hn.collect_hn(sessions, comment_limit=1, transport=transport)
    with sessions() as db:
        item = db.get(HackerNewsItem, 1)
        assert item.observed_at == stamp and item.comments_observed_at == first
        assert [entry["id"] for entry in item.comments] == [101, 102]


def test_custom_profile_takes_skills_from_current_approved_vacancies(sessions):
    stamp = now()
    with sessions.begin() as db:
        db.add(VacancyCollection(profession_id="systems", title="Systems", title_key="systems", approved=True,
                                 updated_at=stamp, cards=[{"skills": ["Rust", "Git"]}, {"skills": ["rust"]}]))
        db.flush()
        profiles = hn.profile_terms(db, "systems", ["LLVM"], stamp)
    assert profiles["systems"] == ["llvm", "rust"]
    assert hn.matches_for_profiles("Compiler update", "https://example.com/rust-stdlib", profiles)["systems"] == ["rust"]


def test_dns_failure_is_reported_before_writing_news(sessions):
    def upstream(request):
        try:
            try:
                raise socket.gaierror(11001, "getaddrinfo failed")
            except socket.gaierror:
                raise OSError("Transport failed") from None
        except OSError as exc:
            raise httpx.ConnectError("Connection failed", request=request) from exc

    with pytest.raises(hn.HackerNewsUnavailable, match=r"DNS lookup failed.*hacker-news\.firebaseio\.com"):
        hn.collect_hn(sessions, transport=httpx.MockTransport(upstream))
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(HackerNewsItem)) == 0
