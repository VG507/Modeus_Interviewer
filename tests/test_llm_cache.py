import asyncio
import json

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from backend import interview_engine, llm
from backend.interview_schemas import CaseState
from backend.settings import Settings

SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
          "additionalProperties": False}


@pytest.fixture
def provider(database_url, monkeypatch):
    url = database_url
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    requests = []
    answers = []

    def upstream(request):
        body = json.loads(request.content)
        requests.append(body)
        content = answers.pop(0) if answers else {"text": f"ответ {len(requests)}"}
        if isinstance(content, Exception):
            raise content
        if isinstance(content, int):
            return httpx.Response(content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient",
                        lambda **options: real_client(transport=httpx.MockTransport(upstream), **options))

    def settings(**options):
        return Settings(**{"database_url": url, "llm_base_url": "http://llm.test/v1", "llm_model": "test-model",
                           "llm_api_key": "", **options})

    return settings, requests, answers


def ask(settings, instructions="Задай вопрос.", payload=None, **options):
    return asyncio.run(llm.structured_completion(settings, "question", SCHEMA, instructions,
                                                 payload or {"answer": "Добавил индекс."}, **options))


def test_same_request_gets_the_stored_answer(provider):
    settings, requests, _ = provider
    first = ask(settings())
    assert ask(settings()) == first
    # Spacing and Unicode form do not make a request new.
    assert ask(settings(), payload={"answer": "  Добавил   индекс. "}) == first
    assert ask(settings(), payload={"answer": "Добавил индекс.".replace("й", "й")}) == first
    assert len(requests) == 1

    # A changed prompt, model or sampling option asks the model again.
    assert ask(settings(), instructions="Задай другой вопрос.") != first
    assert ask(settings(llm_model="other-model")) != first
    assert ask(settings(llm_temperature=1)) != first
    assert len(requests) == 4


def test_rejected_answer_is_asked_again(provider):
    settings, requests, answers = provider
    answers.extend([{"text": "без вопроса"}, {"text": "Что вы сделали?"}])

    def question(raw):
        if not raw["text"].endswith("?"):
            raise ValueError("not a question")
        return raw["text"]

    # The malformed answer is asked again at once, and only the good one is stored.
    assert ask(settings(), parse=question) == "Что вы сделали?"
    assert ask(settings(), parse=question) == "Что вы сделали?"
    assert len(requests) == 2
    answers.extend([{"text": "нет"}, {"text": "снова нет"}])
    with pytest.raises(ValueError):
        ask(settings(), instructions="Задай другой вопрос.", parse=question)
    assert len(requests) == 4


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), 503, 429])
def test_dropped_connection_or_busy_provider_is_asked_once_more(provider, failure):
    settings, requests, answers = provider
    answers.extend([failure, {"text": "Что вы сделали?"}])
    assert ask(settings()) == {"text": "Что вы сделали?"}
    assert len(requests) == 2


@pytest.mark.parametrize("failure, error", [(httpx.ReadTimeout("slow"), httpx.ReadTimeout), (400, RuntimeError)])
def test_timeout_and_bad_request_are_not_repeated(provider, failure, error):
    settings, requests, answers = provider
    answers.extend([failure, {"text": "Что вы сделали?"}])
    with pytest.raises(error):
        ask(settings())
    assert len(requests) == 1


def test_cache_can_be_turned_off_and_check_llm_always_reaches_the_model(provider):
    settings, requests, answers = provider
    assert ask(settings(llm_cache=False)) != ask(settings(llm_cache=False))
    answers.extend([{"ok": True}, {"ok": True}])
    for _ in range(2):
        assert asyncio.run(llm.check_llm(settings()))["status"] == "ok"
    assert len(requests) == 4


def test_sampling_options_reach_the_provider(provider):
    settings, requests, _ = provider
    ask(settings(llm_seed=7), payload={"answer": "один"})
    ask(settings(llm_temperature="", llm_seed=""), payload={"answer": "два"})
    assert (requests[0]["temperature"], requests[0]["seed"]) == (0, 7)
    assert "temperature" not in requests[1] and "seed" not in requests[1]
    with pytest.raises(ValidationError):
        settings(llm_temperature=3)


def test_card_keeps_the_order_the_expert_told_without_the_model(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("the card must not depend on the model")

    monkeypatch.setattr(interview_engine, "ask_model", unexpected)
    facts = [{"id": str(number), "field": field, "text": text, "supporting_message_ids": ["a1"]}
             for number, (field, text) in enumerate([
                 ("task", "Отчёт строился минуту."), ("action", "Добавил индекс."),
                 ("verification", "Сравнил время до и после."), ("action", "Переписал запрос."),
             ])]
    state = CaseState(facts=facts).model_dump()
    content, text = asyncio.run(interview_engine.synthesize(None, "Медленный отчёт", state))
    assert [fact["text"] for fact in content["actions"]] == ["Добавил индекс.", "Переписал запрос."]
    assert asyncio.run(interview_engine.synthesize(None, "Медленный отчёт", state)) == (content, text)


def test_concurrent_answers_settle_on_the_first_stored(provider):
    settings, _, _ = provider
    key = "a" * 64
    assert llm.remember_response(settings(), key, {"text": "первый"}) == {"text": "первый"}
    assert llm.remember_response(settings(), key, {"text": "второй"}) == {"text": "первый"}
