"""Compare the interviewer's questions on the same cases with and without HH context."""

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import re

from backend.db import database
from backend.interview_engine import choose_intent, formulate_question
from backend.interview_schemas import CaseState, Fact
from backend.settings import Settings
from backend.vacancy_context import sources_for_question


FRONTEND_FACTS = (
    ("task", "Страница каталога на React на телефонах загружалась 5 секунд."),
    ("action", "Я вынес тяжёлый модуль фильтров в отдельный чанк."),
    ("decision", "Потому что фильтрами пользовались редко, а модуль попадал в начальную загрузку у всех."),
)
CASES = (
    ("frontend", "Frontend React", "Медленная загрузка страницы", FRONTEND_FACTS, r"загруз\w*|открыва\w*|lcp"),
    ("python_backend", "Python backend", "Медленный отчёт PostgreSQL", (
        ("task", "Отчёт по заказам в PostgreSQL выполнялся 30 секунд."),
        ("action", "Я добавил индекс по дате заказа."),
        ("decision", "Потому что отчёт выбирал небольшой диапазон дат, а в плане был полный проход таблицы."),
    ), r"отч[её]т\w*|скорост\w*|врем[яе]ни? выполнени\w*|быстре\w*"),
    ("frontend_interaction", "Frontend React", "Медленная загрузка страницы", (*FRONTEND_FACTS,
        ("verification", "Я замерил загрузку на одном телефоне и с одинаковым соединением: до изменения 5 секунд, после 2 секунды."),
    ), r"взаимодейств\w*|отзывчив\w*|отклик\w*|клик\w*|нажати\w*"),
)


async def compare_pair(settings, topic, intent, answer, state, hint, order):
    pair = {}
    for variant in order:
        # Both variants use the same schema, instructions, and input fields.
        # Only the quote list changes; skill tags are excluded from this comparison.
        market_context = [{"task_excerpt": hint["task_excerpt"]}] if variant == "with_vacancies" else []
        result = await formulate_question(settings, topic, intent, answer, {**state, "market_context": market_context})
        pair[variant] = {"question": result.text,
                         "selected_vacancy": hint["vacancy_id"] if getattr(result, "source_number", None) == 1 else None}
    return pair


async def compare(settings, samples):
    engine, sessions = database(settings.database_url)
    results = []
    try:
        for case_id, role, topic, facts, focus in CASES:
            answer = {"text": " ".join(text for _, text in facts)}
            state = CaseState(asked_intents=["get_case"], facts=[
                Fact(id=f"fact-{index}", field=field, text=text, supporting_message_ids=["answer"])
                for index, (field, text) in enumerate(facts)
            ]).model_dump()
            state["intake"] = {"role": role}
            intent = choose_intent(state)
            with sessions() as db:
                hints = sources_for_question(db, state["intake"], state, intent)
            if not hints:
                results.append({"case": case_id, "error": "NO_RELEVANT_VACANCIES"})
                continue
            for run in range(samples):
                # Alternate the order so the same variant does not always run first.
                variants = ("baseline", "with_vacancies") if run % 2 == 0 else ("with_vacancies", "baseline")
                pair = await compare_pair(settings, topic, intent, answer, state, hints[0], variants)
                for result in pair.values():
                    result["mentions_target_focus"] = bool(re.search(focus, result["question"], re.I))
                results.append({"case": case_id, "sample": run + 1, "intent": intent, "facts": state["facts"],
                                "hint": hints[0], **pair})
    finally:
        engine.dispose()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = Settings()
    results = asyncio.run(compare(settings, args.samples))
    counts = Counter()
    for pair in results:
        if "error" in pair:
            continue
        for variant in ("baseline", "with_vacancies"):
            counts[f"{pair['case']}.{variant}"] += int(pair[variant]["mentions_target_focus"])
    report = {"model": settings.llm_model, "samples_per_case": args.samples,
              "target_focus_mentions": dict(counts), "pairs": results}
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return int(any("error" in pair for pair in results))


if __name__ == "__main__":
    raise SystemExit(main())
