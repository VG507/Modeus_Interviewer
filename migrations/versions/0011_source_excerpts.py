"""Store verified excerpts for interview questions."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_excerpts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("source_id", sa.String(64), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("profession_id", sa.String(32), sa.ForeignKey("vacancy_collections.profession_id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("task", sa.String(300), nullable=False),
        sa.Column("quote", sa.String(500), nullable=False),
        sa.Column("detail", sa.String(200), nullable=False),
        sa.Column("technology", sa.String(80)),
        sa.Column("version", sa.String(30)),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("method", sa.String(500)),
    )
    op.create_index("ix_source_excerpts_profession_id", "source_excerpts", ["profession_id"])


def downgrade():
    op.drop_index("ix_source_excerpts_profession_id", table_name="source_excerpts")
    op.drop_table("source_excerpts")
