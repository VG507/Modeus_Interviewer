"""Canonical normalization for skills and technology terms.

Cleanup (Unicode, homoglyphs, emoji, versions) -> synonym map -> fuzzy fallback
against the known aliases. Raw texts and quotes stay untouched; only the
comparison key is normalized.
"""

import re
import unicodedata
from functools import lru_cache

from rapidfuzz import fuzz, process

SKILL_ALIASES = {
    "python": ("python", "python3", "py3", "пайтон", "питон"),
    "postgresql": ("postgresql", "postgres", "postgre", "psql", "постгрес", "постгрескл"),
    "javascript": ("javascript", "java script", "js", "джаваскрипт", "жс"),
    "typescript": ("typescript", "ts", "тайпскрипт"),
    "node.js": ("node.js", "nodejs", "node", "нода"),
    "react": ("react.js", "reactjs", "react", "реакт"),
    "vue": ("vue.js", "vuejs", "vue"),
    "kubernetes": ("kubernetes", "k8s", "кубернетис", "кубер"),
    "ci/cd": ("ci/cd", "ci cd", "cicd", "ci-cd"),
    "django": ("django framework", "django", "джанго"),
    "django rest framework": ("django rest framework", "drf"),
    "rest": ("rest api", "restful api", "rest"),
    **{name: (name,) for name in (
        "sql", "fastapi", "flask", "redis", "docker", "git", "graphql", "pytest",
        "selenium", "playwright", "cypress", "postman", "linux", "ansible", "terraform", "helm",
        "prometheus", "grafana", "nginx", "kafka", "rabbitmq", "celery", "mysql", "angular", "html", "css",
    )},
}
ALIASES = {alias: skill for skill, aliases in SKILL_ALIASES.items() for alias in aliases}

# Generic words and locations that are not skills; dropped on normalization.
BLACKLIST = frozenset({
    "москва", "санкт-петербург", "питер", "новосибирск", "екатеринбург", "казань",
    "коммуникабельность", "ответственность", "стрессоустойчивость", "внимательность",
    "обучаемость", "работа в команде", "аналитическое мышление", "целеустремленность",
    "удаленная работа", "удалённая работа", "гибкий график", "дмс",
})

FUZZY_THRESHOLD = 90
FUZZY_MIN_LENGTH = 4

# Cyrillic look-alikes inside Latin tokens ("pуthon" -> "python").
HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y",
    "х": "x", "н": "h", "т": "t", "м": "m", "в": "b", "к": "k",
})
LATIN = re.compile(r"[a-z]")
GARBAGE = re.compile(r"[^\w+#./ -]+", re.UNICODE)
SPACES = re.compile(r"\s+")
VERSION_SUFFIX = re.compile(r"(?<=[a-zа-я])\s+\d+(?:\.\d+){0,3}$|(?<=[a-zа-я])-(?:\d+(?:\.\d+){0,3})$")


def fix_homoglyphs(token):
    return token.translate(HOMOGLYPHS) if LATIN.search(token) else token


def clean_key(value):
    """Lowercase comparison key: Unicode NFC, no emoji, single spaces, yo->e."""
    text = unicodedata.normalize("NFC", str(value or "")).casefold().replace("ё", "е")
    text = GARBAGE.sub(" ", text)
    return SPACES.sub(" ", " ".join(fix_homoglyphs(token) for token in text.split())).strip()


@lru_cache(maxsize=8192)
def canonical_skill(label):
    """Canonical skill name, or None for blacklisted garbage.

    Order: cleaned alias lookup -> version-stripped lookup -> fuzzy match
    against known aliases. Unknown terms survive as their cleaned key.
    """
    key = clean_key(label)
    if not key or key in BLACKLIST:
        return None
    for candidate in (key, VERSION_SUFFIX.sub("", key).strip()):
        if candidate in BLACKLIST:
            return None
        skill = ALIASES.get(candidate)
        if skill:
            return skill
    if len(key) >= FUZZY_MIN_LENGTH:
        match = process.extractOne(key, ALIASES.keys(), scorer=fuzz.ratio, score_cutoff=FUZZY_THRESHOLD)
        if match:
            return ALIASES[match[0]]
    return key
