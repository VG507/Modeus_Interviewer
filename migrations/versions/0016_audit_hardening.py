"""Persist request windows, recover collection leases, and classify public cards."""

import sqlalchemy as sa
from alembic import op

from backend.catalog_topics import catalog_profiles, catalog_topic_columns, resolve_catalog_profession
from backend.db import InterviewSession, KnowledgeCard, now


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("interview_command_receipts", sa.Column("created_at", sa.DateTime()))
    stamp = now()
    receipts = sa.table("interview_command_receipts", sa.column("created_at", sa.DateTime()))
    op.execute(receipts.update().values(created_at=stamp))
    with op.batch_alter_table("interview_command_receipts") as batch:
        batch.alter_column("created_at", existing_type=sa.DateTime(), nullable=False)
    op.create_index("ix_interview_command_receipts_created_at", "interview_command_receipts", ["created_at"])
    op.create_index("ix_login_sessions_expires_at", "login_sessions", ["expires_at"])
    op.create_table(
        "login_windows",
        sa.Column("client", sa.String(64), primary_key=True),
        sa.Column("window", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_index("ix_login_windows_window", "login_windows", ["window"])
    op.create_index("ix_interview_sessions_origin_created_at", "interview_sessions", ["origin", "created_at"])
    op.add_column("vacancy_collections", sa.Column("lease_until", sa.DateTime()))
    op.add_column("knowledge_cards", sa.Column("catalog_profession_id", sa.String(64)))

    db = op.get_bind()
    profiles = catalog_profiles(db)
    cards = catalog_topic_columns(sa.select(KnowledgeCard.id), InterviewSession.context["role"].as_string())
    cards = cards.where(KnowledgeCard.publication_status == "published")
    for card_id, role, _, _, topic_profession in db.execute(cards):
        db.execute(sa.update(KnowledgeCard).where(KnowledgeCard.id == card_id).values(
            catalog_profession_id=resolve_catalog_profession(topic_profession, role, profiles),
            updated_at=KnowledgeCard.updated_at,
        ))


def downgrade():
    op.drop_column("knowledge_cards", "catalog_profession_id")
    op.drop_column("vacancy_collections", "lease_until")
    windows = sa.table("discovery_windows", sa.column("provider"))
    op.execute(windows.delete().where(windows.c.provider.in_(("github", "trends"))))
    op.drop_index("ix_interview_sessions_origin_created_at", table_name="interview_sessions")
    op.drop_table("login_windows")
    op.drop_index("ix_login_sessions_expires_at", table_name="login_sessions")
    op.drop_index("ix_interview_command_receipts_created_at", table_name="interview_command_receipts")
    with op.batch_alter_table("interview_command_receipts") as batch:
        batch.drop_column("created_at")
