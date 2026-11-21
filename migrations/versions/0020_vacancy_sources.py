"""Track the latest successful vacancy snapshot for additional sources."""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "vacancy_source_samples",
        sa.Column("profession_id", sa.String(32), sa.ForeignKey("vacancy_collections.profession_id"), primary_key=True),
        sa.Column("source", sa.String(24), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("vacancy_snapshots.id"), nullable=False),
    )


def downgrade():
    op.drop_table("vacancy_source_samples")
