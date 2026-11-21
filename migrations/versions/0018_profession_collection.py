"""Track combined profession refreshes and preserve source snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vacancy_collections", sa.Column("discovery_searches", sa.JSON()))
    op.add_column("vacancy_snapshots", sa.Column("cards", sa.JSON()))
    op.execute("""
        UPDATE vacancy_snapshots SET cards = (
            SELECT collection.cards FROM vacancy_collections AS collection
            WHERE collection.profession_id = vacancy_snapshots.profession_id
              AND collection.updated_at = vacancy_snapshots.collected_at
        )
    """)
    op.create_table(
        "discovery_revisions",
        sa.Column("item_id", sa.String(128), sa.ForeignKey("discovery_items.id"), primary_key=True),
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.execute("""
        INSERT INTO discovery_revisions (item_id, digest, title, url, observed_at, payload)
        SELECT id, digest, title, url, observed_at, payload FROM discovery_items
        WHERE provider IN ('github', 'trends')
    """)
    op.create_table(
        "profession_collection_runs",
        sa.Column("profession_id", sa.String(32), sa.ForeignKey("vacancy_collections.profession_id"), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("steps", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("profession_collection_runs")
    op.drop_table("discovery_revisions")
    op.drop_column("vacancy_snapshots", "cards")
    op.drop_column("vacancy_collections", "discovery_searches")
