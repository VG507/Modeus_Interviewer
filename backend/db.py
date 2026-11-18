from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_editor: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profession_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    source_ids: Mapped[list] = mapped_column(JSON)
    opening_question: Mapped[str | None] = mapped_column(Text)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    retrieved_on: Mapped[str] = mapped_column(String(10))
    text: Mapped[str] = mapped_column(Text)


class InterviewSession(Base):
    __tablename__ = "interview_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    topic_id: Mapped[str] = mapped_column(ForeignKey("topics.id"))
    context: Mapped[dict] = mapped_column(JSON)
    origin: Mapped[str] = mapped_column(String(16), default="human")
    status: Mapped[str] = mapped_column(String(16), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    case_state: Mapped[dict] = mapped_column(JSON, default=dict)
    processing_status: Mapped[str] = mapped_column(String(16), default="idle")
    processing_stage: Mapped[str | None] = mapped_column(String(24))
    current_question_id: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[dict | None] = mapped_column(JSON)
    job: Mapped[dict | None] = mapped_column(JSON)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}


class Message(Base):
    __tablename__ = "interview_messages"
    __table_args__ = (UniqueConstraint("session_id", "sequence", name="uq_message_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("interview_sessions.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text)
    reply_to_question_id: Mapped[str | None] = mapped_column(ForeignKey("interview_messages.id"))
    status: Mapped[str] = mapped_column(String(16))
    extraction: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class CommandReceipt(Base):
    __tablename__ = "interview_command_receipts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    body_digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)


class KnowledgeCard(Base):
    __tablename__ = "knowledge_cards"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("interview_sessions.id"), unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    origin: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_version: Mapped[int] = mapped_column(Integer, default=1)
    completion_version: Mapped[int] = mapped_column(Integer)
    publication_status: Mapped[str] = mapped_column(String(16), default="draft")
    review_status: Mapped[str] = mapped_column(String(16), default="unreviewed")
    content: Mapped[dict] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


def database(url):
    options = {"check_same_thread": False} if url.startswith("sqlite:") else {}
    engine = create_engine(url, connect_args=options, pool_pre_ping=True)
    if options:
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    return engine, sessionmaker(engine, expire_on_commit=False)
