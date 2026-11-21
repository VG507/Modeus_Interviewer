"""Make GitHub evidence checks resumable across backend restarts."""

import sqlalchemy as sa
from alembic import op


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("discovery_candidates", sa.Column("retry_at", sa.DateTime()))


def downgrade():
    with op.batch_alter_table("discovery_candidates") as batch:
        batch.drop_column("retry_at")
