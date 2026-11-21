"""Provide a durable handoff to the independent arXiv worker."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    slot = op.create_table(
        "arxiv_worker_slot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("worker_until", sa.DateTime()),
        sa.Column("job_id", sa.String(36)),
        sa.Column("job_until", sa.DateTime()),
        sa.Column("search", sa.JSON()),
        sa.Column("result", sa.JSON()),
    )
    op.bulk_insert(slot, [{"id": 1}])


def downgrade():
    op.drop_table("arxiv_worker_slot")
