"""Track the latest successful vacancy snapshot for additional sources."""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    # This migration first shipped as a second "0020", so a database marked 0020
    # may have either llm_responses or vacancy_source_samples. Create what is missing.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "llm_responses" not in tables:
        op.create_table(
            "llm_responses",
            sa.Column("key", sa.String(64), primary_key=True),
            sa.Column("model", sa.String(200), nullable=False),
            sa.Column("response", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    if "vacancy_source_samples" not in tables:
        op.create_table(
            "vacancy_source_samples",
            sa.Column("profession_id", sa.String(32), sa.ForeignKey("vacancy_collections.profession_id"), primary_key=True),
            sa.Column("source", sa.String(24), primary_key=True),
            sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("vacancy_snapshots.id"), nullable=False),
        )


def downgrade():
    op.drop_table("vacancy_source_samples")
