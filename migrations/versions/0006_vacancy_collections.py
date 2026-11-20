"""Store HH samples and the context considered for interview questions."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    collections = op.create_table(
        "vacancy_collections",
        sa.Column("profession_id", sa.String(32), primary_key=True),
        sa.Column("cards", sa.JSON(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("run_id", sa.String(36)),
        sa.Column("error", sa.String(64)),
    )
    op.bulk_insert(collections, [
        {"profession_id": profile, "cards": [], "stats": {}}
        for profile in ("python_backend", "frontend", "qa", "devops")
    ])
    op.create_table(
        "vacancy_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("profession_id", sa.String(32), sa.ForeignKey("vacancy_collections.profession_id"), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False),
    )
    op.create_index("ix_vacancy_snapshots_profession_id", "vacancy_snapshots", ["profession_id"])
    op.add_column("interview_messages", sa.Column("market_sources", sa.JSON()))


def downgrade():
    with op.batch_alter_table("interview_messages") as batch:
        batch.drop_column("market_sources")
    op.drop_index("ix_vacancy_snapshots_profession_id", table_name="vacancy_snapshots")
    op.drop_table("vacancy_snapshots")
    op.drop_table("vacancy_collections")
