from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine, event,
    false, func, select, text,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def insert_for(db, model):
    return (sqlite_insert if db.get_bind().dialect.name == "sqlite" else postgres_insert)(model)


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
    interview_profile: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TelegramInvite(Base):
    __tablename__ = "telegram_invites"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class LoginWindow(Base):
    __tablename__ = "login_windows"
    client: Mapped[str] = mapped_column(String(64), primary_key=True)
    window: Mapped[int] = mapped_column(Integer, index=True)
    attempts: Mapped[int] = mapped_column(Integer)


class DiscoveryWindow(Base):
    __tablename__ = "discovery_windows"
    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    token: Mapped[str | None] = mapped_column(String(36))
    available_at: Mapped[datetime | None] = mapped_column(DateTime)


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


class SourceExcerpt(Base):
    __tablename__ = "source_excerpts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    profession_id: Mapped[str] = mapped_column(ForeignKey("vacancy_collections.profession_id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(24))
    purpose: Mapped[str] = mapped_column(String(24))
    task: Mapped[str] = mapped_column(String(300))
    quote: Mapped[str] = mapped_column(String(500))
    detail: Mapped[str] = mapped_column(String(200))
    technology: Mapped[str | None] = mapped_column(String(80))
    version: Mapped[str | None] = mapped_column(String(30))
    conditions: Mapped[list] = mapped_column(JSON)
    method: Mapped[str | None] = mapped_column(String(500))


class DiscoveryItem(Base):
    __tablename__ = "discovery_items"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime)
    payload: Mapped[dict] = mapped_column(JSON)


class DiscoveryCandidate(Base):
    __tablename__ = "discovery_candidates"
    item_id: Mapped[str] = mapped_column(ForeignKey("discovery_items.id"), primary_key=True)
    profession_id: Mapped[str] = mapped_column(ForeignKey("vacancy_collections.profession_id"), primary_key=True, index=True)
    status: Mapped[str] = mapped_column(String(16))
    reviewed_digest: Mapped[str | None] = mapped_column(String(64))
    assessment: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    excerpt_id: Mapped[str | None] = mapped_column(ForeignKey("source_excerpts.id"), unique=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime)


class DiscoveryRevision(Base):
    __tablename__ = "discovery_revisions"
    item_id: Mapped[str] = mapped_column(ForeignKey("discovery_items.id"), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime)
    payload: Mapped[dict] = mapped_column(JSON)


class DocumentationWatch(Base):
    __tablename__ = "documentation_watches"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
    token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)


class DocumentationSubscription(Base):
    __tablename__ = "documentation_subscriptions"
    watch_id: Mapped[str] = mapped_column(ForeignKey("documentation_watches.id"), primary_key=True)
    profession_id: Mapped[str] = mapped_column(ForeignKey("vacancy_collections.profession_id"), primary_key=True)


class DocumentationRevision(Base):
    __tablename__ = "documentation_revisions"
    item_id: Mapped[str] = mapped_column(ForeignKey("discovery_items.id"), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class RepositoryObservation(Base):
    __tablename__ = "repository_observations"
    item_id: Mapped[str] = mapped_column(ForeignKey("discovery_items.id"), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime)
    stars: Mapped[int] = mapped_column(Integer)


class HackerNewsItem(Base):
    __tablename__ = "hacker_news_items"
    __table_args__ = (Index("ix_hacker_news_items_status_observed_at", "status", "observed_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(DateTime)
    text: Mapped[str] = mapped_column(Text)
    links: Mapped[list[str]] = mapped_column(JSON)
    score: Mapped[int] = mapped_column(Integer)
    descendants: Mapped[int] = mapped_column(Integer)
    kids: Mapped[list[int]] = mapped_column(JSON)
    comments: Mapped[list[dict]] = mapped_column(JSON)
    comments_limited: Mapped[bool] = mapped_column(Boolean)
    comments_incomplete: Mapped[bool] = mapped_column(Boolean)
    comments_observed_at: Mapped[datetime] = mapped_column(DateTime)
    profile_matches: Mapped[dict[str, list[str]]] = mapped_column(JSON)
    feed_ranks: Mapped[dict[str, int]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime)
    observed_at: Mapped[datetime] = mapped_column(DateTime)


class InterviewSession(Base):
    __tablename__ = "interview_sessions"
    __table_args__ = (Index("ix_interview_sessions_origin_created_at", "origin", "created_at"),)
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
    market_sources: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class CommandReceipt(Base):
    __tablename__ = "interview_command_receipts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    body_digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


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
    show_author: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    author_name: Mapped[str | None] = mapped_column(String(120))
    first_submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    consent: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    review: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    catalog_profession_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}


class CardEvent(Base):
    __tablename__ = "card_events"
    __table_args__ = (UniqueConstraint("card_id", "version", name="uq_card_event_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("knowledge_cards.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer)
    content_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    details: Mapped[dict] = mapped_column(JSON)


class VacancyCollection(Base):
    __tablename__ = "vacancy_collections"
    profession_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), server_default="")
    title_key: Mapped[str] = mapped_column(String(360), server_default="", index=True, unique=True)
    search_query: Mapped[str] = mapped_column(String(500), server_default="")
    aliases: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    approved: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    config_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    cards: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    details_cache: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    progress: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    run_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(String(64))
    discovery_searches: Mapped[dict | None] = mapped_column(JSON)


class ProfessionCollectionRun(Base):
    __tablename__ = "profession_collection_runs"
    profession_id: Mapped[str] = mapped_column(ForeignKey("vacancy_collections.profession_id"), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    steps: Mapped[dict] = mapped_column(JSON)


class EscoProfile(Base):
    __tablename__ = "esco_profiles"
    profession_id: Mapped[str] = mapped_column(ForeignKey("vacancy_collections.profession_id"), primary_key=True)
    version: Mapped[str] = mapped_column(String(24))
    target: Mapped[str] = mapped_column(String(180))
    match: Mapped[str] = mapped_column(String(12))
    collected_at: Mapped[datetime] = mapped_column(DateTime)
    occupation: Mapped[dict] = mapped_column(JSON)
    skills: Mapped[list] = mapped_column(JSON)


class OnetProfile(Base):
    __tablename__ = "onet_profiles"
    profession_id: Mapped[str] = mapped_column(ForeignKey("vacancy_collections.profession_id"), primary_key=True)
    version: Mapped[str] = mapped_column(String(16))
    occupation_code: Mapped[str] = mapped_column(String(10))
    occupation_title: Mapped[str] = mapped_column(String(150))
    collected_at: Mapped[datetime] = mapped_column(DateTime)
    tasks: Mapped[list] = mapped_column(JSON)


class HeadHunterRequestWindow(Base):
    __tablename__ = "hh_request_window"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime)


class VacancySnapshot(Base):
    __tablename__ = "vacancy_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profession_id: Mapped[str] = mapped_column(ForeignKey("vacancy_collections.profession_id"), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime)
    stats: Mapped[dict] = mapped_column(JSON)
    cards: Mapped[list | None] = mapped_column(JSON)


class VacancySourceSample(Base):
    __tablename__ = "vacancy_source_samples"
    profession_id: Mapped[str] = mapped_column(ForeignKey("vacancy_collections.profession_id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(24), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("vacancy_snapshots.id"))


class LlmResponse(Base):
    __tablename__ = "llm_responses"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(200))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class CardEmbedding(Base):
    __tablename__ = "card_embeddings"
    card_id: Mapped[str] = mapped_column(ForeignKey("knowledge_cards.id"), primary_key=True)
    case_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    model: Mapped[str] = mapped_column(String(200))
    text_hash: Mapped[str] = mapped_column(String(64))
    # A unit vector as JSON text; the column is pgvector's vector type where the extension exists.
    embedding: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TextEmbedding(Base):
    """Vector of any other text by its content, e.g. a vacancy duty."""
    __tablename__ = "text_embeddings"
    model: Mapped[str] = mapped_column(String(200), primary_key=True)
    text_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# Rebuilt from other tables, so a database copy leaves them for the target to recompute.
DERIVED_TABLES = {"card_embeddings", "text_embeddings"}


def database(url):
    options = {"check_same_thread": False} if url.startswith("sqlite:") else {}
    engine = create_engine(url, connect_args=options, pool_pre_ping=True)
    if options:
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    return engine, sessionmaker(engine, expire_on_commit=False)


COPY_BATCH = 500


def revision(connection):
    return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def copy_database(source_url, target_url):
    """Copy every row into a freshly migrated database, parents before children, in one transaction.

    Rows that migrations seeded in the target are replaced by the source's own copies.
    """
    source, target = database(source_url)[0], database(target_url)[0]
    try:
        with source.connect() as reader, target.begin() as writer:
            if revision(reader) != revision(writer):
                raise RuntimeError("DATABASE_REVISION_MISMATCH: run alembic upgrade head on both databases")
            # A database in use always has a user; a freshly migrated one has only seeded rows.
            if writer.execute(select(func.count()).select_from(User)).scalar():
                raise RuntimeError("TARGET_IN_USE: the target database already has users")
            for table in reversed(Base.metadata.sorted_tables):
                writer.execute(table.delete())
            copied = {}
            for table in Base.metadata.sorted_tables:
                if table.name in DERIVED_TABLES:
                    continue
                rows = reader.execute(select(table))
                copied[table.name] = 0
                while batch := rows.fetchmany(COPY_BATCH):
                    writer.execute(table.insert(), [dict(row._mapping) for row in batch])
                    copied[table.name] += len(batch)
            if writer.dialect.name == "postgresql":
                # Copied ids do not move a serial counter; the next insert would reuse id 1.
                for table in Base.metadata.sorted_tables:
                    for column in table.primary_key.columns:
                        if isinstance(column.type, Integer) and column.autoincrement is not False:
                            writer.execute(text(
                                f"SELECT setval(pg_get_serial_sequence('{table.name}', '{column.name}'), "
                                f"COALESCE(MAX({column.name}), 1), MAX({column.name}) IS NOT NULL) FROM {table.name}"
                            ))
        return copied
    finally:
        source.dispose()
        target.dispose()
