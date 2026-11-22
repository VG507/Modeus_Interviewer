"""Store O*NET occupation tasks for Modeus profiles."""

from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    # A database at 0022 from the embeddings branch has no ESCO table yet.
    if not op.get_context().as_sql and not sa.inspect(op.get_bind()).has_table("esco_profiles"):
        import_module("migrations.versions.0022_esco_profiles").upgrade()
    op.create_table(
        "onet_profiles",
        sa.Column("profession_id", sa.String(32), sa.ForeignKey("vacancy_collections.profession_id"), primary_key=True),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("occupation_code", sa.String(10), nullable=False),
        sa.Column("occupation_title", sa.String(150), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.Column("tasks", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("onet_profiles")
