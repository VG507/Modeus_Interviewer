"""Persist the current stage and counters of an HH collection."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vacancy_collections", sa.Column("progress", sa.JSON()))


def downgrade():
    with op.batch_alter_table("vacancy_collections") as batch:
        batch.drop_column("progress")
