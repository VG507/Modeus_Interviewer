"""Identify the participant and moderator belonging to each demo guest."""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("demo_group", sa.String(36), nullable=True))
    op.create_index("ix_users_demo_group_role", "users", ["demo_group", "is_editor"], unique=True)


def downgrade():
    op.drop_index("ix_users_demo_group_role", table_name="users")
    op.drop_column("users", "demo_group")
