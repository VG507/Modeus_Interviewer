"""Track official documentation and retain revisions for editorial review."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    watches = op.create_table(
        "documentation_watches",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("checked_at", sa.DateTime()),
        sa.Column("attempted_at", sa.DateTime()),
        sa.Column("error", sa.Text()),
        sa.Column("token", sa.String(36)),
        sa.Column("lease_until", sa.DateTime()),
    )
    subscriptions = op.create_table(
        "documentation_subscriptions",
        sa.Column("watch_id", sa.String(48), sa.ForeignKey("documentation_watches.id"), primary_key=True),
        sa.Column("profession_id", sa.String(32), sa.ForeignKey("vacancy_collections.profession_id"), primary_key=True),
    )
    op.create_table(
        "documentation_revisions",
        sa.Column("item_id", sa.String(128), sa.ForeignKey("discovery_items.id"), primary_key=True),
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    targets = ("postgresql-explain", "sqlalchemy-pooling", "celery-retries",
               "postgresql-releases", "sqlalchemy-releases", "celery-releases")
    op.bulk_insert(watches, [{"id": target} for target in targets])
    profiles = sa.table("vacancy_collections", sa.column("profession_id"))
    if op.get_bind().scalar(sa.select(profiles.c.profession_id).where(profiles.c.profession_id == "python_backend")):
        op.bulk_insert(subscriptions, [{"watch_id": target, "profession_id": "python_backend"} for target in targets])


def downgrade():
    op.drop_table("documentation_revisions")
    op.drop_table("documentation_subscriptions")
    op.drop_table("documentation_watches")
    table = sa.table("discovery_items", sa.column("id"), sa.column("provider"))
    candidates = sa.table("discovery_candidates", sa.column("item_id"), sa.column("excerpt_id"))
    document_ids = sa.select(table.c.id).where(table.c.provider == "docs")
    connection = op.get_bind()
    excerpts = list(connection.scalars(sa.select(candidates.c.excerpt_id).where(
        candidates.c.item_id.in_(document_ids), candidates.c.excerpt_id.is_not(None))))
    connection.execute(candidates.delete().where(candidates.c.item_id.in_(document_ids)))
    connection.execute(table.delete().where(table.c.provider == "docs"))
    if excerpts:
        excerpt_table = sa.table("source_excerpts", sa.column("id"))
        connection.execute(excerpt_table.delete().where(excerpt_table.c.id.in_(excerpts)))
