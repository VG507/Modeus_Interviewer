"""Bounded, read-only collection from GitHub."""

import base64
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx


GITHUB = "https://api.github.com"
REPO_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,38}/[a-zA-Z0-9_.-]{1,100}")
SENSITIVE = re.compile(r"\b(?:cve-\d{4}-\d{4,}|ghsa-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+|vulnerabilit\w*|exploit\w*|security\s+advisory|xss|ssrf|rce|csrf|"
                       r"sql injection|privilege escalation|authentication bypass|remote code execution)\b", re.I)
PAGE_SIZE = 30
MAX_BODY = 2_000_000


@dataclass(frozen=True)
class Collected:
    id: str
    title: str
    url: str
    payload: dict


class CollectionError(RuntimeError):
    pass


class ProviderRateLimit(CollectionError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def retry_seconds(headers):
    value = headers.get("retry-after", "")
    if value.isascii() and value.isdecimal() and len(value) <= 8:
        return int(value)
    try:
        if value:
            return max(0, int(parsedate_to_datetime(value).timestamp() - time.time() + 1))
        reset = headers.get("x-ratelimit-reset", "")
        if reset.isascii() and reset.isdecimal() and len(reset) <= 12:
            return max(0, int(int(reset) - time.time() + 1))
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def timestamp(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)


def fetch(client, url, params=None, *, missing=False, redirects=2):
    deadline = time.monotonic() + 25
    with client.stream("GET", url, params=params, headers={"Accept-Encoding": "identity"}) as response:
        status = response.status_code
        if url.startswith(GITHUB + "/") and redirects and status in {301, 302, 307, 308}:
            destination = urljoin(str(response.url), response.headers.get("location", ""))
            target = urlsplit(destination)
            if target.scheme != "https" or target.netloc != "api.github.com" or destination == str(response.url):
                raise CollectionError("GitHub redirected outside its API")
            response.close()
            return fetch(client, destination, missing=missing, redirects=redirects - 1)
        if missing and status == 404:
            return None, False
        if status in (403, 429) and (status == 429 or response.headers.get("x-ratelimit-remaining") == "0"
                                     or response.headers.get("retry-after")):
            raise ProviderRateLimit("Provider rate limit", retry_seconds(response.headers))
        if status != 200:
            raise CollectionError(f"Provider HTTP {status}")
        body = bytearray()
        for part in response.iter_bytes():
            if time.monotonic() > deadline:
                raise CollectionError("Provider response exceeded the time budget")
            body.extend(part)
            if len(body) > MAX_BODY:
                raise CollectionError("Provider response exceeds the size limit")
        return httpx.Response(status, content=bytes(body)), 'rel="next"' in response.headers.get("link", "")


def public_repository(entry):
    return entry.get("private") is False and entry.get("visibility", "public") == "public"


def linked_fix(client, repo, issue_number, fragments):
    pattern = rf"https://github\.com/{re.escape(repo)}/pull/(\d{{1,12}})\b"
    references = re.findall(pattern, "\n".join(part["text"] for part in fragments.values()), re.I)
    if not references:
        return None
    number = references[0]
    response, _ = fetch(client, f"{GITHUB}/repos/{repo}/pulls/{number}", missing=True)
    if response is None:
        return None
    pr = response.json()
    body = (pr.get("body") or "").strip()
    closes_issue = rf"\b(?:clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed))\s+(?:{re.escape(repo)})?#{issue_number}\b"
    if not pr.get("merged_at") or not re.search(closes_issue, body, re.I) or SENSITIVE.search(pr.get("title", "") + "\n" + body):
        return None
    return f"pull:{number}", {"url": f"https://github.com/{repo}/pull/{number}", "text": body[:3000],
                              "merged_fix": True}


def issue_entry(client, repo, entry, cutoff, labels_wanted, *, tracked=False):
    if "pull_request" in entry:
        return None
    issue_id = f'github:{int(entry["id"])}'
    number = int(entry["number"])
    url = f"https://github.com/{repo}/issues/{number}"
    labels = sorted(label["name"].casefold() for label in entry.get("labels", []))
    label_words = set(re.findall(r"[a-z]+", " ".join(labels)))
    title = entry["title"][:500]
    body = (entry.get("body") or "").strip()
    reason = entry.get("state_reason")
    if label_words & {"security", "vulnerability"} or SENSITIVE.search(title + "\n" + body):
        return Collected(issue_id, "Excluded issue", url, {"reason": "Security issue excluded"})
    if not entry.get("closed_at") or timestamp(entry["closed_at"]) < cutoff or entry.get("state") == "open":
        return Collected(issue_id, title, url, {"reason": "Issue reopened or expired"}) if tracked else None
    if reason in {"not_planned", "duplicate"} or label_words & {"duplicate", "wontfix", "obsolete"}:
        return Collected(issue_id, title, url, {"reason": "Issue was closed without a current resolution"}) if tracked else None
    if not body or (not tracked and not (labels_wanted & (set(labels) | label_words))):
        return Collected(issue_id, title, url, {"reason": "Issue has no usable report"}) if tracked else None
    fragments = {"body": {"url": url, "text": body[:2000]}}
    remaining = int(entry.get("comments", 0))
    if remaining:
        endpoint = f"{GITHUB}/repos/{repo}/issues/{number}/comments"
        head, _ = fetch(client, endpoint, {"per_page": 3, "page": 1})
        comments = head.json()
        if remaining > 3:
            tail, _ = fetch(client, endpoint, {"per_page": 3, "page": (remaining + 2) // 3})
            comments += tail.json()
        for comment in comments:
            value = (comment.get("body") or "").strip()
            if not value or SENSITIVE.search(value):
                continue
            comment_id = int(comment["id"])
            fragments[f"comment:{comment_id}"] = {
                "url": f"{url}#issuecomment-{comment_id}", "text": value[:25_000],
                "association": comment.get("author_association"),
            }
    fix = linked_fix(client, repo, number, fragments)
    if fix:
        fragments[fix[0]] = fix[1]
    return Collected(issue_id, title, url, {
        "repo": repo, "number": number, "closed_at": entry["closed_at"], "created_at": entry["created_at"],
        "state_reason": reason, "labels": labels, "fragments": fragments,
    })


def github_issues(client, repos, pages, max_items, cutoff, labels, tracked):
    items = {}
    retained = 0
    partial = len(tracked) > max_items
    visibility = {}

    def is_public(repo):
        key = repo.casefold()
        if key not in visibility:
            response, _ = fetch(client, f"{GITHUB}/repos/{repo}", missing=True)
            visibility[key] = response is not None and public_repository(response.json())
        return visibility[key]

    for item in tracked[:max_items]:
        repo, number = item.payload.get("repo"), item.payload.get("number")
        if not repo or not REPO_NAME.fullmatch(repo) or repo.split("/")[1] in {".", ".."} or not isinstance(number, int):
            continue
        if not is_public(repo):
            items[item.id] = Collected(item.id, "Unavailable issue", item.url, {"reason": "Issue is no longer public"})
            continue
        response, _ = fetch(client, f"{GITHUB}/repos/{repo}/issues/{number}", missing=True)
        current = issue_entry(client, repo, response.json(), cutoff, labels, tracked=True) if response is not None else None
        if current is not None and current.id == item.id:
            items[item.id] = current
        else:
            items[item.id] = Collected(item.id, item.title, item.url, {"reason": "Issue is no longer available"})
    for repo in repos:
        if not is_public(repo):
            continue
        for page in range(1, pages + 1):
            response, more = fetch(client, f"{GITHUB}/repos/{repo}/issues", {
                "state": "closed", "sort": "updated", "direction": "desc", "per_page": PAGE_SIZE, "page": page,
            })
            for entry in response.json():
                issue_id = f'github:{int(entry["id"])}'
                if issue_id in items:
                    continue
                current = issue_entry(client, repo, entry, cutoff, labels)
                if current is None or "reason" in current.payload:
                    continue
                items[issue_id] = current
                retained += int("fragments" in current.payload)
                if retained >= max_items:
                    return list(items.values()), True
            if not more:
                break
        else:
            partial = True
    return list(items.values()), partial


def repository_entry(client, entry):
    repo = entry["full_name"]
    if not REPO_NAME.fullmatch(repo) or repo.split("/")[1] in {".", ".."}:
        raise CollectionError("Provider returned an invalid repository name")
    repo_id = f'repo:{int(entry["id"])}'
    url = f"https://github.com/{repo}"
    readme, _ = fetch(client, f"{GITHUB}/repos/{repo}/readme", missing=True)
    raw = readme.json() if readme is not None else {}
    text = (base64.b64decode(raw["content"]).decode("utf-8", errors="replace").strip()[:2000]
            if raw.get("encoding") == "base64" else "")
    fragments = {"readme": {"url": url + "#readme", "text": text}} if text and not SENSITIVE.search(text) else {}
    return Collected(repo_id, repo, url, {
        "description": entry.get("description"), "stars": int(entry["stargazers_count"]),
        "forks": int(entry.get("forks_count", 0)), "language": entry.get("language"),
        "topics": entry.get("topics", []), "created_at": entry["created_at"], "pushed_at": entry.get("pushed_at"),
        "archived": bool(entry.get("archived")),
        "fragments": fragments,
        **({"reason": "Archived repository"} if entry.get("archived") else
           {"reason": "Repository has no usable README"} if not fragments else {}),
    })


def repository_trends(client, topics, pages, max_items, since, pushed_since, min_stars, tracked):
    items = {}
    partial = False
    for item in tracked[:max_items]:
        response, _ = fetch(client, f"{GITHUB}/repositories/{item.id.removeprefix('repo:')}", missing=True)
        entry = response.json() if response is not None else None
        if entry is not None and public_repository(entry):
            current = repository_entry(client, entry)
            if current.id != item.id:
                raise CollectionError("Repository identity changed during refresh")
            items[item.id] = current
        else:
            items[item.id] = Collected(item.id, item.title, item.url, {
                "stars": item.payload["stars"], "unavailable": True,
                "reason": "Repository is no longer available", "fragments": {},
            })
    new_count = 0
    for topic in topics:
        for page in range(1, pages + 1):
            response, more = fetch(client, f"{GITHUB}/search/repositories", {
                "q": f"topic:{topic} stars:>={min_stars} created:>={since} pushed:>={pushed_since} archived:false fork:false is:public",
                "sort": "stars", "order": "desc", "per_page": PAGE_SIZE, "page": page,
            })
            result = response.json()
            partial |= bool(result["incomplete_results"])
            for entry in result["items"]:
                if not public_repository(entry):
                    continue
                repo_id = f'repo:{int(entry["id"])}'
                if repo_id not in items or items[repo_id].payload.get("unavailable"):
                    items[repo_id] = repository_entry(client, entry)
                    new_count += 1
                if new_count >= max_items:
                    return list(items.values()), True
            if not more:
                break
        else:
            partial = True
    return list(items.values()), partial or len(tracked) > max_items
