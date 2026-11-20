"""Make HH profession profiles editable and reviewable."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vacancy_collections", sa.Column("title", sa.String(120), nullable=False, server_default=""))
    op.add_column("vacancy_collections", sa.Column("title_key", sa.String(360), nullable=False, server_default=""))
    op.add_column("vacancy_collections", sa.Column("search_query", sa.String(500), nullable=False, server_default=""))
    op.add_column("vacancy_collections", sa.Column("aliases", sa.JSON()))
    op.add_column("vacancy_collections", sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("vacancy_collections", sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"))
    table = sa.table("vacancy_collections", sa.column("profession_id"), sa.column("title"), sa.column("title_key"),
                     sa.column("search_query"), sa.column("approved", sa.Boolean()))
    for key, title, query in (
        ("python_backend", "Python backend", "NAME:(Python AND (backend OR разработчик OR developer))"),
        ("frontend", "Frontend", "NAME:(frontend OR front-end OR фронтенд OR react)"),
        ("qa", "QA", "NAME:(QA OR SDET OR тестировщик OR тестирование)"),
        ("devops", "DevOps", "NAME:(DevOps OR SRE OR DevSecOps OR platform)"),
    ):
        op.execute(table.update().where(table.c.profession_id == key).values(
            title=title, title_key=title.casefold(), search_query=query, approved=True,
        ))
    op.create_index("ix_vacancy_collections_title_key", "vacancy_collections", ["title_key"], unique=True)


def downgrade():
    op.drop_index("ix_vacancy_collections_title_key", table_name="vacancy_collections")
    for field in ("config_version", "approved", "aliases", "search_query", "title_key", "title"):
        op.drop_column("vacancy_collections", field)
