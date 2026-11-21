"""Collect IT stories and bounded comment snapshots from the official Hacker News API."""

import re
import socket
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from itertools import batched
from time import monotonic
from urllib.parse import unquote, urlsplit

import httpx
from sqlalchemy import and_, case, func, or_, select

from backend.db import HackerNewsItem, VacancyCollection, insert_for, now
from backend.discovery_fetch import CollectionError, fetch
from backend.vacancy_parser import SKILL_ALIASES, canonical_skill, clean_html, plain
from backend.vacancy_samples import fresh_vacancy_samples, trudvsem_samples


API = "https://hacker-news.firebaseio.com/v0"
DISCUSSION = "https://news.ycombinator.com/item?id="
FEEDS = ("topstories", "newstories", "beststories")
WORKERS = 6
PROFILE_TECHNOLOGIES = {
    "python_backend": ("python", "django", "flask", "fastapi", "postgresql", "sqlalchemy", "redis", "celery", "llm", "ai"),
    "frontend": ("javascript", "typescript", "react", "vue", "angular", "vite", "svelte", "next.js", "webassembly"),
    "qa": ("pytest", "selenium", "playwright", "cypress", "postman", "test automation", "sdet"),
    "devops": ("kubernetes", "docker", "terraform", "ansible", "helm", "prometheus", "nginx", "linux", "rust", "devops"),
}
EXTRA_ALIASES = {"ai": ("ai", "artificial intelligence"), "llm": ("llm", "llms", "large language models")}
AMBIGUOUS_TERMS = {"ts", "js", "rest", "go", "git"}
EXCLUDED_TITLE = re.compile(r"\b(?:ask\s+hn|show\s+hn|jobs?)\b", re.I)
URL_SUFFIX = re.compile(r"\.(?:html?|js|ts|css|php|aspx|pdf)$", re.I)
MAX_INTEGER = 2_147_483_647
DNS_FAILURE = "DNS lookup failed"


class HackerNewsUnavailable(CollectionError):
    pass


def request_error(exc):
    cause = exc
    while cause is not None:
        if isinstance(cause, socket.gaierror):
            return DNS_FAILURE
        cause = cause.__cause__ or cause.__context__
    return type(exc).__name__


def item_ids(value):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(id_ for id_ in value if type(id_) is int and 0 < id_ <= MAX_INTEGER))


def api_json(client, path, deadline):
    if monotonic() >= deadline:
        return None, "Time budget exhausted"
    try:
        response, _ = fetch(client, f"{API}/{path}.json")
        return response.json(), None
    except CollectionError as exc:
        return None, str(exc)
    except httpx.HTTPError as exc:
        return None, request_error(exc)
    except ValueError:
        return None, "Invalid JSON"


def item_batch(client, pool, ids, deadline):
    return pool.map(lambda id_: api_json(client, f"item/{id_}", deadline), ids)


def collect_ranks(client, pool, feed_limit, deadline):
    ranks, failures = {}, {}
    for feed, (data, error) in zip(FEEDS, pool.map(lambda name: api_json(client, name, deadline), FEEDS)):
        if error or not isinstance(data, list):
            failures[feed] = error or "Invalid story feed"
            continue
        for rank, id_ in enumerate(data[:feed_limit], start=1):
            if type(id_) is int and 0 < id_ <= MAX_INTEGER:
                ranks.setdefault(id_, {}).setdefault(feed, rank)
    if len(failures) == len(FEEDS):
        errors = ", ".join(f"{feed}: {error}" for feed, error in failures.items())
        message = f"Hacker News feeds unavailable ({errors})"
        if set(failures.values()) == {DNS_FAILURE}:
            message = (
                "DNS lookup failed while connecting to hacker-news.firebaseio.com. "
                "Check DNS in this terminal or use an approved HTTPS_PROXY if your network requires one."
            )
        raise HackerNewsUnavailable(message)
    return ranks, failures


def profile_terms(db, profession, additional, stamp):
    profiles = list(db.scalars(select(VacancyCollection)))
    if profession and not any(profile.profession_id == profession for profile in profiles):
        raise CollectionError("Unknown profession")
    if additional and not profession:
        raise CollectionError("Specify a profession when using --technology")
    samples = trudvsem_samples(db, profession)
    result = {}
    for profile in profiles:
        id_ = profile.profession_id
        terms = set(PROFILE_TECHNOLOGIES.get(id_, ()))
        if profile.approved:
            mentions = Counter(skill for cards, _ in fresh_vacancy_samples(profile, samples.get(id_), stamp)
                               for card in cards
                               for skill in set(filter(None, map(canonical_skill, card.get("skills", [])))))
            terms.update(skill for skill, count in mentions.items() if count >= 2)
        if id_ == profession:
            terms.update(cs for term in additional if term.strip() and (cs := canonical_skill(term.strip())))
        if terms:
            result[id_] = sorted(terms)
    if profession and profession not in result:
        raise CollectionError("Add profile technologies with --technology or approve a current vacancy collection")
    if not result:
        raise CollectionError("No configured IT profiles")
    return result


def matches_for_profiles(title, url, profiles):
    parsed = urlsplit(url)
    host = parsed.hostname.rstrip(".").rsplit(".", 1)[0]
    path = URL_SUFFIX.sub("", unquote(parsed.path))
    url_words = re.sub(r"[-_]", " ", f"{host} {path}")
    corpus = title + "\n" + url_words

    def matches(term):
        aliases = SKILL_ALIASES.get(term, EXTRA_ALIASES.get(term, (term,)))
        return any(
            (alias not in AMBIGUOUS_TERMS or alias == term) and
            re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", corpus, re.I)
            for alias in aliases
        )
    found = {term for term in {term for terms in profiles.values() for term in terms} if matches(term)}
    return {id_: hits for id_, terms in profiles.items() if (hits := sorted(found.intersection(terms)))}


def public_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    if any(char.isspace() or ord(char) < 32 or char == "\\" for char in value):
        return False
    try:
        url = urlsplit(value)
        port = url.port
        return (url.scheme in {"http", "https"} and bool(url.hostname) and url.username is None
                and port != 0 and url.hostname.rstrip(".") != "news.ycombinator.com")
    except ValueError:
        return False


class LinkedSources(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = {}

    def handle_starttag(self, tag, attrs):
        if tag == "a" and len(self.urls) < 12:
            href = dict(attrs).get("href")
            if public_url(href):
                self.urls[href] = None


def evidence_links(html):
    parser = LinkedSources()
    parser.feed(html[:60_000])
    parser.close()
    return list(parser.urls)


def timestamp(value):
    if type(value) is not int or not 0 < value < 253402300799:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return None


def person(value):
    return value[:64] if isinstance(value, str) and value else None


def story_record(id_, raw, ranks, profiles, profession):
    if not isinstance(raw, dict) or raw.get("id") != id_ or raw.get("type") != "story":
        return None, "not_story"
    score, descendants, url = raw.get("score"), raw.get("descendants"), raw.get("url")
    if raw.get("deleted") or raw.get("dead") or not public_url(url):
        return None, "unavailable_or_no_external_url"
    if type(score) is not int or not 50 < score <= MAX_INTEGER:
        return None, "score"
    if type(descendants) is not int or not 10 < descendants <= MAX_INTEGER:
        return None, "discussion_size"
    title = plain(raw.get("title"))[:500] if isinstance(raw.get("title"), str) else ""
    published_at = timestamp(raw.get("time"))
    if not title or not published_at or EXCLUDED_TITLE.search(title):
        return None, "title_or_date"
    matches = matches_for_profiles(title, url, profiles)
    if not matches or (profession and profession not in matches):
        return None, "irrelevant"
    text = raw.get("text") if isinstance(raw.get("text"), str) else ""
    return {
        "id": id_, "title": title, "url": url, "author": person(raw.get("by")),
        "published_at": published_at, "score": score, "descendants": descendants,
        "text": clean_html(text)[:6000], "links": evidence_links(text),
        "kids": item_ids(raw.get("kids")), "profile_matches": matches, "feed_ranks": ranks,
    }, None


def collect_comments(client, pool, story, limit, deadline):
    frontier = [(id_, story["id"], rank) for rank, id_ in enumerate(story["kids"], start=1) if id_ != story["id"]]
    seen, comments = {story["id"], *story["kids"]}, []
    limited = incomplete = False
    budget = limit
    for depth in range(1, 4):
        if not frontier:
            break
        # Reserve room for replies even if top-level comments fill the budget.
        batch = frontier[:max(1, budget - (3 - depth))] if budget else []
        limited |= len(batch) < len(frontier)
        budget -= len(batch)
        children = []
        for (id_, parent, rank), (raw, error) in zip(
            batch, item_batch(client, pool, [id_ for id_, _, _ in batch], deadline),
        ):
            if error:
                incomplete = True
                continue
            if not isinstance(raw, dict) or raw.get("id") != id_ or raw.get("parent") != parent or raw.get("type") != "comment":
                continue
            html = raw.get("text")
            if not (raw.get("deleted") or raw.get("dead")) and isinstance(html, str) and (text := clean_html(html)[:6000]):
                published = raw.get("time")
                comments.append({
                    "id": id_, "parent": parent, "rank": rank, "depth": depth, "author": person(raw.get("by")),
                    "time": published if timestamp(published) else None, "text": text,
                    "url": DISCUSSION + str(id_), "links": evidence_links(html),
                })
            if depth < 3:
                for child_rank, child_id in enumerate(item_ids(raw.get("kids")), start=1):
                    if child_id not in seen:
                        seen.add(child_id)
                        children.append((child_id, id_, child_rank))
        frontier = children
    return {"comments": comments, "comments_limited": limited, "comments_incomplete": incomplete}


def store_stories(sessions, stories, stamp):
    with sessions.begin() as db:
        for story in sorted(stories, key=lambda record: record["id"]):
            statement = insert_for(db, HackerNewsItem).values(
                **story, first_seen_at=stamp, observed_at=stamp, comments_observed_at=stamp,
            )
            incoming = statement.excluded
            updated = {key: incoming[key] for key in story if key != "id"}
            updated["observed_at"] = incoming.observed_at
            replace_comments = and_(incoming.comments_incomplete.is_(False), or_(
                HackerNewsItem.comments_incomplete.is_(True), incoming.comments_limited.is_(False),
                func.json_array_length(incoming.comments) >= func.json_array_length(HackerNewsItem.comments),
            ))
            for key in ("comments", "comments_limited", "comments_incomplete", "comments_observed_at"):
                updated[key] = case((replace_comments, incoming[key]), else_=getattr(HackerNewsItem, key))
            db.execute(statement.on_conflict_do_update(
                index_elements=[HackerNewsItem.id], set_=updated,
                where=HackerNewsItem.observed_at <= stamp,
            ))


def collect_hn(sessions, profession=None, *, technologies=(), feed_limit=50, limit=10, comment_limit=40, transport=None):
    stamp = now()
    with sessions() as db:
        profiles = profile_terms(db, profession, technologies, stamp)
    deadline = monotonic() + 100
    stories, rejected = [], Counter()
    story_errors = examined = comment_errors = 0
    with (
        httpx.Client(timeout=7, transport=transport, follow_redirects=False,
                     headers={"User-Agent": "Modeus-Interviewer", "Accept": "application/json"}) as client,
        ThreadPoolExecutor(max_workers=WORKERS) as pool,
    ):
        ranks, failures = collect_ranks(client, pool, feed_limit, deadline)
        for batch in batched(ranks, WORKERS):
            if len(stories) >= limit or monotonic() >= deadline:
                break
            for id_, (raw, error) in zip(batch, item_batch(client, pool, batch, deadline)):
                examined += 1
                if error:
                    story_errors += 1
                    continue
                story, reason = story_record(id_, raw, ranks[id_], profiles, profession)
                if reason:
                    rejected[reason] += 1
                    continue
                story.update(collect_comments(client, pool, story, comment_limit, deadline))
                comment_errors += int(story["comments_incomplete"])
                stories.append(story)
                if len(stories) >= limit:
                    break
    store_stories(sessions, stories, stamp)
    return {
        "collected": len(stories), "examined": examined, "feed_failures": failures, "rejected": dict(rejected),
        "story_errors": story_errors, "comment_errors": comment_errors,
        "partial": bool(failures or story_errors or comment_errors or monotonic() >= deadline),
        "items": [{key: story[key] for key in ("id", "title", "url", "score", "profile_matches")} for story in stories],
    }
