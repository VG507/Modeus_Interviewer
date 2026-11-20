"""Cache HH vacancy details for reuse in the next collection."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vacancy_collections", sa.Column("details_cache", sa.JSON()))


def downgrade():
    with op.batch_alter_table("vacancy_collections") as batch:
        batch.drop_column("details_cache")
