from backend.textnorm import BLACKLIST, canonical_skill, clean_key
from backend.vacancy_parser import skill_hits


class TestCleanKey:
    def test_collapses_whitespace_and_case(self):
        assert clean_key("  Redis \n\t CE ") == "redis ce"

    def test_yo_to_e(self):
        assert clean_key("Удалённая работа") == "удаленная работа"

    def test_strips_emoji_and_symbols(self):
        assert clean_key("Python 🚀💚 developer") == "python developer"

    def test_fixes_cyrillic_homoglyphs_in_latin_tokens(self):
        assert clean_key("pуthon") == "python"  # Cyrillic "у"
        assert clean_key("docкer") == "docker"  # Cyrillic "к"

    def test_keeps_pure_cyrillic_tokens(self):
        assert clean_key("питон") == "питон"

    def test_keeps_skill_punctuation(self):
        assert clean_key("CI/CD") == "ci/cd"
        assert clean_key("Node.JS") == "node.js"
        assert clean_key("C++") == "c++"


class TestCanonicalSkill:
    def test_exact_alias(self):
        assert canonical_skill("k8s") == "kubernetes"
        assert canonical_skill("питон") == "python"
        assert canonical_skill("py3") == "python"
        assert canonical_skill("drf") == "django rest framework"

    def test_case_and_whitespace_insensitive(self):
        assert canonical_skill("  PostgreSQL  ") == "postgresql"

    def test_homoglyph_variant_resolves(self):
        assert canonical_skill("pуthon") == "python"

    def test_version_suffix_is_stripped(self):
        assert canonical_skill("python 3.11") == "python"
        assert canonical_skill("FastAPI 0.110") == "fastapi"

    def test_fuzzy_catches_typos(self):
        assert canonical_skill("PostgrSQL") == "postgresql"
        assert canonical_skill("Kubernets") == "kubernetes"
        assert canonical_skill("TypeScrip") == "typescript"

    def test_fuzzy_does_not_merge_distinct_skills(self):
        assert canonical_skill("java") == "java"
        assert canonical_skill("java") != "javascript"

    def test_fuzzy_skips_short_terms(self):
        assert canonical_skill("go") == "go"

    def test_blacklist_returns_none(self):
        assert canonical_skill("Москва") is None
        assert canonical_skill("коммуникабельность") is None
        assert canonical_skill("Удалённая работа") is None
        for word in BLACKLIST:
            assert canonical_skill(word) is None

    def test_empty_and_garbage(self):
        assert canonical_skill("") is None
        assert canonical_skill(None) is None
        assert canonical_skill("🚀🔥") is None

    def test_unknown_skills_survive_cleaned(self):
        assert canonical_skill("ClickHouse") == "clickhouse"
        assert canonical_skill("Apache Kafka") == "apache kafka"

    def test_version_and_bracket_variants(self):
        assert canonical_skill("HTML5") == "html"
        assert canonical_skill("CSS3") == "css"
        assert canonical_skill("vue3") == "vue"
        assert canonical_skill("AngularJS") == "angular"
        assert canonical_skill("Kubernetes (k8s)") == "kubernetes"
        assert canonical_skill("Postgres DB") == "postgresql"
        assert canonical_skill("Golang") == "go"

    def test_ambiguous_labels_stay_out_of_free_text(self):
        assert canonical_skill("Node") == "node.js"
        assert canonical_skill("нода") == "node.js"
        assert list(skill_hits("Kubernetes: node pool, обновление node")) == [("kubernetes", "Kubernetes")]
        assert list(skill_hits("Пишем на Node.js")) == [("node.js", "Node.js")]
