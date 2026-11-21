"""Store Hacker News stories and bounded discussion snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "hacker_news_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("author", sa.String(64)),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("links", sa.JSON(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("descendants", sa.Integer(), nullable=False),
        sa.Column("kids", sa.JSON(), nullable=False),
        sa.Column("comments", sa.JSON(), nullable=False),
        sa.Column("comments_limited", sa.Boolean(), nullable=False),
        sa.Column("comments_incomplete", sa.Boolean(), nullable=False),
        sa.Column("comments_observed_at", sa.DateTime(), nullable=False),
        sa.Column("profile_matches", sa.JSON(), nullable=False),
        sa.Column("feed_ranks", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), server_default="new", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_hacker_news_items_status_observed_at", "hacker_news_items", ["status", "observed_at"])


def downgrade():
    op.drop_index("ix_hacker_news_items_status_observed_at", table_name="hacker_news_items")
    op.drop_table("hacker_news_items")
