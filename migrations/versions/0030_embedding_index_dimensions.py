"""Exclude rows with incompatible dimensions from existing bge-m3 HNSW indexes."""

from importlib import import_module

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    previous = import_module("migrations.versions.0027_card_embedding_index")
    bind = op.get_bind()
    if not previous.vector_column(bind):
        return
    definition = bind.scalar(sa.text("select pg_get_indexdef(to_regclass(:name))"), {"name": previous.INDEX})
    if definition and "vector_dims(" in definition:
        return  # Fresh databases already have the corrected index from 0027.
    op.execute(f"drop index if exists {previous.INDEX}")
    previous.create_index()


def downgrade():
    # The current 0027 creates the same index, so the corrected definition remains valid.
    pass
