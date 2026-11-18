"""Load curated interview topics without external API calls."""

import json
from pathlib import Path

from backend.db import Source, Topic, database
from backend.settings import Settings


def main():
    fixture = Path(__file__).resolve().parents[1] / "data/interview/sql_topic.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    engine, sessions = database(Settings().database_url)
    try:
        with sessions.begin() as db:
            for source in data["sources"]:
                db.merge(Source(**source))
            db.merge(Topic(**data["topic"], opening_question=data["opening_question"]))
            db.merge(Topic(
                id="background-job-failure", profession_id="python_backend", source_ids=[],
                title="Диагностика сбоя фоновой задачи",
                opening_question="Вспомните конкретный сбой фоновой задачи. По чему вы заметили проблему?",
            ))
    finally:
        engine.dispose()
    print("Seeded SQL and background job topics")


if __name__ == "__main__":
    main()
