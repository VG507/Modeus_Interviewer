"""Store a reusable interview profile for each user."""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("interview_profile", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("users", "interview_profile")
