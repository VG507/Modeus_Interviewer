import asyncio

import pytest

from backend import interview_engine
from backend.interview_schemas import CaseState


def test_evidence_uses_original_sentences_and_preserves_uncertainty(monkeypatch):
    async def classify(*args):
        return interview_engine.EvidenceSelection(
            facts=[
                {"field": "task", "sentence": 1},
                {"field": "action", "sentence": 2},
                {"field": "verification", "sentence": 3},
            ],
            ambiguous=[], not_applicable=[], refused=False,
        )

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    state = CaseState(asked_intents=["get_case"]).model_dump()
    answer = {"id": "a1", "text": "Обычно смотрю метрики. Индекс не помог. "
                                "Позже запрос ускорился, но одновременно вышел ещё один релиз."}
    result = asyncio.run(interview_engine.extract(None, "Что произошло?", answer, state))
    assert all(item.quote in answer["text"] for item in result.facts)
    assert {(item.field, item.quote) for item in result.facts} == {
        ("action", "Индекс не помог."),
        ("error", "Индекс не помог."),
        ("context", "Позже запрос ускорился, но одновременно вышел ещё один релиз."),
    }
    assert result.ambiguous == ["verification"]
    next_state, _ = interview_engine.apply_extraction(state, result, "a1", "get_case")
    assert interview_engine.choose_intent(next_state) == "clarify_verification"


def test_unknown_sentence_is_rejected(monkeypatch):
    async def classify(*args):
        return interview_engine.EvidenceSelection(
            facts=[{"field": "action", "sentence": 2}],
            ambiguous=[], not_applicable=[], refused=False,
        )

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    with pytest.raises(interview_engine.InvalidModelOutput):
        asyncio.run(interview_engine.extract(None, "Что сделали?", {"text": "Добавил индекс."}, {}))


@pytest.mark.parametrize("fit,reply", [
    ("work_case", "Жарко было"),
    ("unrelated", "Мы долго обсуждали погоду в выходные."),
])
def test_unanchored_or_unrelated_reply_does_not_create_a_topic(monkeypatch, fit, reply):
    async def classify(*args):
        return interview_engine.DiscoverySelection(
            facts=[{"field": "task", "sentence": 1}],
            ambiguous=[], not_applicable=[], refused=False, answer_fit=fit, topic_title=reply,
        )

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    state = CaseState(open_interview=True, asked_intents=["get_case"]).model_dump()
    answer = {"id": "a1", "text": reply}
    evidence = asyncio.run(interview_engine.extract(None, "Что произошло на работе?", answer, state))
    updated, _ = interview_engine.apply_extraction(state, evidence, "a1", "get_case")
    assert not updated["facts"] and updated["discovered_topic"] is None
    assert interview_engine.choose_intent(updated, needs_rephrase=evidence.needs_rephrase) == "rephrase"
    assert evidence.assessment in {"unclear", "unrelated"}


def test_pilot_reason_and_team_action_survive_wrong_model_labels(monkeypatch):
    async def classify(*args):
        return interview_engine.EvidenceSelection(
            facts=[{"field": "task", "sentence": 1}, {"field": "verification", "sentence": 1}],
            ambiguous=["task"], not_applicable=[], refused=False,
        )

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    case = {"id": "task", "field": "task", "text": "Отчёт строился долго.",
            "supporting_message_ids": ["prior"]}
    state = CaseState(asked_intents=["get_case", "decision"], facts=[case]).model_dump()
    reason = asyncio.run(interview_engine.extract(
        None, "Почему выбрали индекс?", {"text": "Потому что запрос фильтрует по дате."}, state,
    ))
    assert [fact.field for fact in reason.facts] == ["decision"]
    assert not reason.ambiguous
    state = CaseState(asked_intents=["get_case", "action"], facts=[case]).model_dump()
    team = asyncio.run(interview_engine.extract(
        None, "Что сделали?", {"text": "Мы добавили индекс."}, state,
    ))
    updated, _ = interview_engine.apply_extraction(state, team, "a1", "action")
    assert next(fact["responsibility"] for fact in updated["facts"]
                if fact["field"] == "action") == "shared"
    assert interview_engine.choose_intent(updated) == "participation"


def test_diagnostic_finding_and_measured_effect_keep_their_card_sections(monkeypatch):
    async def classify(*args):
        return interview_engine.EvidenceSelection(
            facts=[{"field": "task", "sentence": 1}, {"field": "task", "sentence": 3}],
            ambiguous=[], not_applicable=[], refused=False,
        )

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    task = "Отчёт строился две секунды."
    context = "В плане увидел полный проход таблицы."
    measurement = "Добавил индекс по полю фильтра и на том же наборе данных получил 200 мс."
    state = CaseState(asked_intents=["get_case"]).model_dump()
    answer = {"text": f"{task} {context} {measurement}"}
    result = asyncio.run(interview_engine.extract(None, "Что произошло?", answer, state))
    assert [(fact.field, fact.quote) for fact in result.facts] == [
        ("task", task), ("context", context), ("action", measurement), ("verification", measurement),
    ]


def test_explanation_of_design_fit_is_a_reason_and_explicit_action_stays_separate(monkeypatch):
    async def classify(*args):
        return interview_engine.EvidenceSelection(
            facts=[{"field": "action", "sentence": 2}, {"field": "action", "sentence": 3}],
            ambiguous=[], not_applicable=[], refused=False,
        )

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    state = CaseState(asked_intents=["get_case", "decision"]).model_dump()
    reason = "Потому что запрос фильтруется по равенству и диапазону."
    explanation = "Композитный индекс повторяет этот порядок."
    action = "Затем я проверил план на тестовой базе."
    answer = {"text": f"{reason} {explanation} {action}"}
    result = asyncio.run(interview_engine.extract(None, "Почему выбрали индекс?", answer, state))
    assert [(fact.field, fact.quote) for fact in result.facts] == [
        ("decision", reason), ("decision", explanation), ("action", action),
    ]


def test_reported_reason_and_before_after_measurement_survive_model_omissions(monkeypatch):
    async def classify(settings, schema, instructions, payload):
        facts = [{"field": "action", "sentence": 1}] if payload["intent"] == "get_case" else []
        return schema(facts=facts, ambiguous=[], not_applicable=[], refused=False, answer_fit="work_case")

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    state = CaseState(asked_intents=["get_case"]).model_dump()
    reason = "Потому что фильтрами пользовались редко."
    result = asyncio.run(interview_engine.extract(None, "Что сделали?", {"text": f"Я разделил бандл. {reason}"}, state))
    assert ("decision", reason) in [(fact.field, fact.quote) for fact in result.facts]

    state["asked_intents"].append("verification")
    method = "Я замерил время загрузки на одном телефоне."
    comparison = "До изменения было 5 секунд, после стало 2 секунды."
    result = asyncio.run(interview_engine.extract(None, "Как проверили?", {"text": f"{method} {comparison}"}, state))
    assert [(fact.field, fact.quote) for fact in result.facts] == [("verification", method), ("verification", comparison)]


@pytest.mark.parametrize("fit", ["work_case", "unclear"])
def test_frontend_episode_survives_empty_extraction_and_keeps_only_evidence(monkeypatch, fit):
    async def classify(settings, schema, instructions, payload):
        opening = payload["intent"] == "get_case"
        result = dict(facts=[] if opening else [{"field": "task", "sentence": 1}, {"field": "action", "sentence": 2}],
                      ambiguous=[], not_applicable=[], refused=False, answer_fit=fit if opening else "work_case")
        return schema(**result, topic_title=None) if opening else schema(**result)

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    state = CaseState(open_interview=True, asked_intents=["get_case"]).model_dump()
    task = "Страница каталога на React на телефонах загружалась 5 секунд."
    action = "Я вынес тяжёлый модуль фильтров в отдельный чанк."
    reason = "Потому что фильтрами пользовались редко, а модуль попадал в начальную загрузку у всех."
    reply = {"text": f"{task} {action} {reason}"}
    evidence = asyncio.run(interview_engine.extract(None, "Что произошло?", reply, state))
    state, _ = interview_engine.apply_extraction(state, evidence, "opening", "get_case")
    assert [(fact["field"], fact["text"]) for fact in state["facts"]] == [
        ("task", task), ("action", action), ("decision", reason),
    ]
    assert state["discovered_topic"] and not evidence.needs_rephrase
    assert interview_engine.choose_intent(state) == "verification"

    state["asked_intents"].append("verification")
    method = "Я замерил время загрузки этой же страницы на одном телефоне и с одинаковым соединением."
    comparison = "До изменения было 5 секунд, после стало 2 секунды."
    evidence = asyncio.run(interview_engine.extract(None, "Как проверили?", {"text": f"{method} {comparison}"}, state))
    state, _ = interview_engine.apply_extraction(state, evidence, "measurement", "verification")
    card, _ = interview_engine.render_card(state["discovered_topic"], state, state["facts"])
    assert [fact["text"] for fact in card["task"]] == [task]
    assert [fact["text"] for fact in card["actions"]] == [action]
    assert [fact["text"] for fact in card["decisions"]] == [reason]
    assert [(fact["text"], fact["supporting_message_ids"]) for fact in card["verification"]] == [
        (method, ["measurement"]), (comparison, ["measurement"]),
    ]


def test_long_open_reply_keeps_evidence_from_its_last_batch(monkeypatch):
    task = "Заказы зависли после сбоя очереди."
    verification = "По графику число обработанных заказов сравнили до и после перезапуска."
    detail = "Тестовая очередь заказов работала ночью с заметной задержкой. "
    reply = f"{task} {detail * 120}{verification}"
    assert 2 * interview_engine.ANSWER_BATCH_SIZE < len(reply) <= 8000
    calls = []

    async def classify(settings, schema, instructions, payload):
        sentences = {item["text"]: item["number"] for item in payload["sentences"]}
        calls.append(payload)
        common = dict(ambiguous=[], not_applicable=[], refused=False, answer_fit="work_case")
        if task in sentences:
            assert not payload["case_anchor"]
            return schema(facts=[{"field": "task", "sentence": sentences[task]}],
                          topic_title="Сбой очереди заказов", **common)
        assert payload["case_anchor"] == [task]
        facts = [{"field": "verification", "sentence": sentences[verification]}] if verification in sentences else []
        return schema(facts=facts, **common)

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    state = CaseState(open_interview=True, asked_intents=["get_case"]).model_dump()
    evidence = asyncio.run(interview_engine.extract(None, "Что произошло?", {"text": reply}, state))
    state, _ = interview_engine.apply_extraction(state, evidence, "answer", "get_case")
    assert len(calls) == 3
    assert state["discovered_topic"] == "Сбой очереди заказов"
    assert [(fact["field"], fact["text"]) for fact in state["facts"]] == [
        ("task", task), ("verification", verification),
    ]
