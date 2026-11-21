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


@pytest.mark.parametrize("reply", ["Не знаю.", "не помню", "Честно, не могу вспомнить…", "Без понятия"])
def test_no_recall_skips_the_question_instead_of_asking_to_rephrase(monkeypatch, reply):
    async def classify(*args):
        raise AssertionError("A no-recall reply does not need the model")

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    case = interview_engine.Evidence(facts=[
        interview_engine.Candidate(field="task", quote="Отчёт грузился минуту."),
        interview_engine.Candidate(field="action", quote="Добавил индекс."),
    ], ambiguous=[], not_applicable=[], refused=False)
    state, _ = interview_engine.apply_extraction(CaseState(asked_intents=["get_case"]).model_dump(),
                                                 case, "a1", "get_case")
    state = {**state, "asked_intents": ["get_case", "decision"]}
    result = asyncio.run(interview_engine.extract(None, "Почему вы выбрали индекс?", {"text": reply}, state))
    assert (result.refused, result.needs_rephrase, result.assessment) == (False, False, "declined")
    next_state, extraction = interview_engine.apply_extraction(state, result, "a2", "decision")
    assert extraction["declined_intents"] == ["decision"]
    assert interview_engine.choose_intent(next_state) == "verification"
    assert not interview_engine.NO_RECALL.fullmatch("Не знаю, но после индекса отчёт ускорился.")


@pytest.mark.parametrize("text,expected", [
    ("Понятно, отчёт ускоряли индексом. Как вы поняли, что стало быстрее?",
     "Понятно, отчёт ускоряли индексом. Как вы поняли, что стало быстрее?"),
    ("Понял, что ты добавил индекс. Почему именно его?", interview_engine.FALLBACK_QUESTIONS["decision"]),
])
def test_followup_keeps_a_polite_two_part_question(monkeypatch, text, expected):
    async def model(settings, schema, instructions, payload):
        return schema(text=text)

    monkeypatch.setattr(interview_engine, "ask_model", model)
    result = asyncio.run(interview_engine.formulate_question(
        None, "Отчёт", "decision", {"text": "Добавил индекс."}, CaseState().model_dump()))
    assert result.text == expected


def test_broad_reply_gets_a_short_question_without_examples():
    state = {**CaseState().model_dump(), "intake": {"role": "Тестировщик"}, "followups_without_new_information": 1}
    result = asyncio.run(interview_engine.formulate_question(None, "Рабочий случай", "rephrase", None, state))
    assert result.text == "Нужен один конкретный случай, а не работа в целом. Что тогда случилось и что вы сделали?"
    assert interview_engine.case_examples({}) == interview_engine.DEFAULT_CASE_EXAMPLES


def test_second_broad_reply_shows_a_sample_answer(monkeypatch):
    async def model(settings, schema, instructions, payload):
        return schema(text="Как вы поняли, что отчёт стал строиться быстрее?")

    monkeypatch.setattr(interview_engine, "ask_model", model)
    state = {**CaseState().model_dump(), "intake": {"role": "Тестировщик"}, "followups_without_new_information": 1}
    first = asyncio.run(interview_engine.formulate_question(None, "Рабочий случай", "alternative", None, state))
    assert "(Например" not in first.text
    again = {**state, "followups_without_new_information": 2}
    case = asyncio.run(interview_engine.formulate_question(None, "Рабочий случай", "alternative", None, again))
    assert case.text.endswith("(Например: «Перед релизом я нашёл баг: при оплате заказ создавался дважды. "
                              "Описал шаги воспроизведения, и баг исправили до выката».)")
    check = asyncio.run(interview_engine.formulate_question(None, "Отчёт", "verification", {"text": "Стало лучше."},
                                                            again))
    assert check.text == ("Как вы поняли, что отчёт стал строиться быстрее? "
                          "(Например: «Сравнил до и после на тех же данных: было 60 секунд, стало 3».)")


def test_case_with_facts_asks_once_more_after_two_broad_replies():
    facts = [{"id": "1", "field": "task", "text": "Медленный отчёт", "responsibility": "personal",
              "supporting_message_ids": ["a"]}]
    state = {**CaseState().model_dump(), "facts": facts, "asked_intents": ["get_case", "action"]}
    assert interview_engine.choose_intent({**state, "followups_without_new_information": 2}) == "decision"
    assert interview_engine.choose_intent({**state, "followups_without_new_information": 3}) is None
    empty = {**CaseState().model_dump(), "asked_intents": ["get_case", "rephrase"],
             "followups_without_new_information": 2}
    assert interview_engine.choose_intent(empty) == "alternative"


def test_long_followup_is_replaced_with_a_clear_question(monkeypatch):
    async def model(settings, schema, instructions, payload):
        return schema(text="Слушаю, вы говорили, что отчёт грузился минуту — " + "это значит, что процесс шёл, " * 8
                           + "что вы сделали?")

    monkeypatch.setattr(interview_engine, "ask_model", model)
    result = asyncio.run(interview_engine.formulate_question(
        None, "Отчёт", "action", {"text": "Грузился минуту."}, CaseState().model_dump()))
    assert result.text == interview_engine.FALLBACK_QUESTIONS["action"]


@pytest.mark.parametrize("reply,topic", [
    ("Как-то раз нашёл баг в оплате", "Нашёл баг в оплате"),
    ("Да разные баги находил", None),
])
def test_brief_episode_starts_a_case(monkeypatch, reply, topic):
    async def classify(*args):
        return interview_engine.DiscoverySelection(facts=[], ambiguous=[], not_applicable=[], refused=False,
                                                   answer_fit="unclear", topic_title=None)

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    state = CaseState(open_interview=True, asked_intents=["get_case"]).model_dump()
    result = asyncio.run(interview_engine.extract(None, "Что произошло?", {"text": reply}, state))
    assert result.needs_rephrase is (topic is None)
    assert result.topic_title == topic
    if topic:
        assert [(fact.field, fact.quote) for fact in result.facts] == [("task", reply)]


@pytest.mark.parametrize("reply", [
    RuntimeError("LLM_INVALID_RESPONSE"),
    "Хорошо, я получил ответ: «Отчёт грузился минуту». Суть — отчёт грузился. Вопрос: С чего вы начали?",
])
def test_malformed_followup_falls_back_to_a_fixed_question(monkeypatch, reply):
    async def model(settings, schema, instructions, payload):
        if isinstance(reply, Exception):
            raise reply
        return schema(text=reply)

    monkeypatch.setattr(interview_engine, "ask_model", model)
    result = asyncio.run(interview_engine.formulate_question(
        None, "Отчёт", "action", {"text": "Грузился минуту."}, CaseState().model_dump()))
    assert result.text == interview_engine.FALLBACK_QUESTIONS["action"]


def test_unavailable_model_still_fails_the_followup(monkeypatch):
    async def model(settings, schema, instructions, payload):
        raise RuntimeError("LLM_HTTP_ERROR: 503")

    monkeypatch.setattr(interview_engine, "ask_model", model)
    with pytest.raises(RuntimeError):
        asyncio.run(interview_engine.formulate_question(
            None, "Отчёт", "action", {"text": "Грузился минуту."}, CaseState().model_dump()))


@pytest.mark.parametrize("reply,kept", [
    ("Был медленный отчёт по заказам, он строился около минуты.", []),
    ("Был медленный отчёт, я добавил индекс. Замеров не было, это был срочный хотфикс.", ["verification"]),
])
def test_not_applicable_needs_the_speakers_words(monkeypatch, reply, kept):
    async def classify(*args):
        return interview_engine.TurnSelection(
            facts=[{"field": "task", "sentence": 1}], ambiguous=[], refused=False, answer_fit="work_case",
            not_applicable=["task", "context", "action", "decision", "verification"] if not kept else kept,
        )

    monkeypatch.setattr(interview_engine, "ask_model", classify)
    state = CaseState(asked_intents=["get_case"]).model_dump()
    result = asyncio.run(interview_engine.extract(None, "Что произошло?", {"text": reply}, state))
    assert result.not_applicable == kept
    next_state, _ = interview_engine.apply_extraction(state, result, "a1", "get_case")
    assert interview_engine.choose_intent(next_state) is not None
