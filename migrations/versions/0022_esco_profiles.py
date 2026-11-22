"""Store the latest successful ESCO profile for each Modeus profession."""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "esco_profiles",
        sa.Column("profession_id", sa.String(32), sa.ForeignKey("vacancy_collections.profession_id"), primary_key=True),
        sa.Column("version", sa.String(24), nullable=False),
        sa.Column("target", sa.String(180), nullable=False),
        sa.Column("match", sa.String(12), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.Column("occupation", sa.JSON(), nullable=False),
        sa.Column("skills", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("esco_profiles")
