import json
from pathlib import Path

from backend.interview_schemas import (
    AnswerResult,
    CreateAnswer,
    CreateInterview,
    ErrorResponse,
    Extraction,
    MessageView,
    VersionedRequest,
)


def test_first_turn_examples_match_contract():
    examples = json.loads(Path("data/interview/first_turn.json").read_text(encoding="utf-8"))
    create = CreateInterview.model_validate(examples["create"])
    draft = CreateAnswer.model_validate(examples["draft"])
    submit = VersionedRequest.model_validate(examples["submit"])
    accepted = AnswerResult.model_validate(examples["accepted"])
    extraction = Extraction.model_validate(examples["extraction"])
    next_question = MessageView.model_validate(examples["next_question"])
    conflict = ErrorResponse.model_validate(examples["conflict"])

    assert accepted.interview.topic_id == create.topic_id
    assert draft.expected_version + 1 == submit.expected_version
    assert accepted.interview.version == submit.expected_version + 1
    assert accepted.answer.reply_to_question_id == draft.question_id
    assert accepted.answer.id == extraction.message_id
    assert next_question.session_id == accepted.interview.id
    assert next_question.sequence == accepted.answer.sequence + 1
    assert conflict.error.current_version == accepted.interview.version
