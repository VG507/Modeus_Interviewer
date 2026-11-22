"""Two-level taxonomy over canonical skills: category -> subcategory -> skills.

Applied on top of normalization: raw terms go through canonical_skill first,
then the canonical skill is looked up here. Unknown skills get no category.
"""

from collections import Counter, defaultdict

from backend.textnorm import SKILL_ALIASES, canonical_skill

OTHER = "other"

TAXONOMY = {
    "languages": {
        "general": ("python", "javascript", "typescript", "go"),
    },
    "backend": {
        "frameworks": ("django", "fastapi", "flask"),
        "api": ("django rest framework", "rest", "graphql", "grpc"),
        "runtimes": ("node.js",),
    },
    "frontend": {
        "frameworks": ("react", "vue", "angular"),
        "markup": ("html", "css"),
    },
    "databases": {
        "sql": ("postgresql", "mysql"),
        "nosql": ("redis", "mongodb"),
        "analytics": ("clickhouse",),
        "search": ("elasticsearch",),
        "query": ("sql",),
    },
    "messaging": {
        "brokers": ("kafka", "rabbitmq", "redis streams", "redis pub/sub"),
        "task_queues": ("celery",),
    },
    "devops": {
        "containers": ("docker", "kubernetes", "helm"),
        "iac": ("terraform", "ansible"),
        "systems": ("linux", "nginx"),
        "observability": ("prometheus", "grafana"),
        "cicd": ("ci/cd",),
    },
    "testing": {
        "unit": ("pytest",),
        "e2e": ("selenium", "playwright", "cypress"),
        "api": ("postman",),
    },
    "tools": {
        "vcs": ("git",),
    },
}

SKILL_TAXONOMY = {
    skill: (category, subcategory)
    for category, subcategories in TAXONOMY.items()
    for subcategory, skills in subcategories.items()
    for skill in skills
}


def skill_taxonomy(skill):
    """(category, subcategory) for a canonical skill, or None."""
    return SKILL_TAXONOMY.get(skill)


def term_taxonomy(raw):
    """(category, subcategory) for a raw term, normalized first; None if unknown."""
    canonical = canonical_skill(raw)
    return SKILL_TAXONOMY.get(canonical) if canonical else None


def skill_breakdown(vacancy_skills):
    """Vacancy counts per category and per skill, from one canonical skill list per vacancy.

    A vacancy counts once per category it touches. Skills outside the taxonomy go
    to "other". Busiest category first, "other" always last; skills likewise.
    """
    vacancies, skills = defaultdict(set), defaultdict(Counter)
    for index, vacancy in enumerate(vacancy_skills):
        for skill in set(vacancy):
            category = SKILL_TAXONOMY.get(skill, (OTHER,))[0]
            vacancies[category].add(index)
            skills[category][skill] += 1
    return [{"category": category, "vacancies": len(vacancies[category]),
             "skills": [{"skill": skill, "subcategory": SKILL_TAXONOMY.get(skill, (None, None))[1], "vacancies": count}
                        for skill, count in sorted(skills[category].items(), key=lambda item: (-item[1], item[0]))]}
            for category in sorted(vacancies, key=lambda name: (name == OTHER, -len(vacancies[name]), name))]


def dictionary_coverage():
    """(classified, unclassified, misplaced) canonical skill sets.

    classified: canonical skills present in the taxonomy.
    unclassified: canonical skills missing from the taxonomy.
    misplaced: taxonomy entries that are not canonical skills (config error).
    """
    known = set(SKILL_ALIASES)
    classified = known & SKILL_TAXONOMY.keys()
    return classified, known - classified, set(SKILL_TAXONOMY) - known
