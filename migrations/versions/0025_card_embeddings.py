"""Store vectors of published card cases and of vacancy duties for matching by meaning.

On PostgreSQL with pgvector the column has the vector type and search runs in SQL.
Without the extension, and on SQLite, it holds the same vector as JSON text.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.types import UserDefinedType

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


class Vector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "vector"


def pgvector(bind):
    if bind.dialect.name != "postgresql" or op.get_context().as_sql:
        return False
    if bind.scalar(sa.text("select 1 from pg_extension where extname = 'vector'")):
        return True
    if not bind.scalar(sa.text("select 1 from pg_available_extensions where name = 'vector'")):
        return False
    try:
        with bind.begin_nested():
            bind.execute(sa.text("create extension vector"))
    except sa.exc.DBAPIError:
        return False  # installed on the server, but this role may not create it
    return True


def upgrade():
    # The upstream branch originally created these tables under revision 0022.
    tables = set() if op.get_context().as_sql else set(sa.inspect(op.get_bind()).get_table_names())
    vector = Vector() if pgvector(op.get_bind()) else sa.Text()
    if "card_embeddings" not in tables:
        op.create_table(
            "card_embeddings",
            sa.Column("card_id", sa.String(36), sa.ForeignKey("knowledge_cards.id"), primary_key=True),
            sa.Column("case_index", sa.Integer(), primary_key=True),
            sa.Column("model", sa.String(200), nullable=False),
            sa.Column("text_hash", sa.String(64), nullable=False),
            sa.Column("embedding", vector, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    # Vectors of other texts by content, e.g. vacancy duties matched against interview cases.
    if "text_embeddings" not in tables:
        op.create_table(
            "text_embeddings",
            sa.Column("model", sa.String(200), primary_key=True),
            sa.Column("text_hash", sa.String(64), primary_key=True),
            sa.Column("embedding", vector, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )


def downgrade():
    op.drop_table("text_embeddings")
    op.drop_table("card_embeddings")
