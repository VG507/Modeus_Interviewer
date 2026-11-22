"""Index bge-m3 card vectors with HNSW so search by meaning stays fast as the catalog grows.

A pgvector index needs a fixed size, while the column holds any model's vectors, so the index
covers the default model's rows cast to vector(1024). Another model still searches correctly,
by an exact scan. scripts/benchmark_vector_index.py on 20k rows: exact 47.7 ms, HNSW 2.2 ms
(recall@10 0.97), IVFFlat 12.4 ms. IVFFlat also learns its lists from rows already in the table,
which a migration on an empty table cannot give it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

MODEL = "bge-m3"
DIMENSIONS = 1024
INDEX = "card_embeddings_bge_m3_hnsw"


def vector_column(bind):
    return bind.dialect.name == "postgresql" and not op.get_context().as_sql and bind.scalar(sa.text(
        "select udt_name from information_schema.columns "
        "where table_name = 'card_embeddings' and column_name = 'embedding'"
    )) == "vector"


def upgrade():
    if vector_column(op.get_bind()):
        op.execute(f"create index if not exists {INDEX} on card_embeddings "
                   f"using hnsw ((embedding::vector({DIMENSIONS})) vector_cosine_ops) where model = '{MODEL}'")


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"drop index if exists {INDEX}")
