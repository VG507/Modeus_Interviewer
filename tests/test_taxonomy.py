from backend.taxonomy import (
    SKILL_TAXONOMY,
    TAXONOMY,
    dictionary_coverage,
    skill_breakdown,
    skill_taxonomy,
    term_taxonomy,
)
from backend.textnorm import SKILL_ALIASES


class TestStructure:
    def test_all_canonical_skills_classified(self):
        _, unclassified, _ = dictionary_coverage()
        assert unclassified == set()

    def test_no_misplaced_entries(self):
        _, _, misplaced = dictionary_coverage()
        assert misplaced == set()

    def test_each_skill_in_exactly_one_place(self):
        seen = [skill for subcategories in TAXONOMY.values()
                for skills in subcategories.values() for skill in skills]
        assert len(seen) == len(set(seen))

    def test_taxonomy_covers_every_canonical_skill(self):
        assert set(SKILL_TAXONOMY) == set(SKILL_ALIASES)

    def test_categories_and_subcategories_are_non_empty(self):
        for category, subcategories in TAXONOMY.items():
            assert category and isinstance(category, str)
            for subcategory, skills in subcategories.items():
                assert subcategory and isinstance(subcategory, str)
                assert skills


class TestLookup:
    def test_canonical_skill_lookup(self):
        assert skill_taxonomy("postgresql") == ("databases", "sql")
        assert skill_taxonomy("docker") == ("devops", "containers")
        assert skill_taxonomy("pytest") == ("testing", "unit")

    def test_unknown_skill_has_no_category(self):
        assert skill_taxonomy("clickhouse") is None

    def test_term_lookup_goes_through_normalization(self):
        assert term_taxonomy("PostgrSQL") == ("databases", "sql")  # typo
        assert term_taxonomy("k8s") == ("devops", "containers")  # alias
        assert term_taxonomy("python 3.11") == ("languages", "general")  # version

    def test_garbage_and_unknown_terms_have_no_category(self):
        assert term_taxonomy("Москва") is None  # blacklisted
        assert term_taxonomy("ClickHouse") is None  # unknown skill
        assert term_taxonomy("") is None


class TestBreakdown:
    def test_vacancy_counts_once_per_category(self):
        breakdown = skill_breakdown([["postgresql", "mysql", "docker"], ["postgresql", "postgresql"], ["clickhouse"]])
        assert [group["category"] for group in breakdown] == ["databases", "devops", "other"]
        databases = breakdown[0]
        assert databases["vacancies"] == 2
        assert databases["skills"] == [
            {"skill": "postgresql", "subcategory": "sql", "vacancies": 2},
            {"skill": "mysql", "subcategory": "sql", "vacancies": 1},
        ]
        assert breakdown[2]["skills"] == [{"skill": "clickhouse", "subcategory": None, "vacancies": 1}]

    def test_other_is_last_even_when_busiest(self):
        breakdown = skill_breakdown([["clickhouse"], ["clickhouse"], ["git"]])
        assert [group["category"] for group in breakdown] == ["tools", "other"]

    def test_no_vacancies(self):
        assert skill_breakdown([]) == []
