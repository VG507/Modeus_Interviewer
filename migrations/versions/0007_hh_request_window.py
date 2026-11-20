"""Share the HH request interval across collectors."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "hh_request_window",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_started_at", sa.DateTime()),
    )
    op.bulk_insert(table, [{"id": 1, "last_started_at": None}])


def downgrade():
    op.drop_table("hh_request_window")
