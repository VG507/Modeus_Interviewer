"""Load and extract small, explicitly selected sections of official documentation."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from time import monotonic
from urllib.parse import quote, urljoin

import httpx

from backend.discovery_fetch import CollectionError


@dataclass(frozen=True)
class Target:
    title: str
    technology: str
    url: str
    section: str = ""
    version: str = ""


TARGETS = {
    "postgresql-explain": Target("PostgreSQL 16 · планы запросов", "PostgreSQL",
        "https://www.postgresql.org/docs/16/using-explain.html", "Using EXPLAIN", "16"),
    "sqlalchemy-pooling": Target("SQLAlchemy 2.0 · разрывы соединений", "SQLAlchemy",
        "https://docs.sqlalchemy.org/en/20/core/pooling.html", "Dealing with Disconnects", "2.0"),
    "celery-retries": Target("Celery stable · повторные попытки", "Celery",
        "https://docs.celeryq.dev/en/stable/userguide/tasks.html", "Retrying"),
    "postgresql-releases": Target("PostgreSQL 16 · релизы и поддержка", "PostgreSQL",
        "https://www.postgresql.org/support/versioning/", version="16"),
    "sqlalchemy-releases": Target("SQLAlchemy 2.0 · релизы", "SQLAlchemy",
        "https://docs.sqlalchemy.org/en/20/changelog/changelog_20.html", version="2.0"),
    "celery-releases": Target("Celery stable · релизы", "Celery",
        "https://docs.celeryq.dev/en/stable/changelog.html"),
}

VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
BLOCKS = frozenset("p div li dt dd tr td th pre section article blockquote br dl ul ol table".split())
IGNORED = frozenset("nav aside script style svg footer form button".split())
IGNORED_CLASSES = frozenset("headerlink id_link toc toctree-wrapper related sidebar sphinxsidebar navheader navfooter "
                            "breadcrumb breadcrumbs header-logo global-nav".split())
NUMBERING = re.compile(r"^(?:[A-Z]|\d+)(?:\.\d+)*\.\s+")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
MAX_BYTES = 4 * 1024 * 1024


@dataclass
class Heading:
    level: int
    anchor: str
    title: str = ""
    text: list[str] = field(default_factory=list)

    def content(self):
        parts = "".join(self.text).split("\x00")
        parts[::2] = ["\n\n".join(line for line in (" ".join(line.split()) for line in part.splitlines()) if line)
                      for part in parts[::2]]
        return "\n\n".join(part.strip("\n") for part in parts if part.strip())


class DocumentHTML(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.headings = []
        self.title = ""
        self.rows = []
        self.row = None
        self.cell = None
        self.current = None
        self.feed(html)
        self.close()
        for heading in self.headings:
            heading.title = NUMBERING.sub("", " ".join(heading.title.split())).rstrip(" #¶")

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        classes = set((attrs.get("class") or "").split())
        hidden = ((self.stack[-1][1] if self.stack else False) or tag in IGNORED
                  or bool(IGNORED_CLASSES & classes) or "hidden" in attrs)
        lead = re.fullmatch(r"h([1-6])", tag)
        anchor = attrs.get("id") or attrs.get("name") or ""
        if not hidden:
            if lead:
                parent_id = next((id_ for node, _, id_ in reversed(self.stack)
                                  if node in {"section", "div"} and id_), "")
                self.current = Heading(int(lead[1]), anchor or parent_id)
                self.headings.append(self.current)
            if tag == "tr":
                self.row = []
            if tag in {"td", "th"} and self.row is not None:
                self.cell = []
            if tag in BLOCKS and self.current:
                self.current.text.append("\x00" if tag == "pre" else "\n")
        if tag == "a" and self.current and any(node in {"h1", "h2", "h3", "h4", "h5", "h6"}
                                              for node, _, _ in self.stack):
            href = attrs.get("href") or ""
            if anchor or ({"headerlink", "id_link"} & classes and href.startswith("#") and len(href) > 1):
                self.current.anchor = anchor or href[1:]
        if tag not in VOID:
            self.stack.append((tag, hidden, anchor))

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        position = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if position is None:
            return
        _, hidden, _ = self.stack[position]
        del self.stack[position:]
        if hidden:
            return
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = self.cell = None
        if tag in BLOCKS and self.current:
            self.current.text.append("\x00" if tag == "pre" else "\n")

    def handle_data(self, data):
        if not self.stack or self.stack[-1][1]:
            return
        tags = {tag for tag, _, _ in self.stack}
        if "title" in tags:
            self.title += data
        if self.cell is not None:
            self.cell.append(data)
        if not self.current:
            return
        if tags & {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.current.title += data
        elif "pre" in tags:
            self.current.text.append(data.replace("\r\n", "\n").replace("\x00", ""))
        elif not tags & {"head", "title"}:
            self.current.text.append(re.sub(r"\s+", " ", data.replace("\x00", "")))


def load_html(client, url):
    deadline = monotonic() + 10
    with client.stream("GET", url) as response:
        if response.status_code in {404, 410}:
            return None
        response.raise_for_status()
        if response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/html":
            raise CollectionError("Официальный сайт вернул ответ в неожиданном формате.")
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=16384):
            body.extend(chunk)
            if len(body) > MAX_BYTES or monotonic() > deadline:
                raise CollectionError("Превышен лимит размера или времени загрузки документации.")
        return DocumentHTML(body.decode(response.encoding or "utf-8", errors="replace"))


def postgresql_release(client):
    url = TARGETS["postgresql-releases"].url
    page = load_html(client, url)
    row = next((row for row in page.rows if len(row) >= 5 and row[0] == "16"), None) if page else None
    if row is None or row[2] not in {"Yes", "No"} or not re.fullmatch(r"16\.\d+", row[1]):
        raise CollectionError("Не удалось прочитать статус поддержки PostgreSQL 16.")
    return row[1], row[2] == "Yes", row[4]


def section_url(page_url, anchor):
    return f"{page_url}#{quote(anchor, safe='-_.:')}" if anchor else page_url


def page_version(target, page):
    expected = {
        "PostgreSQL": r"PostgreSQL\s*16\b|Documentation:\s*16:",
        "SQLAlchemy": r"SQLAlchemy\s+2\.0\b",
        "Celery": r"Celery\s+(\d+\.\d+(?:\.\d+)?)\s+documentation",
    }
    match = re.search(expected[target.technology], page.title, re.IGNORECASE)
    if not match:
        raise CollectionError("Не удалось определить версию официальной документации.")
    return target.version or match[1]


def document_sections(client, target):
    page = load_html(client, target.url)
    if page is None:
        return {}, "Официальная страница недоступна (404/410)."
    version = page_version(target, page)
    supported = postgresql_release(client)[1] if target.technology == "PostgreSQL" else None
    start = next((i for i, heading in enumerate(page.headings)
                  if heading.title.casefold() == target.section.casefold()), None)
    if start is None:
        return {}, "Раздел исчез с официальной страницы."
    root = page.headings[start]
    result = {}
    for heading in page.headings[start:]:
        if heading is not root and heading.level <= root.level:
            break
        content = heading.content()
        if not content:
            continue
        if len(content) > 150_000 or len(result) >= 50:
            raise CollectionError("Объём отслеживаемого раздела превышает лимит.")
        key = "root" if heading is root else heading.anchor or heading.title
        if key in result:
            raise CollectionError("На странице обнаружены неоднозначные заголовки.")
        result[key] = (heading.title, section_url(target.url, heading.anchor), {
            "text": content, "version": version, "supported": supported,
        })
    if not result:
        raise CollectionError("В разделе не удалось извлечь текст.")
    return result, "Подраздел исчез с официальной страницы."


def release_notes(client, target):
    if target.technology == "PostgreSQL":
        version, supported, end = postgresql_release(client)
        url = urljoin(target.url, f"/docs/16/release-{version.replace('.', '-')}.html")
        policy = f"Поддержка ветки 16 до {end}. Статус: {'активна' if supported else 'завершена'}."
        page = load_html(client, url)
        title = f"Release {version}"
        if not page or not any(heading.title == title for heading in page.headings):
            raise CollectionError("Не удалось прочитать журнал релиза PostgreSQL.")
    else:
        page = load_html(client, target.url)
        if not page:
            raise CollectionError("Официальный журнал релизов недоступен.")
        page_version(target, page)
        policy, url, title = "", target.url, None
    for index, heading in enumerate(page.headings):
        if not (heading.title == title if title else VERSION.fullmatch(heading.title)):
            continue
        content = heading.content()
        if "no release date" in content.lower():
            continue
        published = publication_date(content)
        if not published:
            continue
        lines = [line for line in (policy, content) if line]
        for child in page.headings[index + 1:]:
            if child.level <= heading.level:
                break
            lines.extend((child.title, child.content()))
        version = heading.title.removeprefix("Release ")
        return version, section_url(url, heading.anchor), "\n\n".join(lines)[:24_000], published
    raise CollectionError("Не удалось найти опубликованный релиз.")


def publication_date(text):
    stamp = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if stamp:
        return date.fromisoformat(stamp[0]).isoformat()
    stamp = re.search(r"Released:\s*([A-Z][a-z]+ \d{1,2}, \d{4})\b", text)
    if stamp:
        return datetime.strptime(stamp[1], "%B %d, %Y").date().isoformat()
    return None
