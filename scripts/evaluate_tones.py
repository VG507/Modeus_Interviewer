"""Compare live interview questions across tones with the same synthetic inputs."""

import asyncio
import re
from unittest.mock import patch

import httpx

from backend import interview_engine
from backend.discovery_fetch import is_dns_failure
from backend.interview_schemas import CaseState
from backend.settings import Settings


CASES = (
    ("Python Backend", "Ускорение отчётов", "decision",
     "Отчёт в PostgreSQL строился минуту. Я добавил составной индекс."),
    ("QA", "Нестабильные автотесты", "action",
     "Автотесты оплаты проходили локально, но иногда падали в CI."),
    ("DevOps", "Задержка сервиса", "verification",
     "После релиза выросла задержка API. Я откатил релиз."),
)
LABELS = {"friendly": "Дружелюбный", "business": "Деловой", "playful": "Шутливый"}


async def evaluate(settings, role, topic, intent, answer, tone):
    state = {**CaseState().model_dump(), "intake": {"role": role, "tone": tone}}
    ask = interview_engine.ask_question
    generated = None

    async def capture(*args, **kwargs):
        nonlocal generated
        generated = await ask(*args, **kwargs)
        return generated

    with patch.object(interview_engine, "ask_question", capture):
        question = await interview_engine.draft_question(settings, topic, intent, {"text": answer}, state)
    return question.text, generated is question


async def main():
    settings = Settings(llm_cache=False)
    answered = 0
    total = len(CASES) * len(LABELS)
    for role, topic, intent, answer in CASES:
        print(f"\n{role} · {topic}\n{answer}", flush=True)
        live_texts = []
        for tone, label in LABELS.items():
            try:
                question, live = await evaluate(settings, role, topic, intent, answer, tone)
            except (httpx.HTTPError, RuntimeError) as exc:
                reason = "ошибка DNS" if is_dns_failure(exc) else type(exc).__name__
                print(f"{label} · {reason}", flush=True)
                continue
            print(f"{label} · {'модель' if live else 'шаблон'} · {question}", flush=True)
            if live:
                answered += 1
                live_texts.append(re.sub(r"[^\w]+", " ", question.casefold().replace("ё", "е")).strip())
        print(f"Разных формулировок модели: {len(set(live_texts))} из {len(live_texts)}")
    print(f"\nОтветов модели: {answered} из {total}")
    return int(answered != total)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
