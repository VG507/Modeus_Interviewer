"""Normalize HH vacancies and keep an auditable source for each extracted field."""

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit


PROFILES = {
    "python_backend": ("Python backend", 'NAME:(Python AND (backend OR разработчик OR developer))',
                       r"\b(?:python\b|django\b|fastapi\b|flask\b)"),
    "frontend": ("Frontend", "NAME:(frontend OR front-end OR фронтенд OR react)",
                 r"\b(?:front[ -]?end\w*|фронт[ -]?енд\w*|фронтенд\w*|react)\b"),
    "qa": ("QA", "NAME:(QA OR SDET OR тестировщик OR тестирование)",
           r"\b(?:qa|sdet|quality assurance|тестиров\w*|test engineer|инженер по тестированию)\b"),
    "devops": ("DevOps", "NAME:(DevOps OR SRE OR DevSecOps OR platform)",
               r"\b(?:devops|devsecops|sre|site reliability|platform engineer|инженер платформ\w*)\b"),
}

SKILL_ALIASES = {
    "python": ("python", "пайтон"), "postgresql": ("postgresql", "postgres", "постгрес"),
    "javascript": ("javascript", "js"), "typescript": ("typescript", "ts"),
    "node.js": ("node.js", "nodejs"), "react": ("react.js", "reactjs", "react"),
    "vue": ("vue.js", "vuejs", "vue"), "kubernetes": ("kubernetes", "k8s"),
    "ci/cd": ("ci/cd", "ci cd", "cicd"),
    "django": ("django framework", "django"),
    "django rest framework": ("django rest framework", "drf"),
    "rest": ("rest api", "restful api", "rest"),
    **{name: (name,) for name in (
        "sql", "fastapi", "flask", "redis", "docker", "git", "graphql", "pytest",
        "selenium", "playwright", "cypress", "postman", "linux", "ansible", "terraform", "helm",
        "prometheus", "grafana", "nginx", "kafka", "rabbitmq", "celery", "mysql", "angular", "html", "css",
    )},
}
ALIASES = {alias: skill for skill, aliases in SKILL_ALIASES.items() for alias in aliases}
SKILL_PATTERN = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(alias) for alias in sorted(ALIASES, key=len, reverse=True)) + r")(?!\w)",
    re.IGNORECASE,
)
LEVELS = {
    "junior": r"\b(?:junior|jr\.?|джуниор\w*|младш\w*|стаж[её]р\w*)\b",
    "middle": r"\b(?:middle|мид[д]?л\w*)\b",
    "senior": r"\b(?:senior|sr\.?|сеньор\w*|старш\w*)\b",
    "lead": r"\b(?:lead|leadership|лид\w*|ведущ\w*)\b",
}
EXPERIENCE = ("noExperience", "between1And3", "between3And6", "moreThan6")
SECTION_HEADINGS = tuple((field, re.compile(pattern, re.IGNORECASE)) for field, pattern in (
    ("tasks", r"(?:ваши |твои |основные |рабочие |ключевые |будущие )?(?:обязанности|задачи)(?: в этой роли| на этой позиции)?|(?:вам |тебе )?предстоит|чем (?:вам |тебе )?предстоит заниматься|что предстоит(?: делать(?: и за что отвечать)?)?|что (?:нужно|будешь|будете) делать|чем вы будете заниматься|из чего будет состоять работа|(?:конкретный перечень )?задач[иь]?, которые предстоит (?:выполнять|реализовать)|responsibilities|what you will do"),
    ("requirements", r"(?:наши |основные )?требования(?: к кандидату)?|наши ожидания|пожелания к опыту|(?:что )?мы (?:ожидаем|жд[её]м)(?: от тебя| от вас)?|что жд[её]м от тебя|что важно|для нас важно|для нас знать, что ты имеешь|кого мы ищем|будет плюсом|будет преимуществом|(?:обязательные|желательные) навыки|hard skills|requirements|qualifications|nice to have"),
    ("conditions", r"условия(?: работы)?|важные условия перед откликом|что мы предлагаем|мы предлагаем|предлагаем|что мы да[её]м|что мы обеспечива(?:ем|им)|преимущества? работы с нами|как сделаем жизнь комфортнее|почему стоит выбрать работу у нас|benefits|what we offer"),
    (None, r"о компании|о команде|о проекте|процесс найма|о нас|(?:основной |наш )?ст[еэ]к(?: технологий)?|about us|about the team"),
))
LINE_SECTIONS = tuple((field, re.compile(pattern, re.IGNORECASE)) for field, pattern in (
    ("requirements", r"^(?:опыт|(?:уверенное |хорошее )?(?:знание|владение)|знать|умение|требуется|work experience)\b"),
    ("conditions", r"^(?:дмс|оклад|зарплата|премии|отпуск|график работы|формат работы|удал[её]нная работа|оформление|(?:официальное )?трудоустройство|соцпакет)\b"),
))


class Description(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden = tag
        if tag in {"p", "li", "br", "div", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == self.hidden:
            self.hidden = None
        if tag in {"p", "li", "div", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.hidden is None:
            self.parts.append(data)


def clean_html(value):
    parser = Description()
    parser.feed(str(value or "")[:60_000])
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)[:30_000]


def plain(value):
    return " ".join(clean_html(value).split())


def skill_hits(text):
    for match in SKILL_PATTERN.finditer(text):
        quote = match.group()
        skill = ALIASES.get(quote.casefold())
        if skill:
            yield skill, quote


def canonical_skill(label):
    label = label.casefold()
    return ALIASES.get(label, label)


def task_key(text):
    return " ".join(re.findall(r"\w+", text.casefold().replace("ё", "е")))


def overlaps_task(snippet, task):
    if snippet == task:
        return True
    shorter, longer = sorted((snippet, task), key=len)
    return len(shorter.split()) >= 3 and f" {shorter} " in f" {longer} "


def extract_sections(description, responsibility="", requirement=""):
    sections = {"tasks": [], "requirements": [], "conditions": []}
    section = None
    for line in description.splitlines():
        heading, separator, tail = line.partition(":")
        for field, pattern in SECTION_HEADINGS:
            if pattern.fullmatch(heading.strip().strip("!?;")):
                section = field
                line = tail.strip() if separator else ""
                break
        words = line.lstrip("-–—• ")
        destination = next((field for field, pattern in LINE_SECTIONS if pattern.search(words)), section)
        if destination is not None and line and len(sections[destination]) < 4:
            quote = line[:500]
            if not any(task_key(quote) == task_key(existing) for existing, _ in sections[destination]):
                sections[destination].append((quote, "description"))
    for field, snippet, source in (("tasks", responsibility, "snippet.responsibility"),
                                    ("requirements", requirement, "snippet.requirement")):
        items = sections[field]
        if snippet and len(items) < 4 and not any(overlaps_task(task_key(snippet), task_key(text)) for text, _ in items):
            items.append((snippet[:240], source))
    return sections


def replace_vacancy_sections(card, responsibility="", requirement=""):
    sections = extract_sections(card["description"], responsibility, requirement)
    return with_sections(card, sections)


def with_sections(card, sections):
    evidence = [item for item in card["evidence"] if item["field_path"].split(".", 1)[0] not in sections]
    for field, items in sections.items():
        for index, (quote, source) in enumerate(items):
            evidence.append({"field_path": f"{field}.{index}", "source_field": source, "quote": quote,
                             "vacancy_id": card["id"], "url": card["url"]})
    return {**card, **{field: [quote for quote, _ in items] for field, items in sections.items()}, "evidence": evidence}


def role_key(role):
    return " ".join(re.findall(r"\w+[+#]*", role.casefold().replace("ё", "е").replace("_", " ")))


def profile_matches(profession_id, role, profile=None):
    if profile is not None and profile.aliases is not None:
        normalized = f" {role_key(role)} "
        return any(key and f" {key} " in normalized for key in map(role_key, [profile.title, *profile.aliases]))
    default = PROFILES.get(profession_id)
    return bool(default and re.search(default[2], role, re.IGNORECASE))


def level_of(text):
    matches = [key for key, pattern in LEVELS.items() if re.search(pattern, text, re.IGNORECASE)]
    return matches[0] if len(matches) == 1 else None


def identity(item):
    external_id = str(item.get("id") or "").strip()
    if external_id.isascii() and external_id.isdigit() and len(external_id) <= 24:
        return external_id, f"https://hh.ru/vacancy/{external_id}"
    url = urlsplit(str(item.get("alternate_url") or ""))
    match = re.fullmatch(r"/vacancy/([0-9]{1,24})/?", url.path)
    if url.scheme == "https" and url.hostname and (url.hostname == "hh.ru" or url.hostname.endswith(".hh.ru")) and match:
        return None, f"https://hh.ru/vacancy/{match[1]}"
    return None, None


def parse_vacancy(item, profession_id, profile=None):
    external_id, url = identity(item)
    if not url:
        return None
    card = build_vacancy(item, profession_id, profile, external_id or url.rsplit("/", 1)[-1], url)
    if card:
        card["hh_id"] = external_id
    return card


def build_vacancy(item, profession_id, profile, vacancy_id, url):
    title = plain(item.get("name"))[:240]
    description = clean_html(item.get("description"))
    if not url or not title or not description or item.get("archived") is True:
        return None
    skills = {}
    evidence = []

    def cite(field, source, quote):
        evidence.append({"field_path": field, "source_field": source, "quote": quote,
                         "vacancy_id": vacancy_id, "url": url})

    def add_skill(skill, source, quote):
        if not skill or (skill not in skills and len(skills) >= 50):
            return
        sources = skills.setdefault(skill, set())
        if (source, quote.casefold()) not in sources and len(sources) < 5:
            sources.add((source, quote.casefold()))
            cite(f"skills.{skill}", source, quote)

    for index, item_skill in enumerate(item.get("key_skills") or []):
        label = plain(item_skill.get("name"))[:120]
        add_skill(canonical_skill(label), f"key_skills[{index}].name", label)
    snippet = item.get("snippet") or {}
    for field, value in (("description", description),
                         ("snippet.requirement", plain(snippet.get("requirement"))),
                         ("snippet.responsibility", plain(snippet.get("responsibility")))):
        for skill, quote in skill_hits(value):
            add_skill(skill, field, quote)

    matches_title = profile_matches(profession_id, title, profile)
    if profession_id == "python_backend" and (profile is None or profile.aliases is None):
        matches_title = matches_title or ("python" in skills and re.search(r"backend|б[эе]кенд", title, re.I))
        if re.search(r"\b(?:frontend|аналитик|data scientist|data engineer|преподаватель|тестировщик)\b", title, re.I):
            matches_title = False
    if not matches_title:
        return None

    experience = (item.get("experience") or {}).get("id")
    if experience not in EXPERIENCE:
        experience = None
    else:
        cite("experience_band", "experience.id", experience)
    level = level_of(title)
    if level:
        cite("level", "name", title)
    card = {"id": vacancy_id, "url": url, "profession_id": profession_id,
            "title": title, "description": description, "skills": list(skills),
            "level": level, "experience_band": experience, "evidence": evidence,
            "published_at": item.get("published_at"), "area_id": (item.get("area") or {}).get("id"),
            "employer_id": (item.get("employer") or {}).get("id")}
    return replace_vacancy_sections(card, plain(snippet.get("responsibility")), plain(snippet.get("requirement")))
