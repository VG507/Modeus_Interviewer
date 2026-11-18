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
