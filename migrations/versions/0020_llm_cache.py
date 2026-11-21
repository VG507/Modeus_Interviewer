"""Store accepted model responses so the same request always gets the same answer."""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "llm_responses",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("llm_responses")
