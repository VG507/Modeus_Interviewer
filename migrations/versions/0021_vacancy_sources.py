"""Track the latest successful vacancy snapshot for additional sources."""

from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    if not op.get_context().as_sql:
        tables = sa.inspect(op.get_bind())
        # An earlier local revision 0020 created only the vacancy source table.
        if not tables.has_table("llm_responses"):
            import_module("migrations.versions.0020_llm_cache").upgrade()
        if tables.has_table("vacancy_source_samples"):
            return
    op.create_table(
        "vacancy_source_samples",
        sa.Column("profession_id", sa.String(32), sa.ForeignKey("vacancy_collections.profession_id"), primary_key=True),
        sa.Column("source", sa.String(24), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("vacancy_snapshots.id"), nullable=False),
    )


def downgrade():
    op.drop_table("vacancy_source_samples")
