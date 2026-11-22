"""API and extraction contracts for the first interview turn."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ContextText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]
ProjectText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
AnswerText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
FactField = Literal["task", "context", "action", "decision", "verification", "error", "limitation"]
Responsibility = Literal["personal", "shared", "other", "unknown"]
AnswerAssessment = Literal["relevant", "unclear", "unrelated", "declined"]
Tone = Literal["friendly", "business", "playful"]
OPEN_TOPIC_ID = "open-interview"


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectContext(Schema):
    task: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    period: ProjectText = ""
    role: ProjectText = ""
    responsibility: ContextText = ""
    stack: ProjectText = ""
    constraints: ContextText = ""


class InterviewContext(Schema):
    role: ShortText
    experience: ContextText = ""
    responsibilities: ContextText = ""
    conditions: ContextText = ""
    # Keep the serialized request stable for idempotent replays from older clients.
    projects: list[ProjectContext] = Field(default_factory=list, max_length=3, exclude_if=lambda projects: not projects)
    # How the interviewer words its questions; see TONES in interview_engine.
    tone: Tone = Field(default="friendly", exclude_if=lambda tone: tone == "friendly")


class CreateInterview(Schema):
    topic_id: Identifier | None = None
    context: InterviewContext | None = Field(default=None, exclude_if=lambda context: context is None)


class VersionedRequest(Schema):
    expected_version: Annotated[int, Field(strict=True, ge=1)]


class UpdateAnswer(VersionedRequest):
    text: AnswerText


class CreateAnswer(UpdateAnswer):
    question_id: Identifier


class TopicSummary(Schema):
    id: Identifier
    profession_id: Identifier
    title: ShortText


class Fact(Schema):
    id: Identifier
    field: FactField
    text: AnswerText
    supporting_message_ids: list[Identifier] = Field(min_length=1)
    responsibility: Responsibility = "unknown"


class Gap(Schema):
    field: FactField
    status: Literal["unknown", "ambiguous", "not_applicable"]
    note: ContextText = ""


class Extraction(Schema):
    message_id: Identifier
    schema_version: Literal[1]
    facts: list[Fact]
    gaps: list[Gap]
    declined_intents: list[Identifier]
    new_topic_notes: list[ContextText]
    assessment: AnswerAssessment | None = None


class CaseState(Schema):
    facts: list[Fact] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    asked_intents: list[Identifier] = Field(default_factory=list)
    declined_intents: list[Identifier] = Field(default_factory=list)
    followups_without_new_information: int = Field(default=0, ge=0)
    active_topic_id: Identifier | None = None
    open_interview: bool = False
    discovered_topic: ShortText | None = None
    past_cases: list[dict] = Field(default_factory=list)
    case_start_sequence: int | None = Field(default=None, ge=1)
    # Cases in a row that ended without any facts.
    empty_cases: int = Field(default=0, ge=0)


class ApiError(Schema):
    code: str
    message: str
    retryable: bool
    request_id: str
    current_version: int | None = None


class ErrorResponse(Schema):
    error: ApiError


class InterviewView(Schema):
    id: Identifier
    topic_id: Identifier
    current_topic_title: str | None = None
    context: InterviewContext
    status: Literal["active", "completing", "completed"]
    version: int = Field(ge=1)
    processing_status: Literal["idle", "queued", "running", "failed"]
    processing_stage: Literal["initial_question", "extraction", "question", "synthesis"] | None
    current_question_id: Identifier | None
    card_id: Identifier | None = None
    error: ApiError | None = None
    created_at: datetime
    updated_at: datetime


class SourceQuote(Schema):
    title: str
    url: str
    quote: str


class MessageView(Schema):
    id: Identifier
    session_id: Identifier
    sequence: int = Field(ge=1)
    kind: Literal["question", "answer", "control"]
    text: str | None
    reply_to_question_id: Identifier | None = None
    status: Literal["draft", "queued", "processing", "processed", "failed"]
    created_at: datetime
    updated_at: datetime
    assessment: AnswerAssessment | None = None
    vacancy_quote: SourceQuote | None = None
    source_quote: SourceQuote | None = None


class InterviewDetail(InterviewView):
    messages: list[MessageView]


class AnswerResult(Schema):
    interview: InterviewView
    answer: MessageView
