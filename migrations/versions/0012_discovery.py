"""Store collected source candidates and repository observations."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "discovery_items",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "discovery_candidates",
        sa.Column("item_id", sa.String(128), sa.ForeignKey("discovery_items.id"), primary_key=True),
        sa.Column("profession_id", sa.String(32), sa.ForeignKey("vacancy_collections.profession_id"), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reviewed_digest", sa.String(64)),
        sa.Column("assessment", sa.JSON()),
        sa.Column("reason", sa.Text()),
        sa.Column("excerpt_id", sa.String(64), sa.ForeignKey("source_excerpts.id"), unique=True),
        sa.Column("valid_until", sa.DateTime()),
    )
    op.create_index("ix_discovery_candidates_profession_id", "discovery_candidates", ["profession_id"])
    op.create_table(
        "repository_observations",
        sa.Column("item_id", sa.String(128), sa.ForeignKey("discovery_items.id"), primary_key=True),
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False),
    )
    windows = op.create_table(
        "discovery_windows",
        sa.Column("provider", sa.String(16), primary_key=True),
        sa.Column("available_at", sa.DateTime()),
        sa.Column("token", sa.String(36)),
    )
    op.bulk_insert(windows, [{"provider": "arxiv"}])


def downgrade():
    connection = op.get_bind()
    excerpts = sa.table("source_excerpts", sa.column("id"))
    candidates = sa.table("discovery_candidates", sa.column("excerpt_id"))
    promoted = list(connection.scalars(sa.select(candidates.c.excerpt_id).where(candidates.c.excerpt_id.is_not(None))))
    op.drop_table("discovery_windows")
    op.drop_table("repository_observations")
    op.drop_index("ix_discovery_candidates_profession_id", table_name="discovery_candidates")
    op.drop_table("discovery_candidates")
    op.drop_table("discovery_items")
    for excerpt_id in promoted:
        connection.execute(excerpts.delete().where(excerpts.c.id == excerpt_id))
