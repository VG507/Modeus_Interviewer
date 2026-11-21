"""Canonical normalization for skills and technology terms.

Cleanup (Unicode, homoglyphs, emoji, versions) -> synonym map -> fuzzy fallback
against the known aliases. Raw texts and quotes stay untouched; only the
comparison key is normalized.
"""

import re
import unicodedata
from functools import lru_cache

SKILL_ALIASES = {
    "python": ("python", "python3", "py3", "пайтон", "питон"),
    "postgresql": ("postgresql", "postgres", "postgres db", "postgre", "psql", "постгрес", "постгрескл"),
    "javascript": ("javascript", "java script", "js", "джаваскрипт", "жс"),
    "typescript": ("typescript", "ts", "тайпскрипт"),
    "node.js": ("node.js", "nodejs"),
    "react": ("react.js", "reactjs", "react", "реакт"),
    "vue": ("vue.js", "vuejs", "vue", "vue3"),
    "angular": ("angular", "angularjs", "angular.js"),
    "html": ("html", "html5"),
    "css": ("css", "css3"),
    "kubernetes": ("kubernetes", "k8s", "кубернетис", "кубер"),
    "ci/cd": ("ci/cd", "ci cd", "cicd", "ci-cd"),
    "django": ("django framework", "django", "джанго"),
    "django rest framework": ("django rest framework", "drf"),
    "rest": ("rest api", "restful api", "rest"),
    **{name: (name,) for name in (
        "sql", "fastapi", "flask", "redis", "docker", "git", "graphql", "pytest",
        "selenium", "playwright", "cypress", "postman", "linux", "ansible", "terraform", "helm",
        "prometheus", "grafana", "nginx", "kafka", "rabbitmq", "celery", "mysql",
    )},
}
ALIASES = {alias: skill for skill, aliases in SKILL_ALIASES.items() for alias in aliases}
# Fine as a whole skill label, too ambiguous to search for in free text ("node pool").
LABEL_ALIASES = {"node": "node.js", "нода": "node.js", "golang": "go"}
LABELS = {**ALIASES, **LABEL_ALIASES}

# Generic words and locations that are not skills; dropped on normalization.
BLACKLIST = frozenset({
    "москва", "санкт-петербург", "питер", "новосибирск", "екатеринбург", "казань",
    "коммуникабельность", "ответственность", "стрессоустойчивость", "внимательность",
    "обучаемость", "работа в команде", "аналитическое мышление", "целеустремленность",
    "удаленная работа", "удалённая работа", "гибкий график", "дмс",
})

FUZZY_THRESHOLD = 0.90
FUZZY_MIN_LENGTH = 4

# Cyrillic look-alikes inside Latin tokens ("pуthon" -> "python").
HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y",
    "х": "x", "н": "h", "т": "t", "м": "m", "в": "b", "к": "k",
})
LATIN = re.compile(r"[a-z]")
GARBAGE = re.compile(r"[^\w+#./ -]+", re.UNICODE)
SPACES = re.compile(r"\s+")
PARENTHESES = re.compile(r"\([^)]*\)")
VERSION_SUFFIX = re.compile(r"(?<=[a-zа-я])\s+\d+(?:\.\d+){0,3}$|(?<=[a-zа-я])-(?:\d+(?:\.\d+){0,3})$")


def fix_homoglyphs(token):
    return token.translate(HOMOGLYPHS) if LATIN.search(token) else token


def clean_key(value):
    """Lowercase comparison key: Unicode NFC, no emoji, single spaces, yo->e."""
    text = unicodedata.normalize("NFC", str(value or "")).casefold().replace("ё", "е")
    text = GARBAGE.sub(" ", text)
    return SPACES.sub(" ", " ".join(fix_homoglyphs(token) for token in text.split())).strip()


def indel_similarity(left, right):
    """Return normalized insertion/deletion similarity using the longest common subsequence."""
    row = [0] * (len(right) + 1)
    for left_char in left:
        diagonal = 0
        for index, right_char in enumerate(right, 1):
            previous = row[index]
            row[index] = diagonal + 1 if left_char == right_char else max(previous, row[index - 1])
            diagonal = previous
    return 2 * row[-1] / (len(left) + len(right))


def fuzzy_skill(key):
    best_score = 0.0
    best_skill = None
    for alias, skill in LABELS.items():
        maximum_score = 2 * min(len(key), len(alias)) / (len(key) + len(alias))
        if maximum_score < FUZZY_THRESHOLD or maximum_score <= best_score:
            continue
        score = indel_similarity(key, alias)
        if score >= FUZZY_THRESHOLD and score > best_score:
            best_score, best_skill = score, skill
    return best_skill


@lru_cache(maxsize=8192)
def canonical_skill(label):
    """Canonical skill name, or None for blacklisted garbage.

    Order: cleaned alias lookup -> version-stripped lookup -> lookup without
    parentheses -> fuzzy match against known aliases. Unknown terms survive as their cleaned key.
    """
    key = clean_key(label)
    if not key or key in BLACKLIST:
        return None
    for candidate in (key, VERSION_SUFFIX.sub("", key).strip(), clean_key(PARENTHESES.sub(" ", str(label)))):
        if candidate in BLACKLIST:
            return None
        skill = LABELS.get(candidate)
        if skill:
            return skill
    if len(key) >= FUZZY_MIN_LENGTH:
        return fuzzy_skill(key) or key
    return key
