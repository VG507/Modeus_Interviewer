"""Evaluate skill normalization against a gold set of raw -> expected pairs.

Precision: of the pairs we mapped to a canonical skill, how many are correct.
Recall: of the pairs that should map to a canonical skill, how many we got.
Rejection accuracy: of the garbage pairs, how many were dropped (None).

Usage: .venv/Scripts/python.exe scripts/evaluate_textnorm.py [--threshold 0.9]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.taxonomy import dictionary_coverage
from backend.textnorm import canonical_skill

GOLD = [
    # (raw, expected canonical skill or None for garbage)
    ("Python", "python"),
    ("python3", "python"),
    ("py3", "python"),
    ("питон", "python"),
    ("Пайтон", "python"),
    ("pуthon", "python"),  # Cyrillic "у"
    ("python 3.11", "python"),
    ("Python 3.9", "python"),
    ("PostgreSQL", "postgresql"),
    ("postgres", "postgresql"),
    ("постгрес", "postgresql"),
    ("PostgrSQL", "postgresql"),  # typo
    ("postgre", "postgresql"),
    ("psql", "postgresql"),
    ("JavaScript", "javascript"),
    ("JS", "javascript"),
    ("TypeScript", "typescript"),
    ("TS", "typescript"),
    ("TypeScrip", "typescript"),  # typo
    ("Node.js", "node.js"),
    ("NodeJS", "node.js"),
    ("React", "react"),
    ("React.JS", "react"),
    ("reactjs", "react"),
    ("Vue.js", "vue"),
    ("Kubernetes", "kubernetes"),
    ("k8s", "kubernetes"),
    ("Kubernets", "kubernetes"),  # typo
    ("кубер", "kubernetes"),
    ("Docker", "docker"),
    ("Doker", "docker"),  # typo
    ("CI/CD", "ci/cd"),
    ("CI-CD", "ci/cd"),
    ("cicd", "ci/cd"),
    ("Django", "django"),
    ("джанго", "django"),
    ("DRF", "django rest framework"),
    ("Django REST Framework", "django rest framework"),
    ("FastAPI", "fastapi"),
    ("FastAPI 0.110", "fastapi"),
    ("fast api", "fastapi"),
    ("Flask", "flask"),
    ("Redis", "redis"),
    ("Git", "git"),
    ("GraphQL", "graphql"),
    ("REST API", "rest"),
    ("restful api", "rest"),
    ("Kafka", "kafka"),
    ("Apache Kafka", "apache kafka"),
    ("RabbitMQ", "rabbitmq"),
    ("Linux", "linux"),
    ("Ansible", "ansible"),
    ("Terraform", "terraform"),
    ("Nginx", "nginx"),
    ("Grafana", "grafana"),
    ("Prometheus", "prometheus"),
    ("MySQL", "mysql"),
    ("Angular", "angular"),
    ("HTML", "html"),
    ("CSS", "css"),
    ("SQL", "sql"),
    ("Pytest", "pytest"),
    ("Selenium", "selenium"),
    ("Celery", "celery"),
    ("ClickHouse", "clickhouse"),  # unknown skill survives cleaned
    ("Golang", "golang"),  # unknown skill survives cleaned
    ("go", "go"),  # too short for fuzzy, kept as-is
    ("java", "java"),  # must NOT merge into javascript
    # Garbage that must be rejected
    ("Москва", None),
    ("Санкт-Петербург", None),
    ("коммуникабельность", None),
    ("ответственность", None),
    ("стрессоустойчивость", None),
    ("работа в команде", None),
    ("Удалённая работа", None),
    ("гибкий график", None),
    ("", None),
    ("🚀", None),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=0.9,
                        help="minimum weighted accuracy to exit 0 (default: 0.9)")
    args = parser.parse_args()

    mapped_ok = mapped_total = 0
    expected_ok = expected_total = 0
    rejected_ok = rejected_total = 0
    errors = []
    for raw, expected in GOLD:
        got = canonical_skill(raw)
        if expected is None:
            rejected_total += 1
            rejected_ok += got is None
        else:
            expected_total += 1
            expected_ok += got == expected
            mapped_total += 1
            mapped_ok += got == expected
        if got != expected:
            errors.append(f"  {raw!r}: expected {expected!r}, got {got!r}")

    precision = mapped_ok / mapped_total if mapped_total else 1.0
    recall = expected_ok / expected_total if expected_total else 1.0
    rejection = rejected_ok / rejected_total if rejected_total else 1.0
    accuracy = (mapped_ok + rejected_ok) / len(GOLD)

    classified, unclassified, misplaced = dictionary_coverage()
    taxonomy_total = len(classified) + len(unclassified)

    print(f"gold pairs:        {len(GOLD)}")
    print(f"precision (mapped): {precision:.3f}  ({mapped_ok}/{mapped_total})")
    print(f"recall (expected):  {recall:.3f}  ({expected_ok}/{expected_total})")
    print(f"rejection (garbage):{rejection:.3f}  ({rejected_ok}/{rejected_total})")
    print(f"overall accuracy:   {accuracy:.3f}")
    print(f"taxonomy coverage:  {len(classified)}/{taxonomy_total} canonical skills classified")
    if unclassified:
        print(f"  unclassified: {sorted(unclassified)}")
    if misplaced:
        print(f"  misplaced taxonomy entries (not canonical skills): {sorted(misplaced)}")
    if errors:
        print("mismatches:")
        print("\n".join(errors))
    if accuracy < args.threshold:
        print(f"FAIL: accuracy {accuracy:.3f} < threshold {args.threshold}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
